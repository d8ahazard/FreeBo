/**
 * Agora signaling capture — reverse-engineering harness.
 *
 * Wraps window.WebSocket BEFORE the Agora Web SDK loads, so we record every frame (sent + received, text +
 * binary) on connections to Agora's gateway/AP servers. This gives us the exact join/signaling wire protocol
 * to replicate natively in Python (aiortc), so we can drop the browser from the media path entirely.
 *
 * Frames are POSTed to /api/agora/capture -> data/captures/agora_signaling.jsonl on the server.
 * Enable by importing this first in main.tsx. Captures only Agora hosts; everything else is untouched.
 */
const AGORA_HOST = /(agora\.io|sd-rtn\.com|agoraio\.cn|edge\.agora)/i;

function post(rec: Record<string, unknown>) {
  try {
    navigator.sendBeacon?.("/api/agora/capture", JSON.stringify(rec)) ||
      fetch("/api/agora/capture", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(rec) }).catch(() => {});
  } catch { /* */ }
}

function b64(buf: ArrayBuffer): string {
  const bytes = new Uint8Array(buf);
  let s = "";
  for (let i = 0; i < bytes.length; i++) s += String.fromCharCode(bytes[i]);
  return btoa(s);
}

/** Serialize a request body (string / binary / FormData) for capture — the AP requests are FormData. */
function bodyCapture(body: any, url?: string): { reqBody?: string; reqB64?: string } {
  if (body == null) return {};
  if (typeof body === "string") return { reqBody: body };
  if (body instanceof ArrayBuffer) return { reqB64: b64(body) };
  if (ArrayBuffer.isView(body)) return { reqB64: b64((body as ArrayBufferView).buffer as ArrayBuffer) };
  if (body instanceof URLSearchParams) return { reqBody: body.toString() };
  if (typeof FormData !== "undefined" && body instanceof FormData) {
    const obj: Record<string, unknown> = {};
    body.forEach((v: any, k: string) => {
      if (typeof v === "string") {
        obj[k] = v;
      } else {  // Blob/File -> async read, post a supplementary frame keyed by field
        obj[k] = `[blob ${v?.size}b ${v?.type}]`;
        try {
          v.arrayBuffer().then((ab: ArrayBuffer) => post({ ts: Date.now(), url: url || "?", dir: "form-blob", field: k, b64: b64(ab) })).catch(() => {});
        } catch { /* */ }
      }
    });
    return { reqBody: "FORMDATA:" + JSON.stringify(obj) };
  }
  return { reqBody: "[unserialized:" + (body?.constructor?.name || typeof body) + "]" };
}

function logFrame(url: string, dir: "send" | "recv", data: unknown) {
  if (typeof data === "string") {
    post({ ts: Date.now(), url, dir, kind: "text", data });
  } else if (data instanceof ArrayBuffer) {
    post({ ts: Date.now(), url, dir, kind: "bin", b64: b64(data) });
  } else if (data instanceof Blob) {
    data.arrayBuffer().then((ab) => post({ ts: Date.now(), url, dir, kind: "bin", b64: b64(ab) })).catch(() => {});
  } else {
    post({ ts: Date.now(), url, dir, kind: "other", data: String(data).slice(0, 200) });
  }
}

function installHttpCapture() {
  // The AP/unilbs gateway lookup happens over HTTPS (fetch/XHR), not WebSocket — capture it too so we have
  // the gateway address + cert/ticket needed to form a native join_v3.
  const origFetch = window.fetch?.bind(window);
  if (origFetch && !(window.fetch as any).__agoracap) {
    const wrapped = async (input: any, init?: any) => {
      const url = typeof input === "string" ? input : input?.url || String(input);
      const isAgora = AGORA_HOST.test(url);
      const reqCap = isAgora ? bodyCapture(init?.body, url) : {};
      const resp = await origFetch(input, init);
      if (isAgora) {
        try {
          const clone = resp.clone();
          const text = await clone.text();
          post({ ts: Date.now(), url, dir: "http", method: init?.method || "GET", ...reqCap, respBody: text.slice(0, 8000) });
        } catch { /* */ }
      }
      return resp;
    };
    (wrapped as any).__agoracap = true;
    window.fetch = wrapped as any;
  }
  // XHR (the SDK may use XHR for the AP lookup)
  const OrigXHR = window.XMLHttpRequest;
  if (OrigXHR && !(OrigXHR as any).__agoracap) {
    const open = OrigXHR.prototype.open;
    const send = OrigXHR.prototype.send;
    OrigXHR.prototype.open = function (this: any, method: string, url: string, ...rest: any[]) {
      this.__agoraUrl = url; this.__agoraMethod = method;
      return open.call(this, method, url, ...(rest as [boolean]));
    };
    OrigXHR.prototype.send = function (this: any, body?: any) {
      if (AGORA_HOST.test(this.__agoraUrl || "")) {
        const reqCap = bodyCapture(body, this.__agoraUrl);
        this.addEventListener("load", () => {
          try { post({ ts: Date.now(), url: this.__agoraUrl, dir: "xhr", method: this.__agoraMethod, ...reqCap, respBody: String(this.responseText || "").slice(0, 8000) }); } catch { /* */ }
        });
      }
      return send.call(this, body);
    };
    (OrigXHR as any).__agoracap = true;
  }
}

export function installAgoraCapture() {
  installHttpCapture();
  const Orig = window.WebSocket;
  if ((Orig as any).__agoracap) return;
  const Wrapped = function (this: WebSocket, url: string | URL, protocols?: string | string[]) {
    const ws = new Orig(url as any, protocols as any);
    const u = String(url);
    if (AGORA_HOST.test(u)) {
      post({ ts: Date.now(), url: u, dir: "open" });
      ws.addEventListener("message", (e) => logFrame(u, "recv", (e as MessageEvent).data));
      ws.addEventListener("close", () => post({ ts: Date.now(), url: u, dir: "close" }));
      const origSend = ws.send.bind(ws);
      ws.send = (d: any) => { logFrame(u, "send", d); return origSend(d); };
    }
    return ws;
  } as any;
  Wrapped.prototype = Orig.prototype;
  Wrapped.CONNECTING = Orig.CONNECTING; Wrapped.OPEN = Orig.OPEN;
  Wrapped.CLOSING = Orig.CLOSING; Wrapped.CLOSED = Orig.CLOSED;
  Wrapped.__agoracap = true;
  window.WebSocket = Wrapped;
  // eslint-disable-next-line no-console
  console.log("[agoracap] Agora signaling capture installed");
}
