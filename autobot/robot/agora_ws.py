"""Minimal async WebSocket client over raw TLS — the Agora edge gateway 101-handshakes fine with standard
headers but the `websockets` library hung on it, so we control the socket directly. Client->server frames are
masked per RFC 6455; server->client are not. Enough for Agora's text(JSON)+binary signaling."""
from __future__ import annotations

import asyncio
import base64
import os
import ssl
import struct


class AgoraWS:
    def __init__(self, reader, writer):
        self._r = reader
        self._w = writer

    @classmethod
    async def connect(cls, host: str, port: int, path: str = "/", origin: str = "https://localhost") -> "AgoraWS":
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        reader, writer = await asyncio.open_connection(host, port, ssl=ctx, server_hostname=host)
        key = base64.b64encode(os.urandom(16)).decode()
        req = (f"GET {path} HTTP/1.1\r\nHost: {host}:{port}\r\nUpgrade: websocket\r\n"
               f"Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n"
               f"Origin: {origin}\r\n\r\n")
        writer.write(req.encode()); await writer.drain()
        # read HTTP response headers until blank line
        hdr = b""
        while b"\r\n\r\n" not in hdr:
            chunk = await reader.read(1)
            if not chunk:
                raise ConnectionError("gateway closed during handshake")
            hdr += chunk
        if b"101" not in hdr.split(b"\r\n")[0]:
            raise ConnectionError("ws upgrade failed: " + hdr[:120].decode("latin1"))
        return cls(reader, writer)

    async def _read_exactly(self, n: int) -> bytes:
        return await self._r.readexactly(n) if n else b""

    async def recv(self):
        """Return (opcode, payload bytes). Handles fragmentation + control frames minimally."""
        data = bytearray()
        opcode0 = None
        while True:
            b0, b1 = await self._read_exactly(2)
            fin = b0 & 0x80
            opcode = b0 & 0x0F
            masked = b1 & 0x80
            ln = b1 & 0x7F
            if ln == 126:
                ln = struct.unpack(">H", await self._read_exactly(2))[0]
            elif ln == 127:
                ln = struct.unpack(">Q", await self._read_exactly(8))[0]
            mask = await self._read_exactly(4) if masked else b""
            payload = await self._read_exactly(ln)
            if masked:
                payload = bytes(payload[i] ^ mask[i % 4] for i in range(ln))
            if opcode == 0x8:  # close
                raise ConnectionError("ws closed by server")
            if opcode == 0x9:  # ping -> pong
                await self._send_frame(0xA, payload); continue
            if opcode == 0xA:  # pong
                continue
            if opcode0 is None:
                opcode0 = opcode
            data += payload
            if fin:
                return opcode0, bytes(data)

    async def _send_frame(self, opcode: int, payload: bytes):
        b0 = 0x80 | opcode
        ln = len(payload)
        header = bytearray([b0])
        mask = os.urandom(4)
        if ln < 126:
            header.append(0x80 | ln)
        elif ln < 65536:
            header.append(0x80 | 126); header += struct.pack(">H", ln)
        else:
            header.append(0x80 | 127); header += struct.pack(">Q", ln)
        header += mask
        masked = bytes(payload[i] ^ mask[i % 4] for i in range(ln))
        self._w.write(bytes(header) + masked)
        await self._w.drain()

    async def send_text(self, s: str):
        await self._send_frame(0x1, s.encode("utf-8"))

    async def send_bytes(self, b: bytes):
        await self._send_frame(0x2, b)

    async def close(self):
        try:
            self._w.close()
            await self._w.wait_closed()
        except Exception:  # noqa: BLE001
            pass
