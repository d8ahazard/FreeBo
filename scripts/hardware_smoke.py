"""P0-R4.0 / R4.10 hardware evidence harness.

Drives the RUNNING FreeBo app over HTTP and records machine-readable evidence for the Phase 0 hardware gates.
It does NOT bypass the app — every command goes through the same API the UI uses, so the safety floor +
generation + latch all apply. The OPERATOR confirms the physical effect (the robot has no ToF/IMU on Air 2,
so `robot_effect_observed` is the human's eyes — never an SDK send).

Run with the robot live and yourself watching it:

    python scripts/hardware_smoke.py --base http://127.0.0.1:8200 --mode smoke

Evidence is written as JSONL under data/test-evidence/ with an immutable summary tied to the tested commit.
Terminology stays precise: queued_to_sidecar != sdk_send_succeeded != robot_effect_observed.
"""
from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

import httpx

REPO = Path(__file__).resolve().parents[1]
EVID = REPO / "data" / "test-evidence"


def _commit() -> str:
    try:
        head = (REPO / ".git" / "HEAD").read_text(encoding="utf-8").strip()
        if head.startswith("ref:"):
            return (REPO / ".git" / head.split(" ", 1)[1].strip()).read_text(encoding="utf-8").strip()[:12]
        return head[:12]
    except Exception:  # noqa: BLE001
        return "unknown"


class Harness:
    def __init__(self, base: str, auto: bool) -> None:
        self.base = base.rstrip("/")
        self.auto = auto                 # auto=True: skip operator prompts (dry wiring test, not acceptance)
        self.c = httpx.Client(timeout=15.0)
        self.rows: list[dict] = []

    def _post(self, path: str, body: dict | None = None) -> dict:
        t0 = time.monotonic()
        try:
            r = self.c.post(self.base + path, json=body or {})
            api = r.json()
        except Exception as e:  # noqa: BLE001
            api = {"ok": False, "error": str(e)}
        return {"api_response": api, "latency_ms": round((time.monotonic() - t0) * 1000, 1)}

    def _telemetry(self) -> dict:
        try:
            return self.c.get(self.base + "/api/telemetry").json()
        except Exception:  # noqa: BLE001
            return {}

    def _ask(self, q: str) -> bool:
        if self.auto:
            return True
        return input(f"  {q} [y/N] ").strip().lower().startswith("y")

    def record(self, name: str, kind: str, result: dict, *, operator_q: str | None = None) -> None:
        api = result.get("api_response", {})
        row = {
            "ts": time.time(), "name": name, "kind": kind,
            # precise terminology — never conflate these:
            "queued_to_sidecar": bool(api.get("queued_to_sidecar", api.get("ok"))),
            "sdk_send_succeeded": bool(api.get("sent_to_agora", api.get("ok"))),
            "command_id": api.get("command_id"),
            "api_response": api,
            "latency_ms": result.get("latency_ms"),
            "latched": api.get("latched"),
            "generation": api.get("generation"),
            "telemetry": self._telemetry(),
        }
        if operator_q is not None:
            row["robot_effect_observed"] = self._ask(operator_q)
        self.rows.append(row)
        flag = row.get("robot_effect_observed")
        print(f"  [{name}] sdk_send={row['sdk_send_succeeded']} latch={row['latched']} "
              f"effect={flag} {row['latency_ms']}ms")

    # --- drive helpers go through the SAME manual control path the UI uses ---
    def drive(self, ly: float, rx: float, dur: float) -> dict:
        return self._post("/api/control", {"kind": "move", "ly": ly, "rx": rx, "duration": dur})

    def smoke(self) -> None:
        print("== P0-R4.0 E-STOP SMOKE GATE ==  (resume between trials)")
        # 5 acknowledged eye commands
        for i in range(5):
            self.record(f"eyes_{i}", "eyes", self._post("/api/control", {"kind": "action", "name": "eyes_happy"}))
        # 5 short forward pulses
        for i in range(5):
            self.record(f"fwd_{i}", "forward", self.drive(0.35, 0.0, 0.4),
                        operator_q="did it nudge forward?")
        # 5 short turns
        for i in range(5):
            self.record(f"turn_{i}", "turn", self.drive(0.0, 0.2, 0.4),
                        operator_q="did it turn a little?")
        # 5 normal stops
        for i in range(5):
            self.record(f"stop_{i}", "stop", self._post("/api/control", {"kind": "stop"}))
        # 10 latched master-STOP trials — operator induces the condition, then we STOP
        scenarios = ["holding forward", "while turning", "during an executor move", "several drives in flight",
                     "during RTM/sidecar interruption"]
        for i in range(10):
            sc = scenarios[i % len(scenarios)]
            if not self.auto:
                input(f"  TRIAL {i}: set up '{sc}' (start the motion), then press Enter to fire STOP...")
            else:
                self.drive(0.35, 0.0, 2.0)  # wiring-test motion
            res = self._post("/api/estop")
            self.record(f"estop_{i}_{sc.replace(' ', '_')}", "master_stop", res,
                        operator_q="did the robot HALT immediately with NO further motion?")
            # explicit RESUME for the next trial
            self.record(f"resume_{i}", "resume", self._post("/api/resume"))

    def save(self) -> None:
        EVID.mkdir(parents=True, exist_ok=True)
        commit = _commit()
        stamp = time.strftime("%Y%m%d-%H%M%S")
        raw = EVID / f"hardware_smoke_{stamp}_{commit}.jsonl"
        with raw.open("w", encoding="utf-8") as f:
            for row in self.rows:
                f.write(json.dumps(row) + "\n")
        estops = [r for r in self.rows if r["kind"] == "master_stop"]
        halted = [r for r in estops if r.get("robot_effect_observed")]
        summary = {
            "commit": commit, "ts": time.time(), "base": self.base, "rows": len(self.rows),
            "estop_trials": len(estops), "estop_halted_ok": len(halted),
            "estop_pass": len(estops) > 0 and len(halted) == len(estops),
            "raw": raw.name,
        }
        (EVID / f"hardware_smoke_{stamp}_{commit}.summary.json").write_text(
            json.dumps(summary, indent=2), encoding="utf-8")
        print("\nSUMMARY:", json.dumps(summary, indent=2))
        if not summary["estop_pass"]:
            print("!! E-STOP gate FAILED — fix before continuing (P0-R4.0).")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:8200")
    ap.add_argument("--mode", choices=["smoke"], default="smoke")
    ap.add_argument("--auto", action="store_true", help="skip operator prompts (wiring test only, NOT acceptance)")
    a = ap.parse_args()
    h = Harness(a.base, a.auto)
    try:
        h.smoke()
    finally:
        h.save()


if __name__ == "__main__":
    main()
