"""MediaHub — the single fan-out point for the robot's decoded live A/V.

Why this exists: the native Agora receiver (`agora_native.py`) decodes the H265 video and Opus audio ONCE,
then publishes each frame/chunk here. Every interested subsystem subscribes independently and at its own
rate, so the expensive decode never gets duplicated and no consumer can stall another:

    receiver --decode--> MediaHub --> UI (JPEG preview)
                                  --> brain / omni (occasional frames + captions)
                                  --> VSLAM (every frame, with RTP timestamps + keyframe flags + IMU sync)
                                  --> face recognition / sightings
                                  --> STT / omni (audio PCM)

The two carriers below carry exactly what each consumer needs:
  * `VideoFrame` keeps the RTP 90 kHz timestamp and a `keyframe` flag — VSLAM needs monotonic per-frame
    timing to fuse with the 6-axis IMU (visual-inertial odometry) and needs to know which frames are IRAP
    (relocalization anchors). It also carries the raw BGR ndarray so a consumer can go straight to OpenCV.
  * `AudioChunk` carries int16 mono PCM + sample rate so STT/omni can consume without re-decoding.

Dependency-light by design (stdlib only here; numpy/cv2/av are only touched lazily in helpers), so importing
this module is safe even on the dependency-thin Pi build.
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

# Standard RTP video clock. Air 2 video is sampled at this rate; dividing the RTP timestamp by it gives
# seconds, which is what VSLAM/IMU fusion wants for relative frame timing.
VIDEO_CLOCK_HZ = 90000


@dataclass
class VideoFrame:
    """One decoded video frame, with everything a consumer (UI / brain / VSLAM) could need."""
    bgr: Any                       # numpy HxWx3 uint8 (BGR), or None if only raw NALs were kept
    width: int
    height: int
    seq: int                       # monotonic frame index since receiver start
    rtp_ts: int                    # RTP timestamp (90 kHz) of the access unit — for VSLAM/IMU sync
    wall_ts: float                 # local monotonic clock (time.monotonic) when assembled
    keyframe: bool = False         # True if the AU contained an IRAP/IDR NAL (relocalization anchor)
    annexb: Optional[bytes] = None # the raw Annex-B access unit (so a consumer can re-decode/record)

    @property
    def t_seconds(self) -> float:
        """RTP presentation time in seconds (wraps every ~13h at 90 kHz; consumers should diff, not abs)."""
        return self.rtp_ts / VIDEO_CLOCK_HZ

    def gray(self):
        """Grayscale view for feature trackers (ORB/optical-flow). Lazy cv2; returns None if unavailable."""
        if self.bgr is None:
            return None
        try:
            import cv2
            return cv2.cvtColor(self.bgr, cv2.COLOR_BGR2GRAY)
        except Exception:  # noqa: BLE001
            return None

    def to_jpeg(self, quality: int = 70, max_w: int = 0):
        """Encode to JPEG bytes (for the UI / brain). Optionally downscale to `max_w` first. None on failure."""
        if self.bgr is None:
            return None
        try:
            import cv2
            img = self.bgr
            if max_w and self.width > max_w:
                h = int(self.height * max_w / self.width)
                img = cv2.resize(img, (max_w, h), interpolation=cv2.INTER_AREA)
            ok, buf = cv2.imencode(".jpg", img, [int(cv2.IMWRITE_JPEG_QUALITY), quality])
            return buf.tobytes() if ok else None
        except Exception:  # noqa: BLE001
            return None


@dataclass
class FrameSample:
    """An atomic snapshot of the newest decoded frame: its JPEG plus the metadata needed to reason about
    freshness (sequence + monotonic timestamp + age + validity). Motion evidence MUST compare `seq` to decide
    whether a NEW frame arrived after a pulse — `seq is None` means the link can't provide sequence evidence
    (treat as UNKNOWN, never as a confident verdict). All fields come from the SAME captured frame."""
    jpeg: Optional[bytes]
    seq: Optional[int]
    wall_ts: float                 # time.monotonic() when the source frame was assembled (0.0 if none)
    age: float                     # seconds between capture and now (monotonic)
    valid: bool                    # a usable JPEG is present
    error: Optional[str] = None


@dataclass
class AudioChunk:
    """One decoded audio chunk — int16 mono PCM, ready for STT/omni without re-decoding."""
    pcm: bytes                     # signed 16-bit little-endian, mono
    sample_rate: int               # e.g. 48000 (Opus native) or 16000 after resample
    rtp_ts: int
    wall_ts: float = field(default_factory=time.monotonic)

    @property
    def samples(self) -> int:
        return len(self.pcm) // 2

    @property
    def duration(self) -> float:
        return self.samples / self.sample_rate if self.sample_rate else 0.0


# A subscriber is just a callable taking the frame/chunk. Keep them CHEAP (enqueue + return); heavy work
# (SLAM, whisper, omni) must happen on the consumer's own thread/task so it never blocks the decode loop.
VideoSub = Callable[[VideoFrame], None]
AudioSub = Callable[[AudioChunk], None]


class MediaHub:
    """Thread-safe publish/subscribe for decoded media. The receiver publishes; consumers subscribe.

    Consumers attach with `subscribe_video`/`subscribe_audio` and get an unsubscribe handle. Publication is
    fan-out: each subscriber is invoked in turn, guarded so one raising consumer can't break the others or
    the decode loop. `latest_video_jpeg()` is a convenience for the UI (it lazily JPEG-encodes the newest
    frame, downscaled), so the common "just show me the camera" path needs no dedicated subscriber.
    """

    def __init__(self) -> None:
        self._vsubs: list[VideoSub] = []
        self._asubs: list[AudioSub] = []
        self._lock = threading.RLock()
        self._latest: Optional[VideoFrame] = None
        self._latest_jpeg: Optional[bytes] = None
        self._latest_jpeg_seq = -1
        self.video_count = 0
        self.audio_count = 0
        self.last_video_ts = 0.0
        self.last_audio_ts = 0.0

    # --- subscribe ---
    def subscribe_video(self, cb: VideoSub) -> Callable[[], None]:
        with self._lock:
            self._vsubs.append(cb)
        return lambda: self._unsub(self._vsubs, cb)

    def subscribe_audio(self, cb: AudioSub) -> Callable[[], None]:
        with self._lock:
            self._asubs.append(cb)
        return lambda: self._unsub(self._asubs, cb)

    def _unsub(self, lst: list, cb) -> None:
        with self._lock:
            try:
                lst.remove(cb)
            except ValueError:
                pass

    # --- publish (called by the receiver) ---
    def publish_video(self, frame: VideoFrame) -> None:
        with self._lock:
            self._latest = frame
            self.video_count += 1
            self.last_video_ts = frame.wall_ts
            subs = list(self._vsubs)
        for cb in subs:
            try:
                cb(frame)
            except Exception:  # noqa: BLE001 — a bad consumer must never kill the stream
                pass

    def publish_audio(self, chunk: AudioChunk) -> None:
        with self._lock:
            self.audio_count += 1
            self.last_audio_ts = chunk.wall_ts
            subs = list(self._asubs)
        for cb in subs:
            try:
                cb(chunk)
            except Exception:  # noqa: BLE001
                pass

    # --- convenience for the UI / brain ---
    def latest_frame(self) -> Optional[VideoFrame]:
        with self._lock:
            return self._latest

    def latest_video_jpeg(self, max_w: int = 640, quality: int = 70) -> Optional[bytes]:
        with self._lock:
            f = self._latest
            if f is None:
                return None
            if f.seq == self._latest_jpeg_seq and self._latest_jpeg is not None:
                return self._latest_jpeg
        jpeg = f.to_jpeg(quality=quality, max_w=max_w)
        with self._lock:
            self._latest_jpeg = jpeg
            self._latest_jpeg_seq = f.seq
        return jpeg

    def latest_sample(self, max_w: int = 960, quality: int = 75) -> FrameSample:
        """Atomic newest-frame sample for motion evidence. Captures ONE `VideoFrame` reference under the lock,
        then encodes THAT exact frame and reports ITS seq + monotonic wall_ts together — never a separate
        metadata read that could race the encode. Reuses the JPEG cache when the newest frame is unchanged."""
        with self._lock:
            f = self._latest
            cached = self._latest_jpeg if (f is not None and f.seq == self._latest_jpeg_seq) else None
        if f is None:
            return FrameSample(jpeg=None, seq=None, wall_ts=0.0, age=0.0, valid=False, error="no_frame_yet")
        jpeg = cached if cached is not None else f.to_jpeg(quality=quality, max_w=max_w)
        if cached is None and jpeg is not None:
            with self._lock:
                # only update the cache if the newest frame is still the one we just encoded
                if self._latest is f:
                    self._latest_jpeg = jpeg
                    self._latest_jpeg_seq = f.seq
        age = max(0.0, time.monotonic() - (f.wall_ts or time.monotonic()))
        return FrameSample(jpeg=jpeg, seq=f.seq, wall_ts=f.wall_ts, age=age, valid=jpeg is not None,
                           error=None if jpeg is not None else "encode_failed")

    def stats(self) -> dict:
        with self._lock:
            return {"video_count": self.video_count, "audio_count": self.audio_count,
                    "last_video_ts": self.last_video_ts, "last_audio_ts": self.last_audio_ts,
                    "video_subs": len(self._vsubs), "audio_subs": len(self._asubs)}
