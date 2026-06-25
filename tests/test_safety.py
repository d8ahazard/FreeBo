"""Safety floor regression tests — the mechanical guarantees the AI cannot bypass."""
from __future__ import annotations

from conftest import settings

from autobot.brain.safety import SafetyFloor


def _sf():
    sf = SafetyFloor()
    sf.begin_tick()
    return sf


def test_speed_is_clamped_to_max_speed():
    sf = _sf()
    d = sf.check_drive(settings(autonomy="auto", allow_motion=True, max_speed=0.5), 1.0, 0.0, 1.0, source="ai")
    assert d.allowed
    assert abs(d.ly) <= 0.5 + 1e-6


def test_manual_autonomy_blocks_ai_motion():
    d = _sf().check_drive(settings(autonomy="manual"), 0.5, 0.0, 1.0, source="ai")
    assert not d.allowed and "autonomy" in d.reason


def test_manual_source_moves_even_in_manual_mode():
    # The human at the UI is always allowed (clamped, not autonomy-gated).
    d = _sf().check_drive(settings(autonomy="manual", max_speed=0.6), 0.5, 0.0, 1.0, source="manual")
    assert d.allowed


def test_allow_motion_off_blocks_ai():
    d = _sf().check_drive(settings(autonomy="auto", allow_motion=False), 0.5, 0.0, 1.0, source="ai")
    assert not d.allowed


def test_rate_limit_per_tick():
    sf = _sf()
    s = settings(autonomy="auto", allow_motion=True, max_speed=1.0)
    allowed = sum(1 for _ in range(12) if sf.check_drive(s, 0.2, 0.0, 0.4, source="ai").allowed)
    assert allowed == s.max_actions_per_tick


def test_duration_capped():
    d = _sf().check_drive(settings(autonomy="auto", allow_motion=True, max_speed=1.0), 0.2, 0.0, 999, source="ai")
    assert d.duration <= settings().max_move_duration + 1e-9


def test_conversational_zeros_forward_keeps_turn():
    d = _sf().check_drive(settings(autonomy="auto", allow_motion=True, mode="conversational", max_speed=1.0),
                          1.0, 0.5, 1.0, source="ai")
    assert d.ly == 0.0 and d.rx != 0.0


def test_talk_gate():
    sf = SafetyFloor()
    assert not sf.check_say(settings(talk_enabled=False)).allowed
    assert sf.check_say(settings(talk_enabled=True)).allowed


def test_estop_latch_blocks_every_source():
    sf = _sf()
    sf.estop_latch()
    s = settings(autonomy="auto", allow_motion=True, max_speed=1.0)
    for src in ("ai", "recovery", "manual", "overseer"):
        d = sf.check_drive(s, 0.5, 0.0, 1.0, source=src)
        assert not d.allowed and d.reason == "estop_latched"


def test_estop_reset_permits_motion_again():
    sf = _sf()
    sf.estop_latch()
    sf.arb._unsafe_clear_for_tests()   # no-sidecar test release (production uses the reconciled CAS)
    d = sf.check_drive(settings(autonomy="auto", allow_motion=True, max_speed=0.6), 0.5, 0.0, 1.0, source="manual")
    assert d.allowed


def test_estop_latch_bumps_control_generation():
    sf = _sf()
    g1 = sf.estop_latch()
    g2 = sf.estop_latch()
    assert g2 > g1 and sf.control_generation() == g2
