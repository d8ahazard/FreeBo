"""Autobot — a single self-hosted app that gives a configurable AI autonomous control of an
Enabot EBO SE robot over the LAN, with a live web UI.

This package merges what used to be the separate Pi "bridge" and PC "brain" into one process.
The only sub-processes are the unavoidable native ones (the TUTK `ebo_bridge` binary under the
bionic linker, plus ffmpeg + mediamtx for video); everything else runs in-process. See AGENTS.md
and docs/ARCHITECTURE.md.
"""

__version__ = "2.0.0"
