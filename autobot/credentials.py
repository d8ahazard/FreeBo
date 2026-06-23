"""Robot secrets loader — the ONLY place the device credentials live in the app.

These are device-specific and captured once by the collector. They are read from the environment (which
the container/run.sh populates from `.env`) plus the on-disk `ioctl9930.bin`. Hard rule (see
.cursor/rules/30-safety.mdc and 40-secrets-and-collector.mdc):

  - never log full values (use `masked()`),
  - never serialize them to the browser/UI,
  - never send them to the AI provider.

Only `autobot.robot.native_link` consumes these, to start the native `ebo_bridge` process.
"""
from __future__ import annotations

import os
import platform
from dataclasses import dataclass, field
from pathlib import Path

# Where the native runtime + vendor blobs live (one app, one tree).
EBO_DIR = os.environ.get("EBO_DIR", "/opt/ebo")

_REQUIRED = ("license", "uid", "authkey", "identity", "token")

# Candidate TUTK shared-library filenames per platform. The native (Pi) bridge dlopen's the 4 separate ARM
# .so by name; the x86/Windows ctypes path (x86_link.py) loads either the combined Wyze lib or the 4-lib set.
def tutk_lib_candidates() -> list[str]:
    sysname = platform.system()
    if sysname == "Windows":
        return ["libIOTCAPIs_ALL.dll", "IOTCAPIs_ALL.dll", "libIOTCAPIs.dll", "libTUTKGlobalAPIs.dll"]
    if sysname == "Darwin":
        return ["libIOTCAPIs_ALL.dylib", "libIOTCAPIs.dylib"]
    return ["libIOTCAPIs_ALL.so", "libIOTCAPIs.so", "libTUTKGlobalAPIs.so"]


def _mask(v: str | None) -> str:
    if not v:
        return ""
    return "***" if len(v) <= 6 else f"{v[:2]}…{v[-2:]}"


@dataclass
class Credentials:
    license: str = ""
    uid: str = ""
    authkey: str = ""
    identity: str = ""
    token: str = ""
    ioctl9930_path: str = ""
    lib_dir: str = ""
    bionic_dir: str = ""
    ebo_dir: str = EBO_DIR
    missing: list[str] = field(default_factory=list)

    @property
    def present(self) -> bool:
        return not self.missing

    def tutk_lib_path(self) -> str | None:
        """Resolve the TUTK transport library for the x86/Windows ctypes link (x86_link.py). Looks in
        `lib_dir` for a platform-appropriate filename. Returns None if none is present."""
        d = Path(self.lib_dir)
        for name in tutk_lib_candidates():
            p = d / name
            if p.is_file():
                return str(p)
        return None

    def masked(self) -> dict:
        """Diagnostics only — safe to log/return. Never exposes full secret values."""
        return {
            "license": _mask(self.license),
            "uid": _mask(self.uid),
            "authkey": _mask(self.authkey),
            "identity": _mask(self.identity),
            "token": _mask(self.token),
            "ioctl9930": bool(self.ioctl9930_path and Path(self.ioctl9930_path).exists()),
            "present": self.present,
            "missing": list(self.missing),
        }


def load_credentials() -> Credentials:
    """Read the robot secrets from the environment + vendor tree. Fails soft: a missing set is reported
    via `missing`/`present`, not raised, so mock mode and diagnostics still work."""
    ebo_dir = os.environ.get("EBO_DIR", EBO_DIR)
    c = Credentials(
        license=os.environ.get("EBO_LICENSE", ""),
        uid=os.environ.get("EBO_UID", ""),
        authkey=os.environ.get("EBO_AUTHKEY", ""),
        identity=os.environ.get("EBO_IDENTITY", ""),
        token=os.environ.get("EBO_TOKEN", ""),
        ioctl9930_path=os.environ.get("EBO_IOCTL9930", os.path.join(ebo_dir, "ioctl9930.bin")),
        lib_dir=os.environ.get("EBO_LIB_DIR", os.path.join(ebo_dir, "lib")),
        bionic_dir=os.environ.get("EBO_BIONIC_DIR", os.path.join(ebo_dir, "bionic")),
        ebo_dir=ebo_dir,
    )
    c.missing = [name.upper() for name in _REQUIRED if not getattr(c, name)]
    return c
