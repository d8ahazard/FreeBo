"""Place Frida gadget + config + rendered hook into a decoded APK tree (one or more ABI dirs).

Usage: python place_gadget.py <decode_dir> [host]
  host defaults to "auto" (mDNS discovery; no per-user IP baked in).
"""
import os, sys, shutil, json

HERE = os.path.dirname(os.path.abspath(__file__))
TOOLS = os.path.join(HERE, "tools")
HOOKS_DIR = os.path.join(HERE, "..", "..", "hooks")
HOOK_SRC = os.path.join(HOOKS_DIR, "agent.js")
# Extra hook scripts concatenated into the same libhook.so (each is a self-contained IIFE). agora_rtm.js logs
# the app's RTM control messages to logcat (AUTOBOT_RTM) for the drive/turn re-sniff; agora_rtc.js logs the
# talkback publish path (AUTOBOT_RTC). Set AUTOBOT_HOOK_EXTRAS to override the comma-separated list.
HOOK_EXTRAS = [h.strip() for h in os.environ.get("AUTOBOT_HOOK_EXTRAS", "agora_rtm.js").split(",") if h.strip()]
PORT = "8400"

GADGET = {
    "arm64-v8a": os.path.join(TOOLS, "libgadget-arm64.so"),
    "armeabi-v7a": os.path.join(TOOLS, "libgadget.so"),
}


def main():
    decode_dir = sys.argv[1]
    host = sys.argv[2] if len(sys.argv) > 2 else "auto"

    hook = open(HOOK_SRC, "r", encoding="utf-8").read()
    hook = hook.replace("__AUTOBOT_HOST__", host).replace("__AUTOBOT_PORT__", PORT)
    # Append the extra capture hooks (RTM control / RTC talkback) so one libhook.so does creds + sniffing.
    for extra in HOOK_EXTRAS:
        p = os.path.join(HOOKS_DIR, extra)
        if os.path.isfile(p):
            hook += "\n\n// ---- " + extra + " ----\n" + open(p, "r", encoding="utf-8").read()
            print(f"appended hook: {extra}")
        else:
            print(f"WARNING: extra hook not found: {extra}")
    cfg = json.dumps(
        {"interaction": {"type": "script", "path": "libhook.so", "on_change": "reload"}}, indent=2
    ) + "\n"

    root_lib = os.path.join(decode_dir, "root", "lib")
    placed = 0
    for abi in os.listdir(root_lib):
        libdir = os.path.join(root_lib, abi)
        if not os.path.isdir(libdir) or abi not in GADGET:
            continue
        open(os.path.join(libdir, "libhook.so"), "w", encoding="utf-8", newline="\n").write(hook)
        open(os.path.join(libdir, "libgadget.config.so"), "w", encoding="utf-8", newline="\n").write(cfg)
        shutil.copyfile(GADGET[abi], os.path.join(libdir, "libgadget.so"))
        print(f"{abi}: placed gadget ({os.path.getsize(GADGET[abi])} B) + config + hook")
        placed += 1
    print(f"host={host}:{PORT}  abis_patched={placed}")
    if not placed:
        sys.exit("ERROR: no matching ABI dirs found under " + root_lib)


if __name__ == "__main__":
    main()
