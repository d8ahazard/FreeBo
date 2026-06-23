#!/usr/bin/env python3
"""Autobot credential receiver.

Listens on the LAN for the captured values POSTed by the Frida hook (collector/hooks/agent.js), assembles
them, and writes the top-level .env + vendor/ioctl9930.bin (the unified app reads these). Values are masked
in the console. Uses only the Python standard library so it can run anywhere with no extra install.

Run:  python collector/receiver.py [--port 8400]
Then run the phone (patched APK) or PC (pc_frida) capture path; connect to your robot once.
See ../docs/COLLECTOR.md.
"""
from __future__ import annotations

import argparse
import base64
import json
import socket
import struct
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

# mDNS so the patched apps can FIND this receiver with zero per-user config. The hook (agent.js) sends a
# one-shot mDNS A query for MDNS_NAME with the unicast-response bit set; we answer with this PC's LAN IP.
# Pure stdlib — no zeroconf dependency (keeps the collector install-free).
MDNS_ADDR = "224.0.0.251"
MDNS_PORT = 5353
MDNS_NAME = "autobot.local"

REPO_ROOT = Path(__file__).resolve().parents[1]
ENV_PATH = REPO_ROOT / ".env"
VENDOR_DIR = REPO_ROOT / "vendor"

ENV_FIELDS = {
    "license": "EBO_LICENSE",
    "uid": "EBO_UID",
    "authkey": "EBO_AUTHKEY",
    "identity": "EBO_IDENTITY",
    "token": "EBO_TOKEN",
}
REQUIRED = set(ENV_FIELDS) | {"ioctl9930"}

captured: dict[str, str] = {}


def mask(v: str) -> str:
    return "***" if len(v) <= 6 else f"{v[:2]}…{v[-2:]}"


def _read_env() -> dict[str, str]:
    env: dict[str, str] = {}
    if ENV_PATH.exists():
        for line in ENV_PATH.read_text(encoding="utf-8").splitlines():
            if "=" in line and not line.strip().startswith("#"):
                k, _, val = line.partition("=")
                env[k.strip()] = val
    else:
        example = REPO_ROOT / ".env.example"
        if example.exists():
            for line in example.read_text(encoding="utf-8").splitlines():
                if "=" in line and not line.strip().startswith("#"):
                    k, _, val = line.partition("=")
                    env[k.strip()] = val
    return env


def _write_env(updates: dict[str, str]):
    env = _read_env()
    env.update(updates)
    # Preserve a friendly header.
    lines = ["# Autobot credentials — written by collector/receiver.py. Do NOT commit.\n"]
    for k, v in env.items():
        lines.append(f"{k}={v}")
    ENV_PATH.parent.mkdir(parents=True, exist_ok=True)
    ENV_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _persist():
    """Write whatever we have so far; write ioctl9930.bin when present."""
    updates = {ENV_FIELDS[f]: captured[f] for f in ENV_FIELDS if f in captured}
    if updates:
        _write_env(updates)
    if "ioctl9930" in captured:
        VENDOR_DIR.mkdir(parents=True, exist_ok=True)
        (VENDOR_DIR / "ioctl9930.bin").write_bytes(base64.b64decode(captured["ioctl9930"]))
    have = sorted(captured.keys())
    missing = sorted(REQUIRED - set(captured.keys()))
    print(f"[receiver] have: {have}")
    if missing:
        print(f"[receiver] still waiting for: {missing}")
    else:
        print("[receiver] OK: ALL CREDENTIALS CAPTURED.")
        print(f"[receiver]    wrote {ENV_PATH}")
        print(f"[receiver]    wrote {VENDOR_DIR / 'ioctl9930.bin'} ({len(base64.b64decode(captured['ioctl9930']))} bytes)")
        print("[receiver] You can stop the receiver (Ctrl-C) and build the bridge.")


