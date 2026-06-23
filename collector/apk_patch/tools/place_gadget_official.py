import os, shutil, json

HOST = "auto"  # mDNS discovery; no per-user IP baked in
PORT = "8400"

HERE = os.path.dirname(os.path.abspath(__file__))
root_lib = os.path.join(HERE, "decode-official", "root", "lib")
tools = os.path.join(HERE, "tools")
hook_src = os.path.join(HERE, "..", "..", "hooks", "agent.js")

hook = open(hook_src, "r", encoding="utf-8").read()
hook = hook.replace("__AUTOBOT_HOST__", HOST).replace("__AUTOBOT_PORT__", PORT)

cfg = json.dumps(
    {"interaction": {"type": "script", "path": "libhook.so", "on_change": "reload"}},
    indent=2,
) + "\n"

# (abi dir, gadget source file)
targets = {
    "arm64-v8a": os.path.join(tools, "libgadget-arm64.so"),
    "armeabi-v7a": os.path.join(tools, "libgadget.so"),  # 32-bit arm gadget
}

for abi, gadget in targets.items():
    libdir = os.path.join(root_lib, abi)
    if not os.path.isdir(libdir):
        print("skip missing abi dir:", abi)
        continue
    open(os.path.join(libdir, "libhook.so"), "w", encoding="utf-8", newline="\n").write(hook)
    open(os.path.join(libdir, "libgadget.config.so"), "w", encoding="utf-8", newline="\n").write(cfg)
    shutil.copyfile(gadget, os.path.join(libdir, "libgadget.so"))
    print(abi, "->", [f for f in ("libgadget.so", "libgadget.config.so", "libhook.so")],
          "gadget", os.path.getsize(os.path.join(libdir, "libgadget.so")))

print("receiver baked:", HOST + ":" + PORT)
