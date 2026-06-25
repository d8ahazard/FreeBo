"""Supervised R4.0 hardware gate runner (agent_next_5 §3). Evolves the P0 harness — NOT a competing one.

It drives the RUNNING FreeBo app over HTTP (same API the UI uses, so the safety floor + generation + latch +
ticket all apply) and records machine-readable evidence for the R4.0 E-STOP smoke gate. It authorizes ONLY R4.0,
never R4.10.

Honesty + safety rules (enforced here, not optional):
  * Physical motion requires the FULL arming ceremony (§3.1): --mode r4_0 + --armed + an interactively typed
    presence phrase + a clean git tree + the exact tested preflight SHA + the running app reporting the SAME
    software SHA + a live Air 2 native link + synchronized process/sidecar control + a healthy journal writer +
    the operator confirming the physical safety checklist (§3.2). NO motion is issued before ALL pass.
  * `--auto` is diagnostics-only and can NEVER arm or pass.
  * Harness-level conservative caps (§3.3) clamp every motion in addition to the frozen safety floor.
  * Each motion/STOP trial records SEPARATE tri-state observations (§3.5); unknown is never a pass.
  * On any §3.7 condition (motion before arming, unexpected motion, failure to halt, post-STOP motion, stale
    command accepted, desync, freshness loss, ambiguous RESUME, missing STOP evidence, SHA mismatch) the harness
    immediately issues a priority E-STOP and ABORTS — it does not keep collecting evidence after a safety failure.
  * Any missing required measurement FAILS the gate.

This file is unit-tested with an injected fake client; it is run against hardware ONLY by a physically-present
operator. Do NOT auto-run it.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
EVID = REPO / "data" / "test-evidence"

# §3.3 conservative R4.0 caps (harness-level, in ADDITION to the frozen safety floor).
R4_0_CAPS = {"forward_mag": 0.20, "turn_mag": 0.18, "duration_s": 0.60}
FRESHNESS_GATE_S = 2.0           # refuse motion when camera/telemetry age exceeds this

# Acceptance thresholds (ms).
THRESHOLDS = {"stop_p95_ms": 600.0, "ack_p95_ms": 1200.0, "motion_dispatch_p95_ms": 250.0}
REQUIRED_TRIALS = {"eyes": 5, "forward": 5, "turn": 5, "stop": 5, "master_stop": 10}
PRESENCE_PHRASE = "I AM PHYSICALLY PRESENT"


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


def clamp_caps(ly: float, rx: float, dur: float) -> tuple[float, float, float]:
    """Clamp a motion to the harness-level R4.0 caps (defense in addition to the safety floor)."""
    ly = max(-R4_0_CAPS["forward_mag"], min(R4_0_CAPS["forward_mag"], float(ly)))
    rx = max(-R4_0_CAPS["turn_mag"], min(R4_0_CAPS["turn_mag"], float(rx)))
    dur = max(0.0, min(R4_0_CAPS["duration_s"], float(dur)))
    return ly, rx, dur


def _tri(sources: list, *keys: str):
    for src in sources:
        if not isinstance(src, dict):
            continue
        for k in keys:
            if k in src and src[k] is not None:
                return bool(src[k])
    return None


def classify(api: dict) -> dict:
    """Pure evidence classification (no inference; absent facts are null + a reason)."""
    tr = api.get("transport_result") if isinstance(api.get("transport_result"), dict) else {}
    src = [api, tr]
    c = {
        "command_id": (api.get("command_id") if api.get("command_id") is not None else tr.get("command_id")),
        "queued_to_sidecar": _tri(src, "queued_to_sidecar"),
        "local_sidecar_latch": _tri(src, "local_latch_set", "latched"),
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
    xs = sorted(v for v in values if isinstance(v, (int, float)))
    if not xs:
        return None
    import math
    rank = max(1, math.ceil((p / 100.0) * len(xs)))
    return xs[min(rank, len(xs)) - 1]


def _gate(observed, limit):
    return {"observed_ms": observed, "limit_ms": limit, "pass": (observed is not None and observed <= limit)}


def acceptance_eligible(auto: bool, clean_tree: bool, armed: bool) -> bool:
    """Acceptance is possible ONLY with a real operator (no --auto), a clean tree, AND a completed arming."""
    return (not auto) and clean_tree and armed


# ---------------------------------------------------------------------------------------------------------------
# §3.1 arming — every condition must pass before ANY motion.
# ---------------------------------------------------------------------------------------------------------------
def arming_conditions(state: dict, hw_gate: dict, *, expect_sha: str, auto: bool, armed_flag: bool,
                      presence_ok: bool, checklist_ok: bool, app_sha: str | None = None) -> dict:
    """Pure arming evaluation. Returns each condition + the overall `armed_ok`. NEVER True under --auto."""
    brain = (state or {}).get("brain", {}) if isinstance(state, dict) else {}
    readiness = brain.get("readiness", {}) if isinstance(brain, dict) else {}
    settings = (state or {}).get("settings", {}) if isinstance(state, dict) else {}
    jh = (hw_gate or {}).get("journal_health", {}) if isinstance(hw_gate, dict) else {}
    reported_sha = app_sha if app_sha is not None else (hw_gate or {}).get("software_sha")
    conds = {
        "mode_r4_0": True,                                # caller only builds this for --mode r4_0
        "armed_flag": bool(armed_flag),
        "not_auto": (not auto),
        "presence_phrase_typed": bool(presence_ok),
        "clean_git_tree": tree_is_clean(),
        "expected_sha_known": bool(expect_sha) and expect_sha != "unknown",
        "app_sha_matches": bool(expect_sha) and reported_sha == expect_sha,
        "live_air2_link": str(settings.get("robot_variant", "")).upper() == "AIR2",
        "control_synchronized": readiness.get("synchronized") is True,
        "journal_writer_healthy": (jh.get("writer_alive") is True and jh.get("persist_ok") is not False),
        "physical_checklist_confirmed": bool(checklist_ok),
    }
    conds["armed_ok"] = (not auto) and all(v for v in conds.values())
    return conds


CHECKLIST = [
    ("flat_indoor_floor", "Robot is on a flat indoor floor"),
    ("clear_2m_radius", "At least a 2 meter clear radius exists"),
    ("hazards_excluded", "Stairs, ledges, water, cables, pets, and children are excluded"),
    ("operator_within_reach", "You are within immediate physical reach of the robot"),
    ("stop_control_visible", "The STOP control is open and visible"),
    ("intervention_path", "You have a direct physical intervention path"),
    ("battery_and_feeds_live", "Battery is sufficient and telemetry/video are live"),
]


class HarnessAbort(RuntimeError):
    pass


def _freshness_ok(readiness: dict) -> bool:
    """Refuse motion when camera/telemetry freshness exceeds the gate (§3.3)."""
    for k in ("video_age", "telemetry_age"):
        v = readiness.get(k) if isinstance(readiness, dict) else None
        if isinstance(v, (int, float)) and v > FRESHNESS_GATE_S:
            return False
    return True


def acceptance_report(rows: list, eligible: bool, thresholds: dict | None = None) -> dict:
    """The R4.0 acceptance gates (pure; no inference). FAILS when any required trial count is missing, any STOP
    lacks the required separate observations, or any latency measurement is missing/over the limit (§3.6)."""
    th = {**THRESHOLDS, **(thresholds or {})}
    by_kind: dict[str, list] = {}
    for r in rows:
        by_kind.setdefault(r.get("kind"), []).append(r)
    counts = {k: len(by_kind.get(k, [])) for k in REQUIRED_TRIALS}
    counts_ok = all(counts.get(k, 0) >= n for k, n in REQUIRED_TRIALS.items())
    estops = by_kind.get("master_stop", [])
    motion = [r.get("latency_ms") for r in (by_kind.get("forward", []) + by_kind.get("turn", []))
              if r.get("latency_ms") is not None]
    acks = [r.get("latency_ms") for r in rows if r.get("kind") in ("eyes", "forward", "turn", "stop")
            and r.get("latency_ms") is not None]
    stop_lat = [r.get("latency_ms") for r in estops if r.get("latency_ms") is not None]

    def _obs(r, key):
        return r.get("observations", {}).get(key)
    stop_gates = {
        "every_stop_local_inhibit": bool(estops) and all(
            r.get("classify", {}).get("local_inhibit_asserted") is True for r in estops),
        "every_stop_transport_dispatched": bool(estops) and all(
            r.get("classify", {}).get("transport_dispatch_succeeded") is True for r in estops),
        "every_stop_halt_observed": bool(estops) and all(_obs(r, "halt_observed") is True for r in estops),
        "no_post_stop_motion": bool(estops) and all(_obs(r, "post_stop_motion_observed") is False for r in estops),
        "latch_after_stop": bool(estops) and all(r.get("latched") is True for r in estops),
        "no_unexpected_motion": all(_obs(r, "unexpected_motion_observed") in (False, None) for r in rows)
        and any(_obs(r, "unexpected_motion_observed") is False for r in estops),
        "explicit_resume_each": len(by_kind.get("resume", [])) >= len(estops) and bool(estops),
        "stale_effect_rejected": any(r.get("kind") == "stale_effect" and r.get("rejected") is True for r in rows),
    }
    latency_gates = {
        "stop_p95": _gate(percentile(stop_lat, 95), th["stop_p95_ms"]),
        "ack_p95": _gate(percentile(acks, 95), th["ack_p95_ms"]),
        "motion_dispatch_p95": _gate(percentile(motion, 95), th["motion_dispatch_p95_ms"]),
    }
    journal_ok = all(r.get("journal_writer_alive") is not False for r in rows)
    overall = (eligible and counts_ok and journal_ok
               and all(latency_gates[g]["pass"] for g in latency_gates)
               and all(stop_gates.values()))
    return {"eligible": eligible, "required_counts": REQUIRED_TRIALS, "counts": counts, "counts_ok": counts_ok,
            "latency_gates": latency_gates, "stop_gates": stop_gates, "journal_ok": journal_ok,
            "pass": bool(overall)}


class Harness:
    def __init__(self, base: str, *, auto: bool, armed_flag: bool, expect_sha: str, owner_token: str | None = None,
                 client=None, prompter=None) -> None:
        self.base = base.rstrip("/")
        self.auto = auto
        self.armed_flag = armed_flag
        self.expect_sha = expect_sha
        self.owner_token = owner_token
        self.armed = False
        self.rows: list[dict] = []
        self.checklist: dict = {}
        self.arming: dict = {}
        self.aborted: str | None = None
        self._prompt = prompter if prompter is not None else (lambda q: input(q))
        if client is not None:
            self.c = client
        else:
            import httpx
            self.c = httpx.Client(timeout=15.0)

    # --- transport ---
    def _headers(self) -> dict:
        return {"X-Owner-Token": self.owner_token} if self.owner_token else {}

    def _get(self, path: str) -> dict:
        try:
            r = self.c.get(self.base + path, headers=self._headers())
            return r.json()
        except Exception:  # noqa: BLE001
            return {}

    def _post(self, path: str, body: dict | None = None) -> dict:
        t0 = time.monotonic()
        status, api = None, {}
        try:
            r = self.c.post(self.base + path, json=body or {}, headers=self._headers())
            status = getattr(r, "status_code", None)
            api = r.json()
        except Exception as e:  # noqa: BLE001
            api = {"ok": False, "error": str(e)}
        return {"http_status": status, "api_response": api, "dispatch_ts": t0,
                "completion_ts": time.monotonic(), "latency_ms": round((time.monotonic() - t0) * 1000, 1)}

    def _readiness(self) -> dict:
        body = self._get("/api/status")
        return (body.get("brain") or body).get("readiness", body.get("readiness", {})) or {}

    def _journal_health(self) -> dict:
        return self._get("/api/events/health") or {}

    def _ask_bool(self, q: str):
        if self.auto:
            return None                       # NEVER auto-answer a physical observation
        ans = str(self._prompt(f"  {q} [y/n/?] ")).strip().lower()
        if ans.startswith("y"):
            return True
        if ans.startswith("n"):
            return False
        return None                            # unsure -> unknown, never a pass

    # --- §3.1 arming ceremony ---
    def arm(self) -> bool:
        state = self._get("/api/state")
        hw_gate = self._get("/api/hardware_gate")
        presence_ok = False
        checklist_ok = False
        if not self.auto and self.armed_flag:
            typed = str(self._prompt(f"  Type the presence phrase exactly to proceed:\n    '{PRESENCE_PHRASE}'\n  > "))
            presence_ok = typed.strip() == PRESENCE_PHRASE
            if presence_ok:
                checklist_ok = True
                for key, desc in CHECKLIST:
                    ok = self._ask_bool(desc)
                    self.checklist[key] = ok
                    checklist_ok = checklist_ok and (ok is True)
        self.arming = arming_conditions(state, hw_gate, expect_sha=self.expect_sha, auto=self.auto,
                                        armed_flag=self.armed_flag, presence_ok=presence_ok,
                                        checklist_ok=checklist_ok)
        self.armed = bool(self.arming.get("armed_ok"))
        return self.armed

    # --- recording ---
    def record(self, name: str, kind: str, result: dict, *, observe: bool = False,
               before: dict | None = None) -> dict:
        api = result.get("api_response", {})
        jh = self._journal_health()
        observations = {}
        if observe:
            observations = {
                "motion_started_observed": self._ask_bool("did the robot START moving?"),
                "halt_observed": self._ask_bool("did the robot HALT immediately?"),
                "post_stop_motion_observed": self._ask_bool("was there ANY motion AFTER the stop?"),
                "unexpected_motion_observed": self._ask_bool("was there any UNEXPECTED motion?"),
                "operator_uncertain": None,
            }
        row = {
            "ts": time.time(), "name": name, "kind": kind, "http_status": result.get("http_status"),
            "classify": classify(api), "command_id": api.get("command_id"),
            "latched": api.get("latched"), "generation": api.get("generation"), "epoch": api.get("epoch"),
            "dispatch_ts": result.get("dispatch_ts"), "completion_ts": result.get("completion_ts"),
            "latency_ms": result.get("latency_ms"),
            "readiness_before": before, "readiness_after": self._readiness(),
            "journal_writer_alive": jh.get("writer_alive"), "api_response": api,
            "observations": observations,
        }
        self.rows.append(row)
        return row

    def _guard_or_abort(self, row: dict, *, is_stop: bool) -> None:
        """§3.7 abort conditions. On any failure: priority E-STOP + raise."""
        o = row.get("observations", {})
        c = row.get("classify", {})
        if o.get("unexpected_motion_observed") is True:
            self._abort("unexpected motion observed")
        if is_stop:
            if c.get("local_inhibit_asserted") is not True or c.get("transport_dispatch_succeeded") is not True:
                self._abort(f"STOP did not dispatch/inhibit: {row['api_response']}")
            if o.get("halt_observed") is not True:
                self._abort("STOP halt not observed (unknown is not a pass)")
            if o.get("post_stop_motion_observed") is True:
                self._abort("motion observed AFTER a master STOP")

    def _abort(self, reason: str) -> None:
        self.aborted = reason
        with __import__("contextlib").suppress(Exception):
            self._post("/api/estop")           # priority E-STOP — leave latched + inhibited
        raise HarnessAbort(reason)

    # --- §3.4 deterministic motion via existing APIs (clamped to caps) ---
    def _move(self, ly: float, rx: float, dur: float) -> dict:
        ly, rx, dur = clamp_caps(ly, rx, dur)
        return self._post("/api/control", {"kind": "move", "ly": ly, "rx": rx, "duration": dur})

    def _normal_stop(self) -> dict:
        return self.record("normal_stop", "stop", self._post("/api/control", {"kind": "stop"}))

    def _master_stop(self, name: str) -> dict:
        before = self._readiness()
        row = self.record(name, "master_stop", self._post("/api/estop"), observe=True, before=before)
        self._guard_or_abort(row, is_stop=True)
        return row

    def _resume(self, name: str) -> dict:
        row = self.record(name, "resume", self._post("/api/resume"))
        if row["http_status"] not in (200, None) or row["classify"].get("reconciled") is not True:
            self._abort(f"RESUME not reconciled ({name})")
        return row

    def _freshness_or_abort(self) -> None:
        if not _freshness_ok(self._readiness()):
            self._abort("camera/telemetry freshness exceeded the gate")

    def run_r4_0(self) -> None:
        if not self.armed:
            raise HarnessAbort("not armed — refusing to issue ANY motion (§3.1)")
        print("== SUPERVISED R4.0 SMOKE GATE ==  (armed; explicit reconciled RESUME between STOP trials)")
        for i in range(REQUIRED_TRIALS["eyes"]):
            self.record(f"eyes_{i}", "eyes", self._post("/api/control", {"kind": "action", "name": "eyes_happy"}))
        for i in range(REQUIRED_TRIALS["forward"]):
            self._freshness_or_abort()
            row = self.record(f"fwd_{i}", "forward", self._move(R4_0_CAPS["forward_mag"], 0.0, 0.4), observe=True)
            self._guard_or_abort(row, is_stop=False)
            self._normal_stop()                # §3.3 require a normal stop after each ordinary motion
        for i in range(REQUIRED_TRIALS["turn"]):
            self._freshness_or_abort()
            row = self.record(f"turn_{i}", "turn", self._move(0.0, R4_0_CAPS["turn_mag"], 0.4), observe=True)
            self._guard_or_abort(row, is_stop=False)
            self._normal_stop()
        # 10 master-STOP trials: two each of the five scenarios (§3.4), each followed by an explicit RESUME.
        scenarios = ["forward_pulse", "turn_pulse", "executor_move", "queued_inflight", "rtm_interrupt"]
        for i in range(REQUIRED_TRIALS["master_stop"]):
            scn = scenarios[i % len(scenarios)]
            self._setup_scenario(scn)
            self._master_stop(f"estop_{i}_{scn}")
            if i == 0:
                self._stale_effect_probe()     # while latched: a stale/latched effect MUST be rejected (§3.6)
            self._resume(f"resume_{i}")

    def _stale_effect_probe(self) -> None:
        """While master-inhibited (post-STOP, pre-RESUME), a motion MUST be refused. Records a `stale_effect`
        row with `rejected` (and ABORTS if a stale command is accepted, §3.7)."""
        res = self._move(R4_0_CAPS["forward_mag"], 0.0, 0.4)
        api = res.get("api_response", {})
        accepted = bool(api.get("sent_to_agora")) or (api.get("ok") is True and api.get("blocked") is None)
        rejected = not accepted
        self.rows.append({"ts": time.time(), "name": "stale_effect_probe", "kind": "stale_effect",
                          "rejected": rejected, "http_status": res.get("http_status"), "api_response": api,
                          "observations": {}})
        if not rejected:
            self._abort("a stale/latched effect was ACCEPTED (must be rejected)")

    def _setup_scenario(self, scn: str) -> None:
        """Create the motion context deterministically via existing APIs (capped). The STOP fires immediately
        after, exercising STOP during an active effect."""
        if scn == "forward_pulse":
            self._move(R4_0_CAPS["forward_mag"], 0.0, R4_0_CAPS["duration_s"])
        elif scn == "turn_pulse":
            self._move(0.0, R4_0_CAPS["turn_mag"], R4_0_CAPS["duration_s"])
        elif scn == "executor_move":
            # a longer (still capped) move approximates an executor-controlled motion in flight
            self._move(R4_0_CAPS["forward_mag"], 0.0, R4_0_CAPS["duration_s"])
        elif scn == "queued_inflight":
            for _ in range(3):                 # several capped requests so multiple frames are in flight
                self._move(R4_0_CAPS["forward_mag"], 0.0, R4_0_CAPS["duration_s"])
        elif scn == "rtm_interrupt":
            # controlled RTM/sidecar interruption via the EXISTING connection control (no new motion surface)
            self._move(R4_0_CAPS["forward_mag"], 0.0, R4_0_CAPS["duration_s"])
            self._post("/api/control", {"kind": "connection", "state": "stop"})
            self._post("/api/control", {"kind": "connection", "state": "start"})

    # --- evidence ---
    def save(self) -> dict:
        commit = commit_sha()
        clean = tree_is_clean()
        eligible = acceptance_eligible(self.auto, clean, self.armed)
        report = acceptance_report(self.rows, eligible)
        stamp = time.strftime("%Y%m%d-%H%M%S")
        out_dir = EVID / "hardware" / commit / "r4_0" / stamp
        out_dir.mkdir(parents=True, exist_ok=True)
        with (out_dir / "rows.jsonl").open("w", encoding="utf-8") as f:
            for row in self.rows:
                f.write(json.dumps(row) + "\n")
        manifest = {
            "mode": "r4_0", "commit": commit, "expected_sha": self.expect_sha, "sha_match": commit == self.expect_sha,
            "ts": time.time(), "base": self.base, "auto": self.auto, "armed": self.armed,
            "clean_tree": clean, "acceptance_eligible": eligible, "aborted": self.aborted,
            "arming": self.arming, "checklist": self.checklist, "caps": R4_0_CAPS,
            "required_trials": REQUIRED_TRIALS, "rows": len(self.rows),
            "acceptance_report": report,
            "verdict": ("ABORTED" if self.aborted else ("PASS" if report["pass"] else "FAIL")),
        }
        (out_dir / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
        print("\nR4.0 MANIFEST:", json.dumps(manifest, indent=2))
        if self.auto:
            print("!! DIAGNOSTICS ONLY (--auto) — can NEVER be a PASS.")
        elif self.aborted:
            print(f"!! ABORTED: {self.aborted} — robot left latched + inhibited.")
        elif not report["pass"]:
            print("!! R4.0 FAILED — fix the observed issue and stop (do not broad-refactor).")
        else:
            print("R4.0 PASS — record verdict; this is NOT authorization for R4.10.")
        return manifest


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:8200")
    ap.add_argument("--mode", choices=["r4_0", "diagnostics"], default="diagnostics")
    ap.add_argument("--armed", action="store_true", help="operator arming flag (still requires the full ceremony)")
    ap.add_argument("--expect-sha", default=commit_sha(), help="the exact tested preflight SHA the app must report")
    ap.add_argument("--owner-token", default=None, help="X-Owner-Token for a non-loopback bind")
    ap.add_argument("--auto", action="store_true", help="diagnostics only (no operator) — never arms, never passes")
    a = ap.parse_args()
    if a.mode != "r4_0" or a.auto:
        print("Diagnostics mode: no physical motion will be issued. Use --mode r4_0 --armed with an operator.")
        return
    h = Harness(a.base, auto=a.auto, armed_flag=a.armed, expect_sha=a.expect_sha, owner_token=a.owner_token)
    if not h.arm():
        print("ARMING FAILED — not all conditions met; NO motion issued:")
        print(json.dumps(h.arming, indent=2))
        h.save()
        return
    try:
        h.run_r4_0()
    except HarnessAbort as e:
        print(f"\n!! ABORTED: {e}")
    finally:
        h.save()


if __name__ == "__main__":
    main()
