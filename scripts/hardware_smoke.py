"""P0-R4.0 / R4.10 hardware evidence harness (rewritten — P0 §8).

Drives the RUNNING FreeBo app over HTTP and records machine-readable evidence for the Phase 0 hardware gates.
It does NOT bypass the app — every command goes through the same API the UI uses, so the safety floor +
generation + latch + ticket all apply.

Honesty rules (enforced here, NOT optional):
  * Evidence is NEVER inferred from `ok`. Three independent facts are kept separate and default to null when
    the API doesn't report them: `queued_to_sidecar`, `sdk_send_succeeded`, `robot_effect_observed`.
  * `robot_effect_observed` is the OPERATOR's eyes (Air 2 has no ToF/IMU we read here). It is NEVER auto-set.
    Under `--auto` it stays null and acceptance is impossible (`acceptance_eligible=false`, diagnostics only).
  * Acceptance requires a CLEAN git tree + a real operator (`--auto` off). A dirty tree is diagnostics only.
  * A failed STOP or a failed/!reconciled RESET ABORTS the run (we never keep driving under an unknown latch).
  * The summary records the tested commit, HTTP status + body, process+sidecar readiness before/after, and
    dispatch/completion timestamps. It NEVER prints PASS under `--auto`.

Do NOT run this against the robot as part of this directive — it is wired + unit-tested here only.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
EVID = REPO / "data" / "test-evidence"


def commit_sha() -> str:
    try:
        return subprocess.run(["git", "rev-parse", "HEAD"], cwd=str(REPO), capture_output=True,
                              text=True, timeout=10).stdout.strip() or "unknown"
    except Exception:  # noqa: BLE001
        return "unknown"


def tree_is_clean() -> bool:
    try:
        out = subprocess.run(["git", "status", "--porcelain"], cwd=str(REPO), capture_output=True,
                             text=True, timeout=10).stdout
        return out.strip() == ""
    except Exception:  # noqa: BLE001
        return False


def _tri(sources: list, *keys: str):
    """Tri-state bool for the FIRST present key across `sources` (top-level then nested), else None. Absent !=
    False, and NEVER inferred from `ok`."""
    for src in sources:
        if not isinstance(src, dict):
            continue
        for k in keys:
            if k in src and src[k] is not None:
                return bool(src[k])
    return None


def classify(api: dict) -> dict:
    """Pure evidence classification (agent_next_2 §8.1). Consumes BOTH top-level and NESTED `transport_result`
    facts WITHOUT inference; absent facts are null + a reason. Never substitutes transport_dispatch_succeeded for
    the individual SDK facts."""
    tr = api.get("transport_result") if isinstance(api.get("transport_result"), dict) else {}
    src = [api, tr]
    c = {
        "command_id": (api.get("command_id") if api.get("command_id") is not None else tr.get("command_id")),
        "queued_to_sidecar": _tri(src, "queued_to_sidecar"),
        "local_sidecar_latch": _tri(src, "local_latch_set", "latched"),
        "sdk_send_attempted": _tri(src, "sdk_send_attempted"),
        "initial_zero_sdk_send_succeeded": _tri(src, "initial_zero_sdk_send_succeeded"),
        "sdk_send_succeeded": _tri(src, "sent_to_agora", "sdk_send_succeeded", "initial_zero_sdk_send_succeeded"),
        "retry_count": (tr.get("retry_count") if tr.get("retry_count") is not None else api.get("retry_count")),
        "local_inhibit_asserted": _tri(src, "local_inhibit_asserted"),
        "transport_dispatch_succeeded": _tri(src, "transport_dispatch_succeeded"),
        "reconciled": _tri(src, "reconciled", "resumed"),
        "epoch": (api.get("epoch") if api.get("epoch") is not None else tr.get("epoch")),
        "generation": (api.get("generation") if api.get("generation") is not None else tr.get("generation")),
        "sidecar_dispatch_ts": tr.get("dispatch_ts"),
        "sidecar_completion_ts": tr.get("completion_ts"),
        "reasons": [],
    }
    for k in ("queued_to_sidecar", "sdk_send_succeeded", "local_inhibit_asserted"):
        if c[k] is None:
            c["reasons"].append(f"{k}=unknown (API did not report it)")
    return c


def percentile(values: list, p: float):
    """Nearest-rank percentile (p in 0..100). None for an empty list."""
    xs = sorted(v for v in values if isinstance(v, (int, float)))
    if not xs:
        return None
    import math
    rank = max(1, math.ceil((p / 100.0) * len(xs)))
    return xs[min(rank, len(xs)) - 1]


# Acceptance thresholds (agent_next_2 §8.3). All times in milliseconds.
THRESHOLDS = {"stop_p95_ms": 600.0, "tts_cancel_p95_ms": 300.0, "ack_p95_ms": 1200.0, "motion_dispatch_p95_ms": 250.0}


def acceptance_report(rows: list, eligible: bool, thresholds: dict | None = None) -> dict:
    """Compute the acceptance gates from recorded rows (pure; no inference). Returns per-gate pass/fail + the
    overall verdict. NEVER passes when not eligible. Latencies use endpoint->initial-zero-send where available."""
    th = {**THRESHOLDS, **(thresholds or {})}
    estops = [r for r in rows if r.get("kind") == "master_stop"]
    stop_latencies = [r.get("stop_latency_ms") for r in estops if r.get("stop_latency_ms") is not None]
    acks = [r.get("latency_ms") for r in rows if r.get("kind") in ("eyes", "forward", "turn", "stop")
            and r.get("latency_ms") is not None]
    motion = [r.get("latency_ms") for r in rows if r.get("kind") in ("forward", "turn")
              and r.get("latency_ms") is not None]
    tts = [r.get("tts_cancel_ms") for r in rows if r.get("tts_cancel_ms") is not None]
    gates = {
        "stop_p95": _gate(percentile(stop_latencies, 95), th["stop_p95_ms"]),
        "ack_p95": _gate(percentile(acks, 95), th["ack_p95_ms"]),
        "motion_dispatch_p95": _gate(percentile(motion, 95), th["motion_dispatch_p95_ms"]),
        "tts_cancel_p95": _gate(percentile(tts, 95), th["tts_cancel_p95_ms"]),
        "every_stop_observed_halt": all(r.get("robot_effect_observed") is True for r in estops) and bool(estops),
        "no_post_stop_motion": all(r.get("post_stop_motion_observed") is False for r in estops) and bool(estops),
        "stale_effect_rejected": any(r.get("kind") == "stale_effect" and r.get("rejected") is True for r in rows),
    }
    overall = eligible and all(g.get("pass") for g in gates.values() if isinstance(g, dict)) \
        and all(v for k, v in gates.items() if not isinstance(v, dict))
    return {"eligible": eligible, "gates": gates, "pass": bool(overall)}


def _gate(observed, limit):
    """A latency gate: pass only when we HAVE a measurement and it is within the limit (missing = fail, not pass)."""
    return {"observed_ms": observed, "limit_ms": limit, "pass": (observed is not None and observed <= limit)}


def acceptance_eligible(auto: bool, clean_tree: bool) -> bool:
    """Acceptance is possible ONLY with a real operator (no --auto) AND a clean tree."""
    return (not auto) and clean_tree


def estop_gate_pass(rows: list[dict], eligible: bool) -> bool:
    """The E-STOP gate passes ONLY when eligible AND every master-stop trial both dispatched its transport
    and was OBSERVED by the operator to halt. Missing observation (null) never counts as a pass."""
    if not eligible:
        return False
    estops = [r for r in rows if r.get("kind") == "master_stop"]
    if not estops:
        return False
    return all(r.get("robot_effect_observed") is True
               and r.get("classify", {}).get("transport_dispatch_succeeded") is True
               for r in estops)


class HarnessAbort(RuntimeError):
    pass


class Harness:
    def __init__(self, base: str, auto: bool, *, client=None) -> None:
        self.base = base.rstrip("/")
        self.auto = auto
        self.rows: list[dict] = []
        if client is not None:
            self.c = client
        else:
            import httpx
            self.c = httpx.Client(timeout=15.0)

    # --- transport ---
    def _post(self, path: str, body: dict | None = None) -> dict:
        t0 = time.monotonic()
        status, api = None, {}
        try:
            r = self.c.post(self.base + path, json=body or {})
            status = getattr(r, "status_code", None)
            api = r.json()
        except Exception as e:  # noqa: BLE001
            api = {"ok": False, "error": str(e)}
        return {"http_status": status, "api_response": api,
                "dispatch_ts": t0, "completion_ts": time.monotonic(),
                "latency_ms": round((time.monotonic() - t0) * 1000, 1)}

    def _readiness(self) -> dict:
        try:
            r = self.c.get(self.base + "/api/status")
            body = r.json()
            return (body.get("brain") or body).get("readiness", body.get("readiness", {})) or {}
        except Exception:  # noqa: BLE001
            return {}

    def _ask(self, q: str):
        if self.auto:
            return None                       # NEVER auto-set the physical effect
        ans = input(f"  {q} [y/n/?] ").strip().lower()
        if ans.startswith("y"):
            return True
        if ans.startswith("n"):
            return False
        return None                            # operator unsure -> unknown, not a pass

    def record(self, name: str, kind: str, result: dict, *, operator_q: str | None = None,
               before: dict | None = None) -> dict:
        api = result.get("api_response", {})
        row = {
            "ts": time.time(), "name": name, "kind": kind,
            "http_status": result.get("http_status"),
            "classify": classify(api),
            "command_id": api.get("command_id"),
            "latched": api.get("latched"), "generation": api.get("generation"), "epoch": api.get("epoch"),
            "dispatch_ts": result.get("dispatch_ts"), "completion_ts": result.get("completion_ts"),
            "latency_ms": result.get("latency_ms"),
            "readiness_before": before, "readiness_after": self._readiness(),
            "api_response": api,
            "robot_effect_observed": (self._ask(operator_q) if operator_q is not None else None),
        }
        self.rows.append(row)
        print(f"  [{name}] http={row['http_status']} sdk_send={row['classify']['sdk_send_succeeded']} "
              f"latch={row['latched']} effect={row['robot_effect_observed']} {row['latency_ms']}ms")
        return row

    def drive(self, ly: float, rx: float, dur: float) -> dict:
        return self._post("/api/control", {"kind": "move", "ly": ly, "rx": rx, "duration": dur})

    # --- gates ---
    def _master_stop(self, name: str, scenario: str) -> dict:
        before = self._readiness()
        if not self.auto:
            input(f"  TRIAL: set up '{scenario}' (start the motion), then press Enter to fire STOP...")
        res = self._post("/api/estop")
        row = self.record(name, "master_stop", res,
                          operator_q="did the robot HALT immediately with NO further motion?", before=before)
        c = row["classify"]
        # ABORT on a failed STOP: the local inhibit MUST be asserted and the transport MUST have dispatched.
        if c.get("local_inhibit_asserted") is not True or c.get("transport_dispatch_succeeded") is not True:
            raise HarnessAbort(f"STOP did not dispatch/inhibit ({name}): {row['api_response']}")
        return row

    def _resume(self, name: str) -> dict:
        before = self._readiness()
        res = self._post("/api/resume")
        row = self.record(name, "resume", res, before=before)
        # Do NOT continue under an unknown latch: a resume that wasn't reconciled (ok/resumed) ABORTS.
        if row["http_status"] not in (200, None) or row["classify"].get("reconciled") is not True:
            raise HarnessAbort(f"RESUME not reconciled ({name}); refusing to continue: {row['api_response']}")
        return row

    def smoke(self) -> None:
        print("== P0-R4.0 E-STOP SMOKE GATE ==  (explicit reconciled RESUME between trials)")
        for i in range(5):
            self.record(f"eyes_{i}", "eyes", self._post("/api/control", {"kind": "action", "name": "eyes_happy"}))
        for i in range(5):
            self.record(f"fwd_{i}", "forward", self.drive(0.35, 0.0, 0.4), operator_q="did it nudge forward?")
        for i in range(5):
            self.record(f"turn_{i}", "turn", self.drive(0.0, 0.2, 0.4), operator_q="did it turn a little?")
        for i in range(5):
            self.record(f"stop_{i}", "stop", self._post("/api/control", {"kind": "stop"}))
        scenarios = ["holding forward", "while turning", "during an executor move", "several drives in flight",
                     "during RTM/sidecar interruption"]
        for i in range(10):
            self._master_stop(f"estop_{i}_{scenarios[i % len(scenarios)].replace(' ', '_')}",
                              scenarios[i % len(scenarios)])
            self._resume(f"resume_{i}")

    def save(self, *, aborted: str | None = None) -> dict:
        EVID.mkdir(parents=True, exist_ok=True)
        commit = commit_sha()
        clean = tree_is_clean()
        eligible = acceptance_eligible(self.auto, clean)
        stamp = time.strftime("%Y%m%d-%H%M%S")
        raw = EVID / f"hardware_smoke_{stamp}_{commit[:12]}.jsonl"
        with raw.open("w", encoding="utf-8") as f:
            for row in self.rows:
                f.write(json.dumps(row) + "\n")
        estops = [r for r in self.rows if r["kind"] == "master_stop"]
        observed = [r for r in estops if r.get("robot_effect_observed") is True]
        summary = {
            "commit": commit, "ts": time.time(), "base": self.base, "auto": self.auto,
            "clean_tree": clean, "acceptance_eligible": eligible, "aborted": aborted,
            "rows": len(self.rows), "estop_trials": len(estops), "estop_observed_halt": len(observed),
            "estop_pass": estop_gate_pass(self.rows, eligible), "raw": raw.name,
        }
        (EVID / f"hardware_smoke_{stamp}_{commit[:12]}.summary.json").write_text(
            json.dumps(summary, indent=2), encoding="utf-8")
        print("\nSUMMARY:", json.dumps(summary, indent=2))
        if not eligible:
            print("!! DIAGNOSTICS ONLY (acceptance_eligible=false): "
                  + ("--auto set" if self.auto else "dirty tree") + " — this run can NEVER be a PASS.")
        elif not summary["estop_pass"]:
            print("!! E-STOP gate FAILED — fix before continuing (P0-R4.0).")
        return summary


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:8200")
    ap.add_argument("--mode", choices=["smoke"], default="smoke")
    ap.add_argument("--auto", action="store_true",
                    help="diagnostics only (no operator) — acceptance_eligible=false, never a PASS")
    a = ap.parse_args()
    h = Harness(a.base, a.auto)
    aborted = None
    try:
        h.smoke()
    except HarnessAbort as e:
        aborted = str(e)
        print(f"\n!! ABORTED: {e}")
    finally:
        h.save(aborted=aborted)


if __name__ == "__main__":
    main()
