/**
 * In-browser wired ADB over WebUSB (the "nice over browser" path) using @yume-chan/ya-webadb.
 *
 * This lets the WebUI talk ADB to a USB-tethered phone with NO host `adb` installed — handy on a fresh
 * machine. It's loaded dynamically with a computed specifier so the app still builds/runs even when the
 * optional packages aren't installed (then webusbSupported() drives the UI to fall back to host adb).
 *
 * Scope: connect + read device info + run shell commands. Installing the patched APK in-browser (sync push +
 * `pm install`) is a follow-up; until then the backend (host adb) handles install. See docs/TODO-FREEBO.md.
 *
 * Requires: npm i @yume-chan/adb @yume-chan/adb-daemon-webusb @yume-chan/adb-credential-web  (Chrome/Edge).
 */

export function webusbSupported(): boolean {
  return typeof navigator !== "undefined" && !!(navigator as unknown as { usb?: unknown }).usb;
}

type AnyMod = Record<string, unknown>;

async function loadLibs(): Promise<{ adb: AnyMod; usb: AnyMod; cred: AnyMod } | null> {
  try {
    // Computed specifiers so bundlers don't hard-fail when the optional deps are absent.
    const adb = (await import(/* @vite-ignore */ "@yume-chan/" + "adb")) as AnyMod;
    const usb = (await import(/* @vite-ignore */ "@yume-chan/" + "adb-daemon-webusb")) as AnyMod;
    const cred = (await import(/* @vite-ignore */ "@yume-chan/" + "adb-credential-web")) as AnyMod;
    return { adb, usb, cred };
  } catch {
    return null;
  }
}

export interface WebAdbSession {
  shell: (command: string) => Promise<string>;
  info: Record<string, string>;
  close: () => Promise<void>;
}

/** Prompt the user to pick a USB device, authorize the RSA key on the phone, and open an ADB session. */
export async function connect(): Promise<WebAdbSession> {
  if (!webusbSupported()) throw new Error("WebUSB not supported in this browser (use Chrome/Edge over https/localhost).");
  const libs = await loadLibs();
  if (!libs) throw new Error("WebADB libraries not installed. Run: npm i @yume-chan/adb @yume-chan/adb-daemon-webusb @yume-chan/adb-credential-web");

  const { adb, usb, cred } = libs as {
    adb: { Adb: any; AdbDaemonTransport: any };
    usb: { AdbDaemonWebUsbDeviceManager: any };
    cred: { default: any };
  };

  const manager = usb.AdbDaemonWebUsbDeviceManager.BROWSER;
  if (!manager) throw new Error("WebUSB device manager unavailable.");
  const device = await manager.requestDevice();
  if (!device) throw new Error("No device selected.");
  const connection = await device.connect();
  const credentialStore = new cred.default("FreeBo");
  const transport = await adb.AdbDaemonTransport.authenticate({
    serial: device.serial,
    connection,
    credentialStore,
  });
  const client = new adb.Adb(transport);

  const shell = async (command: string): Promise<string> => {
    const proc = await client.subprocess.spawn(command);
    const reader = proc.stdout.getReader();
    const chunks: Uint8Array[] = [];
    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      if (value) chunks.push(value);
    }
    return new TextDecoder().decode(concat(chunks));
  };

  const info: Record<string, string> = { serial: device.serial };
  try {
    info.model = (await shell("getprop ro.product.model")).trim();
    info.android = (await shell("getprop ro.build.version.release")).trim();
  } catch {
    /* best-effort */
  }

  return {
    shell,
    info,
    close: async () => {
      try {
        await client.close();
      } catch {
        /* ignore */
      }
    },
  };
}

function concat(chunks: Uint8Array[]): Uint8Array {
  const total = chunks.reduce((n, c) => n + c.length, 0);
  const out = new Uint8Array(total);
  let off = 0;
  for (const c of chunks) {
    out.set(c, off);
    off += c.length;
  }
  return out;
}
