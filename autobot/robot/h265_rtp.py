"""H265 / HEVC RTP depacketizer (RFC 7798) -> Annex-B NAL stream for decoding with PyAV.

aiortc ships VP8/H264 depacketizers but not HEVC, and the EBO Air 2 publishes H265 (payload type 49). This
turns a sequence of RTP H265 payloads (single NAL, Aggregation Packets, and Fragmentation Units) into
Annex-B (start-code-prefixed) access units we can feed to an `av` HEVC decoder.

H265 RTP payload header (2 bytes):
  bit 0      forbidden_zero_bit (must be 0)
  bits 1-6   nal_unit_type (0..63)
  bits 7-12  nuh_layer_id
  bits 13-15 nuh_temporal_id_plus1
nal_unit_type values used by the RTP payload format:
  48 = Aggregation Packet (AP)
  49 = Fragmentation Unit (FU)
  50 = PACI (not handled; rare)
  <48 = single NAL unit (the H265 NAL itself)
"""
from __future__ import annotations

import struct

START = b"\x00\x00\x00\x01"


class H265Depacketizer:
    def __init__(self):
        self._fu_buf = bytearray()
        self._fu_started = False

    def feed(self, payload: bytes) -> list[bytes]:
        """Feed one RTP payload (after the 12-byte RTP header is stripped). Returns a list of complete
        Annex-B NAL units (each already start-code-prefixed). May return 0..N."""
        out: list[bytes] = []
        if len(payload) < 2:
            return out
        nal_type = (payload[0] >> 1) & 0x3F

        if nal_type == 48:  # Aggregation Packet: [hdr2][ (len16)(nal) ]*
            i = 2
            while i + 2 <= len(payload):
                size = struct.unpack(">H", payload[i:i + 2])[0]
                i += 2
                if i + size > len(payload):
                    break
                out.append(START + payload[i:i + size])
                i += size
        elif nal_type == 49:  # Fragmentation Unit: [hdr2][fu_header][frag...]
            if len(payload) < 3:
                return out
            fu_header = payload[2]
            start = bool(fu_header & 0x80)
            end = bool(fu_header & 0x40)
            fu_nal_type = fu_header & 0x3F
            if start:
                # rebuild the NAL header (2 bytes) with the real type from the FU header
                hdr0 = (payload[0] & 0x81) | (fu_nal_type << 1)
                self._fu_buf = bytearray([hdr0, payload[1]])
                self._fu_started = True
            if self._fu_started:
                self._fu_buf += payload[3:]
            if end and self._fu_started:
                out.append(START + bytes(self._fu_buf))
                self._fu_buf = bytearray()
                self._fu_started = False
        else:  # single NAL unit
            out.append(START + payload)
        return out