def _local_ip_for(peer_ip: str) -> str | None:
    """Pick the local interface IP that routes toward the querier (so multi-homed PCs answer correctly)."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect((peer_ip, 9))
        return s.getsockname()[0]
    except Exception:  # noqa: BLE001
        return None
    finally:
        s.close()


def _encode_name(name: str) -> bytes:
    out = b"".join(bytes([len(lbl)]) + lbl.encode("ascii") for lbl in name.split(".") if lbl)
    return out + b"\x00"


def _parse_question(data: bytes):
    """Return (qname_lower, qtype) for the first question, or (None, None)."""
    try:
        qd = struct.unpack_from(">H", data, 4)[0]
        if qd < 1:
            return None, None
        off = 12
        labels = []
        while True:
            ln = data[off]
            if ln == 0:
                off += 1
                break
            if ln & 0xC0:  # compression in a question is not expected; bail
                return None, None
            labels.append(data[off + 1: off + 1 + ln].decode("ascii", "ignore"))
            off += 1 + ln
        qtype = struct.unpack_from(">H", data, off)[0]
        return ".".join(labels).lower(), qtype
    except Exception:  # noqa: BLE001
        return None, None


def _build_response(ip: str) -> bytes:
    header = struct.pack(">HHHHHH", 0, 0x8400, 0, 1, 0, 0)  # response + authoritative, 1 answer
    rr = _encode_name(MDNS_NAME)
    rr += struct.pack(">HHIH", 1, 0x8001, 120, 4)  # A, IN|cache-flush, ttl, rdlength
    rr += socket.inet_aton(ip)
    return header + rr


def _mdns_responder():
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)  # not on Windows; ignored if absent
    except (AttributeError, OSError):
        pass
    try:
        sock.bind(("", MDNS_PORT))
        mreq = struct.pack("=4sl", socket.inet_aton(MDNS_ADDR), socket.INADDR_ANY)
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
    except OSError as e:
        print(f"[receiver] mDNS disabled (could not bind :{MDNS_PORT}: {e}). Devices won't auto-discover —")
        print("[receiver] pass the PC IP explicitly when patching (--host) or free up port 5353.")
        return
    print(f"[receiver] mDNS responder up: answering '{MDNS_NAME}' so patched apps auto-find this PC.")
    while True:
        try:
            data, addr = sock.recvfrom(2048)
        except OSError:
            break
        qname, qtype = _parse_question(data)
        if qname != MDNS_NAME or qtype not in (1, 255):  # A or ANY
            continue
        ip = _local_ip_for(addr[0])
        if not ip:
            continue
        try:
            sock.sendto(_build_response(ip), addr)  # unicast straight back (QU bit)
        except OSError:
            pass


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):  # quiet default logging
        pass

    def do_POST(self):
        if self.path != "/capture":
            self.send_response(404); self.end_headers(); return
        try:
            n = int(self.headers.get("Content-Length", "0"))
            body = json.loads(self.rfile.read(n) or b"{}")
            field = str(body.get("field", ""))
            value = str(body.get("value", ""))
        except Exception as e:  # noqa: BLE001
            self.send_response(400); self.end_headers(); self.wfile.write(str(e).encode()); return
        if field and value:
            new = field not in captured
            captured[field] = value
            print(f"[receiver] {'+' if new else '~'} {field} = {mask(value)}")
            _persist()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b'{"ok":true}')

    def do_GET(self):
        # health/status
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps({"have": sorted(captured.keys()),
                                     "missing": sorted(REQUIRED - set(captured.keys()))}).encode())


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=8400)
    ap.add_argument("--no-mdns", action="store_true", help="disable mDNS auto-discovery responder")
    args = ap.parse_args()
    print(f"[receiver] listening on {args.host}:{args.port}")
    print(f"[receiver] will write -> {ENV_PATH} and {VENDOR_DIR / 'ioctl9930.bin'}")
    if not args.no_mdns:
        threading.Thread(target=_mdns_responder, daemon=True).start()
    print("[receiver] Now run the capture path (phone patched APK or pc_frida) and connect to your robot.")
    ThreadingHTTPServer((args.host, args.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
