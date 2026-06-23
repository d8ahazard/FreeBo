# eboproto — vendored copy

This directory is a **vendored copy** of the protocol codec maintained at
`ebo-se-lan-bridge/vendor/lib/_re/eboproto/` (the reverse-engineering handoff). It is the portable,
dependency-free C source-of-truth for the Enabot EBO control protocol:

- LAN MAVLink builders — **byte-identical** to `autobot/robot/frames.py` (proven by `eboproto_selftest.c`).
- `avSendIOCtrl` stream-control + bridge pipe framing + inbound MAVLink status parsing.
- The cloud **RTM JSON** control plane (drive/dock/avoid/laser/shoot/move/emote) for Air 2 / EBO Max.
- Per-variant routing (`ebo_build` / `ebo_route`).

## Why a copy

The `_re/` tree is gitignored and owned by the collector/RE work. We keep an in-app copy so FreeBo can
build/ship it independently. **Do not edit the `_re/` original from here**; if the protocol changes there,
re-copy the five files (`eboproto.{h,c}`, `eboproto_selftest.c`, `Makefile`, `README.md`).

## How FreeBo uses it

- `autobot/robot/proto.py` prefers a ctypes-loaded `libeboproto` shared lib when present, and otherwise
  falls back to the pure-Python `frames.py` MAVLink builders + Python RTM JSON builders.
- `scripts/eboproto_check.py` builds + runs `eboproto_selftest` (when a C compiler is available) as the
  gate that asserts `eboproto` stays byte-identical to `frames.py`.
- `scripts/bootstrap.py` will `make shared` here (best-effort) to produce `libeboproto` for the ctypes path.

## Follow-up (not done yet)

Compiling `eboproto` directly into `autobot/robot/native/ebo_bridge.c` (replacing its hand-rolled framing)
is a future cleanup. It is safe to defer because `frames.py` already emits identical bytes, so the running
bridge is unaffected.
