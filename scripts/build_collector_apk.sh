#!/usr/bin/env bash
# Build the patched credential-collector app from the OFFICIAL Enabot app (downloads nothing proprietary into
# git — this fetches the public app + open-source tools at build time, injects the Frida gadget + our
# hooks/agent.js, and signs it). This is the reproducible alternative to grabbing the prebuilt APK from a
# release. Needs: a JDK (java), python3, curl, and xz. Output: collector/apk_patch/build/signed/*.apk
#
# Reference (manual steps): collector/apk_patch/README.md. Override any URL/version via env vars below.
set -euo pipefail
cd "$(dirname "$0")/.."

PATCH_DIR="collector/apk_patch"
BUILD="$PATCH_DIR/build"
TOOLS="$BUILD/tools"
mkdir -p "$TOOLS" "$BUILD"

# --- sources (all public; pin/override as needed) ---
OFFICIAL_APK_URL="${FREEBO_EBO_APK_URL:-https://mediakit.enabot.com/ebo/apk-release/prod_app_google_release_latest.apk}"
APKEDITOR_URL="${APKEDITOR_URL:-https://github.com/REAndroid/APKEditor/releases/download/V1.4.1/APKEditor-1.4.1.jar}"
UBERSIGNER_URL="${UBERSIGNER_URL:-https://github.com/patrickfav/uber-apk-signer/releases/download/v1.3.0/uber-apk-signer-1.3.0.jar}"
GADGET_VER="${FRIDA_GADGET_VER:-16.5.6}"
GADGET_ARM64_URL="https://github.com/frida/frida/releases/download/${GADGET_VER}/frida-gadget-${GADGET_VER}-android-arm64.so.xz"

fetch() { local url="$1" out="$2"; [[ -f "$out" ]] && { echo "[have] $out"; return; }; echo "[get ] $out"; curl -fL "$url" -o "$out"; }

echo "== 1/6 download official app + tools =="
fetch "$OFFICIAL_APK_URL" "$BUILD/official.apk"
fetch "$APKEDITOR_URL"    "$TOOLS/APKEditor.jar"
fetch "$UBERSIGNER_URL"   "$TOOLS/uber-apk-signer.jar"
if [[ ! -f "$TOOLS/libgadget-arm64.so" ]]; then
  fetch "$GADGET_ARM64_URL" "$TOOLS/gadget-arm64.so.xz"; xz -dk -f "$TOOLS/gadget-arm64.so.xz"
  mv -f "$TOOLS/gadget-arm64.so" "$TOOLS/libgadget-arm64.so"
fi

echo "== 2/6 merge (if split) -> universal apk =="
# An official single .apk needs no merge; APKEditor 'm' is harmless on a single apk too.
java -jar "$TOOLS/APKEditor.jar" m -i "$BUILD/official.apk" -o "$BUILD/merged.apk" -f || cp -f "$BUILD/official.apk" "$BUILD/merged.apk"

echo "== 3/6 decode -> smali + resources =="
java -jar "$TOOLS/APKEditor.jar" d -i "$BUILD/merged.apk" -o "$BUILD/decode" -f

echo "== 4/6 inject System.loadLibrary(\"gadget\") + set extractNativeLibs (see README for the smali target) =="
echo "    NOTE: edit build/decode/.../EBOApplication.smali (ROLA) or App.smali (EBO HOME) <clinit> per"
echo "    collector/apk_patch/README.md, and set android:extractNativeLibs=\"true\" in AndroidManifest.xml."

echo "== 5/6 drop gadget + rendered hook into every lib/<abi>/ =="
cp -f "$TOOLS/libgadget-arm64.so" "$BUILD/" 2>/dev/null || true
python3 "$PATCH_DIR/tools/place_gadget.py" "$BUILD/decode"

echo "== 6/6 rebuild + sign =="
java -jar "$TOOLS/APKEditor.jar" b -i "$BUILD/decode" -o "$BUILD/patched-unsigned.apk" -f
java -jar "$TOOLS/uber-apk-signer.jar" --apks "$BUILD/patched-unsigned.apk" -o "$BUILD/signed"

echo "[done] signed collector app -> $BUILD/signed/  (sideload it, capture once, then UNINSTALL it)"
