"""The in-process robot-link contract — the ONLY interface through which the brain touches the robot.

This replaces the old HTTP `BridgeClient`: instead of the brain POSTing to a separate Pi process, it calls
these methods in-process. Two implementations exist (NativeRobotLink, MockRobotLink) chosen by
`Settings.robot_link`. Every method fails soft (returns a result dict with ok=False rather than raising) so
the agent loop and UI keep running and the robot stays stopped on error. See docs/BRIDGE_PROTOCOL.md.
"""
from __future__ import annotations

import abc
import time
from typing import TYPE_CHECKING, Any

from ..config import Settings

if TYPE_CHECKING:
    from .media_hub import FrameSample


class RobotLink(abc.ABC):
    """Abstract robot link. Methods mirror the verbs the brain needs; results are JSON-able dicts.

    Video is exposed as upstream metadata (the web server proxies WHEP/HLS to a local mediamtx) rather than
    streamed through this interface. `whep_upstream`/`hls_base` are None when there is no video (mock mode).
    """

    # Enabot model/variant — set by make_link() from settings.robot_variant. Drives control routing.
    variant: str = "SE"

    def channel_for(self, action: str) -> str:
        """Which transport channel a control action uses for this robot's variant (proto.route). SE/AIR use
        LAN MAVLink; AIR2/PRO route motion over the cloud RTM plane (transport pending — see feat-rtm)."""
        from . import proto
        return proto.route(self.variant, action)

    # --- lifecycle ---
    def start(self) -> None:
        """Start any subprocesses/threads. Safe to call once. Default: no-op."""

    def close(self) -> None:
        """Tear down subprocesses/threads. Default: no-op."""

    # --- read ---
    @abc.abstractmethod
    async def info(self) -> dict[str, Any]: ...

    @abc.abstractmethod
    async def telemetry(self) -> dict[str, Any]: ...

    @abc.abstractmethod
    async def snapshot(self) -> tuple[bytes | None, str | None]:
        """Returns (jpeg_bytes, error). (None, reason) when asleep / not ready."""

    async def snapshot_sample(self) -> "FrameSample":
        """Sequence-aware snapshot for motion evidence. Default wraps `snapshot()` with `seq=None` (this link
        can't prove frame freshness, so the executor must treat its evidence as UNKNOWN, never confident).
        Hub-backed links (Air 2 native) override this to return real sequence numbers + timestamps."""
        from .media_hub import FrameSample
        jpeg, err = await self.snapshot()
        return FrameSample(jpeg=jpeg, seq=None, wall_ts=time.monotonic(), age=0.0,
                           valid=jpeg is not None, error=err)

    # --- control ---
    @abc.abstractmethod
    async def drive(self, ly: float, rx: float, *, generation: int | None = None,
                    epoch: int | None = None) -> dict[str, Any]: ...

    @abc.abstractmethod
    async def move(self, ly: float, rx: float, duration: float, *, generation: int | None = None,
                   epoch: int | None = None) -> dict[str, Any]: ...

    @abc.abstractmethod
    async def stop(self) -> dict[str, Any]: ...

    async def estop(self, generation: int | None = None, epoch: int | None = None) -> dict[str, Any]:
        """Hard emergency stop at the link layer. Default = a normal stop; links with a sustained-drive
        sidecar (Air 2 native) override this to LATCH + slam a zero-frame burst so no in-flight drive resumes.
        `generation`+`epoch` are the authoritative control transition (P0 §2) the link/sidecar should adopt.
        Every implementation MUST accept them (no TypeError-fallback contract)."""
        return await self.stop()

    async def estop_reset(self, *, expected_epoch: int | None = None, expected_generation: int | None = None,
                          release_epoch: int | None = None,
                          release_generation: int | None = None) -> dict[str, Any]:
        """Reconcile a link-level E-STOP latch via the prepared two-phase release (agent_next_2 §2). Default
        (links with no sustained-drive sidecar) reconcile trivially to the reserved release state. Air 2 overrides
        this to run sidecar prepare_reset -> commit_reset and reports honest reconciliation evidence (NOT an SDK
        send). The process clears its own latch only after this returns reconciled + the reserved release state."""
        return {"ok": True, "reconciled": True, "control_ready": True, "latched": False,
                "epoch": release_epoch, "generation": release_generation}

    @abc.abstractmethod
    async def action(self, name: str) -> dict[str, Any]: ...

    @abc.abstractmethod
    async def connection(self, state: str) -> dict[str, Any]: ...

    @abc.abstractmethod
    async def say_audio(self, g711: bytes, codec: str = "mulaw") -> dict[str, Any]: ...

    @abc.abstractmethod
    async def say_text(self, text: str) -> dict[str, Any]: ...

    def prefers_text_tts(self) -> bool:
        """If True, the brain's `say` hands TEXT to this link (say_text) instead of rendering G.711 itself.
        The Air 2 cloud link sets this — it speaks via the browser publishing TTS audio into the Agora call
        (the robot's own speaker), not the local G.711/talkback path."""
        return False

    # --- audio in (for the voice/STT skill); default: no audio available ---
    def set_audio_sink(self, callback) -> None:
        """Register a callback(mulaw_bytes) for inbound robot mic audio. Default: no-op (no audio)."""
        raise NotImplementedError("no audio on this link")

    # --- video upstreams (proxied by the web server; None when there is no video) ---
    @property
    def whep_upstream(self) -> str | None:
        return None

    @property
    def hls_base(self) -> str | None:
        return None

    def stream_auth_header(self) -> dict[str, str]:
        return {}


def make_link(settings: Settings) -> RobotLink:
    """Pick the link implementation from settings.robot_link. Imports are lazy so mock-mode dev on a PC
    never needs the native modules / robot secrets.

    - mock:       hardware-free fake (any PC).
    - native:     real robot via the bionic TUTK bridge (Pi/ARM Linux).
    - native_x86: real robot via TUTK ctypes on x86/Windows (transport UNTESTED — see x86_link.py).
    """
    if settings.robot_link == "mock":
        from .mock_link import MockRobotLink
        link: RobotLink = MockRobotLink()
    elif settings.robot_link == "air2":
        from .air2_link import Air2BridgeLink
        link = Air2BridgeLink()
    elif settings.robot_link == "air2_native":
        from .air2_native_link import Air2NativeLink
        link = Air2NativeLink()
    elif settings.robot_link == "native_x86":
        from .x86_link import X86RobotLink
        link = X86RobotLink()
    else:
        from .native_link import NativeRobotLink
        link = NativeRobotLink()
    link.variant = (settings.robot_variant or "SE").upper()
    return link
