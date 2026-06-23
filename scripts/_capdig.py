import json

rows = [json.loads(l) for l in open("data/captures/agora_signaling.jsonl", encoding="utf-8", errors="replace") if l.strip()]

# 1) The sent join_v3 ortc (what the browser advertises — esp. rtpCapabilities)
for r in rows:
    if r.get("kind") == "text" and r.get("dir") == "send" and '"join_v3"' in r.get("data", ""):
        m = json.loads(r["data"])["_message"]
        ortc = m.get("ortc", {})
        print("=== SENT join_v3 _message keys ===", list(m.keys()))
        print("codec=", m.get("codec"), "role=", m.get("role"))
        print("=== SENT join_v3 ortc keys ===", list(ortc.keys()))
        print("=== SENT ortc.rtpCapabilities ===")
        print(json.dumps(ortc.get("rtpCapabilities"), indent=1)[:3000])
        break

# 2) Any message mentioning audio/opus/publish/source/mute (sent), to find the publish/enable-audio verb
print("\n=== SENT text frames mentioning audio/publish/source/mute ===")
seen = set()
for r in rows:
    if r.get("kind") == "text" and r.get("dir") == "send":
        d = r.get("data", "")
        low = d.lower()
        if any(k in low for k in ('"audio"', "opus", "publish", "source", "mute", "111")):
            try:
                t = json.loads(d).get("_type")
            except Exception:
                t = "?"
            key = (t, "audio" in low, "publish" in low, "source" in low, "mute" in low)
            if key not in seen:
                seen.add(key)
                print(f"  [{t}] {d[:400]}")

# 3) All recv on_* types (to confirm no audio stream + see notifications)
print("\n=== recv on_* messages (unique) ===")
seenr = set()
for r in rows:
    if r.get("kind") == "text" and r.get("dir") == "recv":
        d = r.get("data", "")
        if '"on_' in d:
            try:
                m = json.loads(d)
                t = m.get("_type")
            except Exception:
                continue
            if t and t not in seenr:
                seenr.add(t)
                print(f"  {t}: {d[:300]}")
