import zipfile, os

HERE = os.path.dirname(os.path.abspath(__file__))


def check(path, dex_with_inject, abis):
    z = zipfile.ZipFile(path)
    n = set(z.namelist())
    print("===", os.path.basename(path), "===")
    for abi in abis:
        ok = all(("lib/" + abi + "/" + f) in n for f in ["libgadget.so", "libgadget.config.so", "libhook.so"])
        print("  ", abi, "gadget+config+hook:", "OK" if ok else "MISSING")
    print("   gadget string in", dex_with_inject + ":", b"gadget" in z.read(dex_with_inject))
    hk = [p for p in n if p.endswith("/libhook.so")][0]
    h = z.read(hk)
    print('   host=auto:', (b'AUTOBOT_HOST_BAKED = "auto"' in h), " mdns name:", (b"autobot.local" in h))


check(os.path.join(HERE, "rola-autobot-arm64.apk"), "classes.dex", ["arm64-v8a", "armeabi-v7a"])
check(os.path.join(HERE, "ebohome-autobot-arm64.apk"), "classes2.dex", ["arm64-v8a"])
