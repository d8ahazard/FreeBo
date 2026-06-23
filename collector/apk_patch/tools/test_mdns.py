"""Validate the mDNS responder + the exact wire format agent.js uses (QU query -> unicast A reply)."""
import socket, struct, threading, time, sys, os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", ".."))
from collector import receiver  # noqa: E402

threading.Thread(target=receiver._mdns_responder, daemon=True).start()
time.sleep(0.5)


def build_query(host):
    b = bytearray([0, 0, 0, 0, 0, 1, 0, 0, 0, 0, 0, 0])
    for lbl in host.split("."):
        b.append(len(lbl))
        b += lbl.encode()
    b.append(0)
    b += bytes([0, 1, 0x80, 1])  # A, IN|unicast
    return bytes(b)


def skip_name(d, off):
    while True:
        ln = d[off]
        if ln == 0:
            return off + 1
        if ln & 0xC0:
            return off + 2
        off += 1 + ln


def parse_a(d):
    qd = struct.unpack_from(">H", d, 4)[0]
    an = struct.unpack_from(">H", d, 6)[0]
    off = 12
    for _ in range(qd):
        off = skip_name(d, off) + 4
    for _ in range(an):
        off = skip_name(d, off)
        typ, _cls, _ttl, rdlen = struct.unpack_from(">HHIH", d, off)
        off += 10
        if typ == 1 and rdlen == 4:
            return ".".join(str(x) for x in d[off:off + 4])
        off += rdlen
    return None


s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
s.setblocking(False)
s.sendto(build_query(receiver.MDNS_NAME), (receiver.MDNS_ADDR, receiver.MDNS_PORT))
ip = None
for _ in range(25):
    try:
        data, addr = s.recvfrom(2048)
        ip = parse_a(data)
        if ip:
            break
    except BlockingIOError:
        time.sleep(0.03)
s.close()
print("RESOLVED:", ip)
sys.exit(0 if ip else 1)
