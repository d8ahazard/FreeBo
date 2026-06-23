"""Video pipeline helpers for the native link: mediamtx config + ffmpeg/snapshot commands + the WHEP/HLS
upstream metadata the web server proxies to. The lifecycle (spawning/restarting the processes) lives in
NativeRobotLink; this module holds the pure, stateless pieces. Reused from the upstream ebo-se-lan-bridge.
"""
from __future__ import annotations

import base64
import os
from pathlib import Path

RTSP_PATH = "ebo"

# Stream auth for mediamtx (RTSP/WebRTC/HLS). On the trusted LAN this is optional.
STREAM_USER = os.environ.get("EBO_STREAM_USER", "ebo")
STREAM_PASS = os.environ.get("EBO_STREAM_PASS", "")

# Local mediamtx upstreams (same box).
WHEP_UPSTREAM = f"http://127.0.0.1:8889/{RTSP_PATH}/whep"
HLS_BASE = "http://127.0.0.1:8888"


def rtsp_url(host: str = "127.0.0.1") -> str:
    auth = f"{STREAM_USER}:{STREAM_PASS}@" if STREAM_PASS else ""
    return f"rtsp://{auth}{host}:8554/{RTSP_PATH}"


def stream_auth_header() -> dict[str, str]:
    """Basic-auth header for mediamtx, so creds never appear in a browser URL. Empty if no password set."""
    if not STREAM_PASS:
        return {}
    tok = base64.b64encode(f"{STREAM_USER}:{STREAM_PASS}".encode()).decode()
    return {"Authorization": "Basic " + tok}


def render_mediamtx_config(template_path: str, out_path: str) -> None:
    """Substitute the stream credentials into the mediamtx config template."""
    tpl = Path(template_path).read_text(encoding="utf-8")
    cfg = tpl.replace("__STREAM_USER__", STREAM_USER).replace("__STREAM_PASS__", STREAM_PASS)
    Path(out_path).write_text(cfg, encoding="utf-8")


def has_param_set(data: bytes, codec: int) -> bool:
    """True if an Annex-B frame carries codec parameter sets (clean start point for ffmpeg):
    HEVC VPS(32)/SPS(33), H.264 SPS(7)."""
    i, n = 0, len(data)
    while i + 4 < n:
        if data[i] == 0 and data[i + 1] == 0 and (data[i + 2] == 1 or (data[i + 2] == 0 and i + 3 < n and data[i + 3] == 1)):
            j = i + (3 if data[i + 2] == 1 else 4)
            if j < n:
                if codec == 80:                       # HEVC
                    t = (data[j] >> 1) & 0x3F
                    if t == 32 or t == 33:
                        return True
                else:                                 # H.264
                    if (data[j] & 0x1F) == 7:
                        return True
            i = j
        else:
            i += 1
    return False
