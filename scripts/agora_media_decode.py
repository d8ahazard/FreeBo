"""Decode the protobuf-framed Agora media frames from the capture to see if the H265 payload is in the clear.
Generic protobuf wire walker (no schema needed) -> finds the media payload -> checks for RTP/H265 signatures."""
import base64, json, collections, math

PATH = "data/captures/agora_signaling.jsonl"


def rv(b, i):
    shift = 0; out = 0
    while i < len(b):
        c = b[i]; i += 1
        out |= (c & 0x7F) << shift
        if not (c & 0x80):
            return out, i
        shift += 7
    return out, i


def walk(b):
    """Return list of (field_no, wiretype, value) for one protobuf message."""
    i = 0; out = []
    while i < len(b):
        try:
            tag, i = rv(b, i)
        except Exception:
            break
        f = tag >> 3; wt = tag & 7
        if wt == 0:
            v, i = rv(b, i); out.append((f, 0, v))
        elif wt == 2:
            ln, i = rv(b, i); sub = b[i:i+ln]; i += ln; out.append((f, 2, sub))
        elif wt == 5:
            out.append((f, 5, b[i:i+4])); i += 4
        elif wt == 1:
            out.append((f, 1, b[i:i+8])); i += 8
        else:
            break
    return out


def entropy(b):
    if not b:
        return 0.0
    c = collections.Counter(b); n = len(b)
    return -sum((x/n) * math.log2(x/n) for x in c.values())


def classify(payload):
    if not payload:
        return "empty"
    b0 = payload[0]
    if payload[:4] == b"\x00\x00\x00\x01" or payload[:3] == b"\x00\x00\x01":
        return "H264/H265 Annex-B start code"
    if b0 == 0x80 or b0 == 0x90:  # RTP version 2
        pt = payload[1] & 0x7F if len(payload) > 1 else "?"
        return f"RTP packet (v2, pt={pt})"
    e = entropy(payload[:256])
    if e > 7.3:
        return f"high-entropy (likely ENCRYPTED, H={e:.2f})"
    return f"unknown (H={e:.2f}, first={payload[:4].hex()})"


rows = []
with open(PATH, encoding="utf-8") as f:
    for line in f:
        line = line.strip()
        if line:
            try:
                rows.append(json.loads(line))
            except Exception:
                pass

media_shown = 0
for r in rows:
    if r.get("kind") != "bin" or not r.get("b64"):
        continue
    raw = base64.b64decode(r["b64"])
    top = walk(raw)
    if not top:
        continue
    f0, wt0, v0 = top[0]
    # media-data frames had outer field #1 == 100 (the '08 64' prefix)
    if f0 == 1 and wt0 == 0 and v0 == 100:
        # field #2 (bytes) is the submessage
        sub = next((v for (f, w, v) in top if f == 2 and w == 2), None)
        if sub is None:
            continue
        inner = walk(sub)
        print(f"\n--- MEDIA FRAME len={len(raw)} ---")
        for (f, w, v) in inner:
            if w == 2:
                print(f"  field {f} (bytes len {len(v)}): {classify(v)}  head={v[:20].hex()}")
            else:
                print(f"  field {f} (wt{w}): {v if w == 0 else v.hex()}")
        media_shown += 1
        if media_shown >= 4:
            break

if media_shown == 0:
    print("no field#1==100 media frames found; dumping top-level field nums of first 10 binary frames:")
    n = 0
    for r in rows:
        if r.get("kind") == "bin" and r.get("b64"):
            raw = base64.b64decode(r["b64"]); top = walk(raw)
            print("  len", len(raw), "fields:", [(f, w, (v if w == 0 else len(v))) for (f, w, v) in top][:6])
            n += 1
            if n >= 10:
                break
