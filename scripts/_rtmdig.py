import json
import re
import sys

P = sys.argv[1] if len(sys.argv) > 1 else "collector/captured/ebo_audio.txt"
rows = []
for line in open(P, encoding="utf-8", errors="replace"):
    m = re.search(r"AUTOBOT_(RTM|HTTP)[^:]*:\s*(.*)$", line)
    if not m:
        continue
    kind, body = m.group(1), m.group(2).strip()
    rows.append((kind, body))

# Parse RTM messages
rtm = []
for kind, body in rows:
    if kind != "RTM":
        continue
    try:
        j = json.loads(body)
        rtm.append(j)
    except Exception:
        pass

print(f"=== total RTM msgs: {len(rtm)} ===")

# Count by id + show a sample payload + cadence
from collections import defaultdict
times = defaultdict(list)
sample = {}
for j in rtm:
    i = j.get("id")
    ts = j.get("timestamp")
    times[i].append(ts)
    if i not in sample:
        sample[i] = j.get("data")

print("\n=== RTM id: count, median interval (s), sample data ===")
for i in sorted(times, key=lambda k: -len(times[k])):
    ts = sorted(t for t in times[i] if isinstance(t, (int, float)))
    if len(ts) >= 2:
        diffs = sorted((ts[k+1]-ts[k])/1000.0 for k in range(len(ts)-1))
        med = diffs[len(diffs)//2]
        cad = f"~{med:.1f}s" if med < 1e6 else "?"
    else:
        cad = "once"
    print(f"  id={i:<7} count={len(times[i]):<4} cadence={cad:<8} data={json.dumps(sample[i])[:90]}")

# Connect sequence: first 20 RTM ids in order
print("\n=== first 20 RTM (id : data) in order ===")
for j in rtm[:20]:
    print(f"  {j.get('id')} : {json.dumps(j.get('data'))[:80]}")

# Teardown: last 25 RTM ids in order (the session-kill sequence)
print("\n=== last 25 RTM (id : data) in order ===")
for j in rtm[-25:]:
    print(f"  {j.get('id')} : {json.dumps(j.get('data'))[:80]}")

# Drive (101007) cadence detail
dts = sorted(t for j in rtm if j.get("id") == 101007 for t in [j.get("timestamp")] if isinstance(t, (int, float)))
if len(dts) >= 2:
    gaps = [(dts[k+1]-dts[k])/1000.0 for k in range(len(dts)-1)]
    print(f"\n=== drive(101007): {len(dts)} frames, gaps min/med/max = "
          f"{min(gaps):.2f}/{sorted(gaps)[len(gaps)//2]:.2f}/{max(gaps):.2f}s ===")
    # show a sample of drive payloads
    for j in [x for x in rtm if x.get("id") == 101007][:6]:
        print("   drive data:", json.dumps(j.get("data"))[:100])

# Drive (101007) per-axis value ranges — the numbers we calibrate the motion model to.
drives = [j.get("data") for j in rtm if j.get("id") == 101007 and isinstance(j.get("data"), dict)]
if drives:
    print(f"\n=== drive(101007): per-axis ranges over {len(drives)} frames ===")
    for ax in ("lx", "ly", "rx", "ry"):
        vals = [d.get(ax) for d in drives if isinstance(d.get(ax), (int, float))]
        nz = [v for v in vals if v]
        if vals:
            print(f"  {ax}: min={min(vals):<5} max={max(vals):<5} "
                  f"abs_max={max(abs(v) for v in vals):<5} nonzero_frames={len(nz)}")
    btns = sorted({d.get("buttons") for d in drives if "buttons" in d}, key=lambda x: (x is None, x))
    print(f"  buttons seen: {btns}")
    # Distinct nonzero motion vectors (dedup), most→least common, to read off discrete stick steps.
    from collections import Counter
    vecs = Counter((d.get("lx", 0), d.get("ly", 0), d.get("rx", 0), d.get("ry", 0))
                   for d in drives if any(d.get(a) for a in ("lx", "ly", "rx", "ry")))
    print("  top nonzero (lx,ly,rx,ry) vectors:")
    for vec, c in vecs.most_common(12):
        print(f"    {vec}  x{c}")

# HTTP endpoints hit
print("\n=== HTTP requests (unique URLs) ===")
seen = set()
for kind, body in rows:
    if kind != "HTTP":
        continue
    mu = re.search(r"url=(https?://[^,}\s]+)", body)
    if mu and mu.group(1) not in seen:
        seen.add(mu.group(1))
        print("  " + mu.group(1)[:120])
