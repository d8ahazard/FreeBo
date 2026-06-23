#!/usr/bin/env python3
"""Eye-animation discovery for the EBO SE.

The upstream bridge only knew eyes on/off (display/enable). The EBO SE actually renders animated eye
expressions; we don't have the official param map, so this script sweeps likely PARAM_SET targets and
pauses between each so YOU can watch the robot's face and record what happens.

It talks to a RUNNING Autobot app over its REST API (POST /api/debug/param), which sends one raw MAVLink
param-set frame through the in-process native link. Record the winners in
autobot/robot/frames.py EYE_ANIMATIONS once you've identified indices.

Usage:
    python3 probe_eyes.py --app http://127.0.0.1:8200 [--group display] \
        [--keys expression,eye,emoji,face,mood] [--range 0:12] [--dwell 2.5]
"""
import argparse, json, sys, time, urllib.request


def post(app, path, body=None):
    data = json.dumps(body).encode() if body is not None else b""
    req = urllib.request.Request(app.rstrip("/") + path, data=data,
                                 headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read() or b"{}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--app", required=True, help="Autobot base URL, e.g. http://127.0.0.1:8200")
    ap.add_argument("--group", default="display")
    ap.add_argument("--keys", default="expression,eye,emoji,face,mood")
    ap.add_argument("--range", default="0:12", help="value range lo:hi inclusive")
    ap.add_argument("--dwell", type=float, default=2.5)
    args = ap.parse_args()

    lo, hi = (int(x) for x in args.range.split(":"))
    keys = [k.strip() for k in args.keys.split(",") if k.strip()]

    print("Make sure the robot is AWAKE and you can see its face.")
    print("This sends raw param_set frames via the app endpoint /api/debug/param.\n")

    findings = {}
    for key in keys:
        print(f"\n=== group='{args.group}' key='{key}' ===")
        for val in range(lo, hi + 1):
            try:
                post(args.app, "/api/debug/param",
                     {"group": args.group, "key": key, "value": float(val)})
            except Exception as e:
                print(f"  (param send failed: {e}) — is the app running in native mode?")
                break
            time.sleep(args.dwell)
            note = input(f"  {key}={val}: what did the eyes do? (blank=skip, q=next key) ").strip()
            if note.lower() == "q":
                break
            if note:
                findings[f"{key}:{val}"] = note

    print("\n--- Findings ---")
    for k, v in findings.items():
        print(f"  {k} -> {v}")
    if findings:
        print("\nRecord the winners in autobot/robot/frames.py EYE_ANIMATIONS (and EBO_EYE_KEY if a "
              "different key worked), then restart the app so the brain's set_eyes tool picks them up.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
