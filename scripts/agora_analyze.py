"""Analyze the captured Agora signaling to map the join protocol for the native (aiortc) reimplementation."""
import json, collections, sys

PATH = "data/captures/agora_signaling.jsonl"
rows = []
with open(PATH, encoding="utf-8") as f:
    for line in f:
        line = line.strip()
        if line:
            try:
                rows.append(json.loads(line))
            except Exception:
                pass

types = collections.Counter()
urls = collections.Counter()
inner = []
for r in rows:
    urls[r.get("url", "?")] += 1
    if r.get("kind") == "text":
        try:
            m = json.loads(r["data"])
            t = m.get("_type", "(no _type)")
            types[(r.get("dir"), t)] += 1
            inner.append((r.get("dir"), t, m))
        except Exception:
            types[(r.get("dir"), "(unparsed-text)")] += 1
    elif r.get("kind") == "bin":
        types[(r.get("dir"), "(binary)")] += 1

print("=== frames:", len(rows), " text-json:", len(inner), "===")
print("\n=== URLs ===")
for u, c in urls.most_common():
    print(f"  {c:4d}  {u}")
print("\n=== message types (dir, _type): count ===")
for (d, t), c in sorted(types.items(), key=lambda x: -x[1]):
    print(f"  {c:4d}  {d:5s} {t}")

# Show the sequence of types (the state machine), compactly
print("\n=== sequence (dir _type) ===")
seq = [f"{d}:{t}" for (d, t, _m) in inner]
print("  " + " -> ".join(seq[:60]))

# Find subscribe / publish / media-relevant messages
print("\n=== subscribe/publish/media messages (first of each kind) ===")
seen = set()
for d, t, m in inner:
    if any(k in t for k in ("subscribe", "publish", "ice", "candidate", "media", "stream", "video", "audio", "dtls", "offer", "answer", "sdp")):
        if t not in seen:
            seen.add(t)
            print(f"  [{d}] {t}: {json.dumps(m)[:500]}")

# The join RESPONSE (the recv right after a send:join_v3) — carries the gateway's ICE/DTLS/SRTP setup.
print("\n=== join response (gateway ortc) ===")
import base64
for i, r in enumerate(rows):
    if r.get("kind") == "text" and '"join_v3"' in r.get("data", "") and r.get("dir") == "send":
        # next text recv on same url
        for r2 in rows[i+1:i+8]:
            if r2.get("kind") == "text" and r2.get("dir") == "recv":
                m = json.loads(r2["data"])
                print("  RESP:", json.dumps(m)[:1500])
                break
        break

# Binary media frame headers — identify framing (DTLS handshake? SRTP? custom Agora?).
print("\n=== first binary media frames (url:size:first16bytes hex) ===")
shown = 0
for r in rows:
    if r.get("kind") == "bin" and r.get("b64"):
        raw = base64.b64decode(r["b64"])
        print(f"  {r.get('url','?').split('//')[-1]:42s} len={len(raw):5d}  {raw[:16].hex()}")
        shown += 1
        if shown >= 12:
            break
