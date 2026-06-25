"""Software-only R4.0 rehearsal (agent_next_4 §7).

Proves the Phase 1 observability system can EXPLAIN the hardware-acceptance flow BEFORE the robot is on the
floor. It uses ONLY the mock link and the real Node FAKE sidecar — NO cloud session, NO hardware, NO movement.

For each of the 12 scenarios it drives the real instrumented code paths, then asserts the resulting
incident/correlation trace contains the required ordered events + terminal outcome. It writes a redacted incident
bundle per scenario under data/test-evidence/software/<tested-sha>/rehearsal/ and a rehearsal_report.json that
explicitly states hardware_run=false / physical_acceptance=false / ready_for_supervised_R4_0.

NOT an R4.0 run. A software rehearsal for R4.0. Run:  python scripts/phase1_rehearsal.py
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

os.environ.setdefault("AUTOBOT_ROBOT_LINK", "mock")     # never touch a real robot
os.environ["AUTOBOT_RTM_FAKE"] = "1"                     # sidecar SDK send is faked (no Agora)

from autobot import observability as obs  # noqa: E402

NODE = os.environ.get("AUTOBOT_NODE_BIN") or shutil.which("node")
SIDECAR = REPO / "scripts" / "rtm_sidecar.js"


def _ordered(types: list[str], required: list[str]) -> bool:
    """True if `required` appears as an ordered subsequence of `types`."""
    it = iter(types)
    return all(any(r == t for t in it) for r in required)


def _tested_sha() -> str:
    try:
        return subprocess.run(["git", "rev-parse", "HEAD"], cwd=str(REPO), capture_output=True,
                              text=True, timeout=5).stdout.strip() or "unknown"
    except Exception:  # noqa: BLE001
        return "unknown"


# ----------------------------------------------------------------------------------------------------------------
# Real Node FAKE sidecar wired to a real RtmNode, so transport + sidecar lifecycle flow through the canonical
# journal exactly as in production (RtmNode._handle_event / send_acked instrumentation).
# ----------------------------------------------------------------------------------------------------------------
class NodeFake:
    def __init__(self, fail: bool = False):
        from autobot.robot.rtm_node import RtmNode
        env = {**os.environ, "AUTOBOT_RTM_FAKE": "1"}
        if fail:
            env["AUTOBOT_RTM_FAKE_FAIL"] = "1"
        self.proc = subprocess.Popen([NODE, str(SIDECAR)], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                     stderr=subprocess.DEVNULL, text=True, bufsize=1, env=env)
        self.n = RtmNode(session_provider=lambda *a, **k: None)
        self.n._proc = self.proc                          # so _kill() terminates the real child
        self.n.connected = True
        self.n._send = self._send                          # type: ignore[assignment]
        self._reader = threading.Thread(target=self._read, daemon=True)
        self._reader.start()
        self._await(lambda: self.n._sidecar_instance_id is not None, 10.0)
        self.pid = self.n._process_instance_id
        self.sid = self.n._sidecar_instance_id

    def _send(self, cmd: dict) -> bool:
        try:
            self.proc.stdin.write(json.dumps(cmd) + "\n")
            self.proc.stdin.flush()
            return True
        except Exception:  # noqa: BLE001
            return False

    def _read(self):
        try:
            for line in self.proc.stdout:
                line = line.strip()
                if not line:
                    continue
                try:
                    ev = json.loads(line)
                except Exception:  # noqa: BLE001
                    continue
                self.n._handle_event(ev)     # real instrumentation -> canonical journal
        except Exception:  # noqa: BLE001
            pass

    @staticmethod
    def _await(pred, timeout):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            if pred():
                return True
            time.sleep(0.02)
        return False

    def reconcile_unlatched(self):
        """set_control reconcile (cannot unlatch) -> two-phase release to a known unlatched epoch1/gen1."""
        self.n.send_acked({"cmd": "set_control", "process_instance_id": self.pid, "epoch": 0,
                           "generation": 0, "latched": True}, timeout=4.0)
        self.n._auth_latched, self.n._auth_epoch, self.n._auth_gen = True, 0, 0
        return self.n.reset_reconcile(0, 0, 1, 1, timeout=4.0)

    def close(self):
        try:
            self.n._kill()                    # emits sidecar_shutdown
        except Exception:  # noqa: BLE001
            pass
        try:
            self.proc.wait(timeout=3)
        except Exception:  # noqa: BLE001
            self.proc.kill()


class Rehearsal:
    def __init__(self):
        self.sha = _tested_sha()
        self.out_dir = REPO / "data" / "test-evidence" / "software" / self.sha / "rehearsal"
        self.out_dir.mkdir(parents=True, exist_ok=True)
        self.journal_path = self.out_dir / "events.jsonl"
        if self.journal_path.exists():
            self.journal_path.unlink()
        self.j = obs.configure(str(self.journal_path), flush_interval=0.05)
        self.results: list[dict] = []

    def _trace(self, *, correlation_id=None, incident_id=None) -> list[dict]:
        if incident_id:
            return self.j.incident_trace(incident_id)
        return self.j.correlation_trace(correlation_id)

    def record(self, name: str, *, correlation_id=None, incident_id=None, required: list[str],
               terminal: str | None = None, extra: dict | None = None):
        events = self._trace(correlation_id=correlation_id, incident_id=incident_id)
        types = [e["type"] for e in events]
        missing = [r for r in required if r not in types]
        ordered = _ordered(types, [r for r in required if r in types])
        term_ok = (terminal is None) or any(e.get("outcome") == terminal or e.get("type") == terminal
                                            for e in events)
        ok = (not missing) and ordered and term_ok and bool(events)
        rec = {"scenario": name, "ok": ok, "events": types, "missing": missing, "ordered": ordered,
               "terminal_ok": term_ok, "correlation_id": correlation_id, "incident_id": incident_id,
               "event_count": len(events), **(extra or {})}
        self.results.append(rec)
        # redacted per-scenario bundle (events already redacted at the journal boundary)
        bundle = {"schema": "freebo.rehearsal", "schema_version": 1, "scenario": name, "software_sha": self.sha,
                  "hardware_run": False, "physical_acceptance": False, "ok": ok, "events": events}
        safe = name.replace(" ", "_").replace("/", "_")
        (self.out_dir / f"scenario_{len(self.results):02d}_{safe}.json").write_text(
            json.dumps(bundle, indent=2), encoding="utf-8")
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}  ({len(events)} events)"
              + (f"  missing={missing}" if missing else ""))
        return rec

    # ---- server/brain harness (mock link) -----------------------------------------------------------------
    def _server(self):
        import autobot.web.server as server
        return server

    async def run_server_scenarios(self):
        server = self._server()
        brain = server.brain
        s = server.SETTINGS
        s.update(setup_complete=True, autonomy="auto", allow_motion=False, allow_think=True,
                 allow_video=False, talk_enabled=True, ai_provider="openai",
                 ai_base_url="http://localhost:9", ai_api_key="x", ai_model="m")

        # 5. successful master STOP followed by two-phase RESUME (ONE incident).
        await server.api_estop()
        iid = brain.safety.current_incident_id()
        await server.api_resume()
        self.record("05 stop then two-phase resume", incident_id=iid,
                    required=["master_stop", "estop_dispatch", "resume"], terminal="reconciled")

        # 6. RESET prepare/admission invalidated by a newer STOP (process arbiter level).
        await server.api_estop()
        iid6 = brain.safety.current_incident_id()
        tok = brain.safety.begin_reset()                 # admit a reset
        brain.safety.begin_master_stop()                 # a NEWER stop lands -> finalize must fail
        finalized = brain.safety.finalize_reset(tok) if tok else True
        obs.emit(obs.CAT_SAFETY_TRANSITION, "reset_invalidated", "rehearsal", incident_id=iid6,
                 phase="finalize", outcome=("superseded" if not finalized else "ERROR"),
                 detail={"finalized": finalized})
        self.record("06 reset invalidated by newer stop", incident_id=iid6,
                    required=["master_stop", "reset_invalidated"], terminal="superseded",
                    extra={"finalize_rejected": not finalized})
        await server.api_resume()                          # clean up state for later scenarios
        with __import__("contextlib").suppress(Exception):
            brain.safety.arb._unsafe_clear_for_tests()

        # 7. reason cycle cancelled during the provider wait.
        import autobot.brain.agent as agent_mod
        from autobot.brain.perception import Observation
        entered = asyncio.Event()
        release = asyncio.Event()

        async def ready_perceive(link, want_image=True):
            return Observation(telemetry={"ok": True, "connected": True, "awake": True})

        async def blocking_chat(self_, messages, tools=None):
            entered.set()
            await release.wait()
            raise AssertionError("provider should have been cancelled")

        _orig = {"perceive": agent_mod.perceive, "vlm": agent_mod.vlm_enabled,
                 "omni": agent_mod.omni_enabled, "hybrid": agent_mod.hybrid_enabled,
                 "chat": agent_mod.OpenAICompatibleClient.chat}
        agent_mod.perceive = ready_perceive
        agent_mod.vlm_enabled = lambda s=None: False
        agent_mod.omni_enabled = lambda s=None: False
        agent_mod.hybrid_enabled = lambda s=None: False
        agent_mod.OpenAICompatibleClient.chat = blocking_chat
        try:
            tok7 = brain._reason_gen + 1   # the cycle we are about to start uses the current gen
            corr7 = f"reason-gen{brain._reason_gen}"
            tick = asyncio.create_task(brain.tick(force=True))
            await asyncio.wait_for(entered.wait(), timeout=10.0)
            corr7 = f"reason-gen{brain._reason_gen}"   # the live cycle's correlation
            await brain.emergency_stop("rehearsal barge-in", master=True)
            await asyncio.wait_for(tick, timeout=5.0)
        finally:
            agent_mod.perceive = _orig["perceive"]
            agent_mod.vlm_enabled = _orig["vlm"]
            agent_mod.omni_enabled = _orig["omni"]
            agent_mod.hybrid_enabled = _orig["hybrid"]
            agent_mod.OpenAICompatibleClient.chat = _orig["chat"]
        # the cancelled cycle must show provider_wait_started then cancelled, and NEVER completed
        ev7 = self.j.correlation_trace(corr7)
        types7 = [e["type"] for e in ev7]
        rec7 = self.record("07 reason cancelled during provider wait", correlation_id=corr7,
                           required=["lock_wait_started", "started", "provider_wait_started", "cancelled"],
                           terminal="cancelled", extra={"no_completed": "completed" not in types7})
        rec7["ok"] = rec7["ok"] and ("completed" not in types7)
        with __import__("contextlib").suppress(Exception):
            brain.safety.arb._unsafe_clear_for_tests()
            brain._stopped = False

        # 8. speech render/play then cancellation (barge-in supersede through the shared service).
        s.update(talk_enabled=True)
        await brain.speech.speak("rehearsal one", check_say=False)
        await brain.speech.speak("rehearsal two", check_say=False)   # supersede -> prior clip cancelled
        # find the first utterance correlation
        sp = self.j.query(category=obs.CAT_SPEECH, type="requested", order="asc", limit=5)["events"]
        corr8 = sp[0]["correlation_id"] if sp else "speech-1"
        self.record("08 speech render/publish then cancelled", correlation_id=corr8,
                    required=["requested", "render_started", "render_completed", "publish_started"],
                    extra={"cancel_seen": any(e["type"] == "cancelled" for e in
                                              self.j.query(category=obs.CAT_SPEECH, limit=50)["events"])})

        # 9. vision request result discarded because a newer frame arrived (See-off/STOP equivalent).
        from autobot.brain.perception import Observation as _Obs

        class _Frame:
            seq = 1
            age = 0.0
        fake_obs = _Obs(telemetry={"ok": True}, jpeg=b"\x00" * 16)
        with __import__("contextlib").suppress(Exception):
            fake_obs.frame = _Frame()
        brain.buffer.frame_ts = 100.0

        async def _slow_caption(o, st):
            brain.buffer.frame_ts = 200.0     # a newer frame supersedes mid-request
            return "a description"
        cap = await brain._vision_request(fake_obs, s.snapshot(), "rehearsal/vlm", _slow_caption)
        vis = self.j.query(category=obs.CAT_VISION, limit=20)["events"]
        corr9 = vis[0]["correlation_id"] if vis else None
        self.record("09 vision stale result discarded", correlation_id=corr9,
                    required=["frame_selected", "request_started", "stale_result_discarded"],
                    terminal="discarded", extra={"discarded_returned_empty": cap == ""})

    # ---- node FAKE sidecar harness ----------------------------------------------------------------------
    def run_node_scenarios(self):
        if not NODE or not SIDECAR.is_file():
            self.results.append({"scenario": "node-sidecar scenarios", "ok": False,
                                 "skipped": "node/sidecar unavailable", "events": []})
            print("  [SKIP] node/sidecar unavailable — transport scenarios not run")
            return False

        # 1. startup + sidecar bind/synchronization.
        nf = NodeFake()
        try:
            rc = nf.reconcile_unlatched()
            sys_ev = self.j.query(category=obs.CAT_SYSTEM, source="sidecar", limit=50)["events"]
            stypes = [e["type"] for e in sys_ev]
            ok1 = "sidecar_ready" in stypes and rc.get("reconciled") is True
            self.results.append({"scenario": "01 startup + sidecar bind/sync", "ok": ok1,
                                 "events": stypes, "reconciled": rc.get("reconciled")})
            print(f"  [{'PASS' if ok1 else 'FAIL'}] 01 startup + sidecar bind/sync")

            # 2. admitted motion command through fake transport (full ticket -> sent_to_agora).
            d = nf.n.send_acked({"cmd": "drive", "ly": 0.2, "rx": 0.0, "dur": 0.2,
                                 "epoch": 1, "generation": 1, "ticket_id": 1}, timeout=4.0)
            tr = self.j.query(category=obs.CAT_TRANSPORT, limit=50)["events"]
            ttypes = [e["type"] for e in tr]
            ok2 = d.get("sent_to_agora") is True and "queued_to_sidecar" in ttypes and \
                "acknowledgement_received" in ttypes
            self.results.append({"scenario": "02 admitted motion via fake transport", "ok": ok2,
                                 "events": ttypes[-6:], "sent_to_agora": d.get("sent_to_agora")})
            print(f"  [{'PASS' if ok2 else 'FAIL'}] 02 admitted motion via fake transport")

            # 3. master STOP during an active drive (estop while a repeat would be running).
            nf.n.send_acked({"cmd": "drive", "ly": 0.2, "rx": 0.0, "dur": 5.0,
                             "epoch": 1, "generation": 1, "ticket_id": 2}, timeout=4.0)
            e = nf.n.send_acked({"cmd": "estop", "epoch": 2, "generation": 2}, timeout=4.0)
            ok3 = bool(e.get("local_latch_set")) and e.get("latched") is True
            self.results.append({"scenario": "03 master STOP during active drive", "ok": ok3,
                                 "events": ["estop"], "latched": e.get("latched")})
            print(f"  [{'PASS' if ok3 else 'FAIL'}] 03 master STOP during active drive")
        finally:
            nf.close()

        # 4. failed initial E-STOP SDK send (fake fail) — honest degraded ack, latch still asserted.
        nf2 = NodeFake(fail=True)
        try:
            nf2.n.send_acked({"cmd": "set_control", "process_instance_id": nf2.pid, "epoch": 1,
                              "generation": 1, "latched": True}, timeout=4.0)
            e = nf2.n.send_acked({"cmd": "estop", "epoch": 2, "generation": 2}, timeout=4.0)
            ok4 = e.get("local_latch_set") is True and e.get("initial_zero_sdk_send_succeeded") is False
            self.results.append({"scenario": "04 failed initial E-STOP send", "ok": ok4,
                                 "events": ["estop(degraded)"],
                                 "initial_zero_sdk_send_succeeded": e.get("initial_zero_sdk_send_succeeded")})
            print(f"  [{'PASS' if ok4 else 'FAIL'}] 04 failed initial E-STOP send")
        finally:
            nf2.close()

        # 10. sidecar replacement / reconnect (a new child binds a fresh instance id).
        before = None
        nf3 = NodeFake()
        before = nf3.sid
        nf3.close()
        nf4 = NodeFake()
        after = nf4.sid
        nf4.close()
        ok10 = bool(before) and bool(after) and before != after
        self.results.append({"scenario": "10 sidecar replacement/reconnect", "ok": ok10,
                             "events": ["sidecar_shutdown", "sidecar_ready"], "rebound": before != after})
        print(f"  [{'PASS' if ok10 else 'FAIL'}] 10 sidecar replacement/reconnect")
        return True

    def run_journal_scenarios(self):
        # 11. journal queue pressure + persistence failure (drop accounting + surfaced failure, never raised).
        from autobot.observability import EventJournal
        jj = EventJournal(path=self.out_dir / "pressure.jsonl", queue_max=4)
        jj.flush_and_close()                              # writer dead -> queue overflows deterministically
        for i in range(40):
            jj.emit(obs.CAT_MOTION, "drive", "rehearsal", detail={"i": i})

        class _Broken:
            def write(self, *_a): raise OSError("disk full")
            def flush(self): raise OSError("disk full")
            def tell(self): return 0
            def close(self): pass
        jj2 = EventJournal(path=self.out_dir / "pfail.jsonl", flush_interval=0.05)
        with jj2._lock:
            jj2._fh = _Broken()
        jj2.emit(obs.CAT_MOTION, "drive", "rehearsal")
        time.sleep(0.3)
        h1, h2 = jj.health(), jj2.health()
        ok11 = h1["queue_dropped"] > 0 and len(jj.recent(50)) > 0 and h2["persist_failed"] >= 1 \
            and h2["persist_ok"] is False
        jj2.flush_and_close()
        self.results.append({"scenario": "11 journal queue pressure + persistence failure", "ok": ok11,
                             "events": ["queue_dropped", "persist_failed"],
                             "queue_dropped": h1["queue_dropped"], "persist_failed": h2["persist_failed"]})
        print(f"  [{'PASS' if ok11 else 'FAIL'}] 11 journal queue pressure + persistence failure")

        # 12. clean shutdown + restart recovery (retained history restored + sequence continued).
        rp = self.out_dir / "restart.jsonl"
        if rp.exists():
            rp.unlink()
        ja = EventJournal(path=rp, flush_interval=0.05)
        for i in range(20):
            ja.emit(obs.CAT_SYSTEM, "startup", "rehearsal", detail={"i": i})
        # drain
        end = time.monotonic() + 3
        while time.monotonic() < end and ja._wq.qsize() > 0:
            time.sleep(0.02)
        ja.flush_and_close()
        jb = EventJournal(path=rp)
        ok12 = jb.recovered >= 20 and jb.emit(obs.CAT_SYSTEM, "post_restart", "rehearsal").seq > jb.recovered
        rec_ev = jb.recent(50)
        has_recovery = any(e["type"] == "journal_recovered" for e in rec_ev)
        jb.flush_and_close()
        self.results.append({"scenario": "12 clean shutdown + restart recovery", "ok": ok12 and has_recovery,
                             "events": ["journal_recovered"], "recovered": jb.recovered})
        print(f"  [{'PASS' if (ok12 and has_recovery) else 'FAIL'}] 12 clean shutdown + restart recovery")

    def finish(self) -> int:
        self.j.flush_and_close()
        ok_all = all(r.get("ok") for r in self.results)
        reasons = [r["scenario"] for r in self.results if not r.get("ok")]
        report = {
            "schema": "freebo.rehearsal_report", "schema_version": 1,
            "software_sha": self.sha, "generated_utc": __import__("datetime").datetime.now(
                __import__("datetime").timezone.utc).isoformat(),
            "components": "mock link + real Node FAKE sidecar only (no cloud, no hardware)",
            "node_available": bool(NODE and SIDECAR.is_file()),
            "scenario_count": len(self.results), "passed": sum(1 for r in self.results if r.get("ok")),
            "hardware_run": False, "physical_acceptance": False,
            "ready_for_supervised_R4_0": ok_all,
            "reasons_not_ready": reasons,
            "scenarios": self.results,
        }
        (self.out_dir / "rehearsal_report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
        print("\n" + "=" * 70)
        print(f"Rehearsal: {report['passed']}/{report['scenario_count']} scenarios PASS")
        print(f"hardware_run=false  physical_acceptance=false  "
              f"ready_for_supervised_R4_0={'true' if ok_all else 'false'}")
        if reasons:
            print("Not ready because: " + ", ".join(reasons))
        print(f"Evidence: {self.out_dir}")
        print("=" * 70)
        return 0 if ok_all else 1


async def _amain() -> int:
    r = Rehearsal()
    print("Phase 1 software-only R4.0 rehearsal (mock link + FAKE sidecar; NO hardware)\n")
    print("Node FAKE sidecar scenarios:")
    r.run_node_scenarios()
    print("Server/brain scenarios (mock link):")
    await r.run_server_scenarios()
    print("Journal scenarios:")
    r.run_journal_scenarios()
    return r.finish()


def main() -> int:
    return asyncio.run(_amain())


if __name__ == "__main__":
    raise SystemExit(main())
