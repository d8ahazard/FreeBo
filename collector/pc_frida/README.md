# pc_frida — PC credential capture (fallback path)

Attaches Frida to the ROLA app running on a **USB-connected (rooted) phone** or an **Android emulator**, and
captures your robot credentials using the same hook as the phone path. Easier to debug than APK patching.
See [../../docs/COLLECTOR.md](../../docs/COLLECTOR.md).

## Requirements

- `pip install frida frida-tools`
- A target running `frida-server`:
  - a rooted Android phone over USB, or
  - an Android emulator (e.g. Android Studio AVD, or Genymotion) with ROLA installed.
- Push and start the matching `frida-server` on the target (see https://frida.re/docs/android/).

## Use

```bash
# 1) start the receiver on this PC
python ../receiver.py --port 8400

# 2) find the ROLA package id
frida-ps -Uai

# 3) attach (or --spawn to launch it) and load the hook
python run.py --package com.example.rola --host 127.0.0.1 --port 8400 --spawn

# 4) in the app, connect to your EBO once — the receiver writes the top-level .env + ioctl9930.bin
```

`run.py` substitutes your receiver host/port into `../hooks/agent.js`, loads it, and also prints masked
Frida `send()` messages so you can see captures live. The hook POSTs to the receiver directly, so the
receiver is the source of truth for what was written.
