#!/usr/bin/env bash
# Build the Autobot Pi 4 image with pi-gen, from Windows via WSL2 (Debian) + Docker.
#
# Prereqs (one-time, inside your WSL2 Debian):
#   sudo apt-get update && sudo apt-get install -y git docker.io qemu-user-static binfmt-support
#   sudo update-binfmts --enable
#   sudo usermod -aG docker "$USER"   # then restart the shell
#   # IMPORTANT: run this from the WSL2 *Linux* filesystem (e.g. ~/autobot), NOT /mnt/c — NTFS breaks pi-gen.
#
# Also build the web UI first (needs Node, can be done on Windows or in WSL):
#   (cd webui && npm install && npm run build)
#
# Then:  bash deploy/pi-gen/build-wsl.sh
# Output image lands in: <pigen>/deploy/   (path printed at the end)
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "${HERE}/../.." && pwd)"
PIGEN_DIR="${PIGEN_DIR:-${HOME}/pi-gen}"
MEDIAMTX_VERSION="${MEDIAMTX_VERSION:-1.19.0}"

echo "[build] repo: ${REPO_ROOT}"
echo "[build] pi-gen: ${PIGEN_DIR}"

case "${REPO_ROOT}" in
	/mnt/*) echo "[build] WARNING: building from a Windows-mounted path (/mnt/...). pi-gen image extraction"
	        echo "[build]          fails on NTFS. Copy this repo into the WSL2 Linux filesystem first." ;;
esac

if [ ! -d "${REPO_ROOT}/webui/dist" ]; then
	echo "[build] ERROR: webui/dist not found. Build the UI first: (cd webui && npm install && npm run build)"
	exit 1
fi

# 1) get pi-gen
if [ ! -d "${PIGEN_DIR}" ]; then
	git clone --depth=1 https://github.com/RPi-Distro/pi-gen.git "${PIGEN_DIR}"
fi

# 2) WSL2 fixes: don't try to modprobe nbd if it's built into the kernel.
QH="${PIGEN_DIR}/scripts/qcow2_handling"
if [ -f "${QH}" ] && grep -q '^[[:space:]]*modprobe nbd' "${QH}"; then
	sed -i 's/^\([[:space:]]*\)modprobe nbd max_part=16/\1[ -d \/sys\/class\/block\/nbd0 ] || modprobe nbd max_part=16/' "${QH}" || true
fi

# pi-gen's depends + build-docker.sh expect qemu-user-binfmt in the builder image, but the Dockerfile only
# installs qemu-user-static -> "qemu-arm not found". Add qemu-user-binfmt so the in-container binfmt setup
# and the dependency check both work (needed under Docker Desktop / WSL2).
if [ -f "${PIGEN_DIR}/Dockerfile" ] && ! grep -q 'qemu-user-binfmt' "${PIGEN_DIR}/Dockerfile"; then
	sed -i 's/qemu-user-static/qemu-user-static qemu-user-binfmt/' "${PIGEN_DIR}/Dockerfile" || true
fi

# 3) assemble the app payload the stage will copy into the image
PAYLOAD="${HERE}/stage-autobot/00-autobot/files/payload"
rm -rf "${PAYLOAD}"
mkdir -p "${PAYLOAD}"
cp -r "${REPO_ROOT}/autobot"          "${PAYLOAD}/autobot"
cp    "${REPO_ROOT}/requirements.txt" "${PAYLOAD}/requirements.txt"
cp -r "${REPO_ROOT}/webui/dist"       "${PAYLOAD}/webui-dist"
if [ -d "${REPO_ROOT}/collector/bionic" ]; then
	# only the runtime libs, not docs/scripts
	mkdir -p "${PAYLOAD}/bionic"
	find "${REPO_ROOT}/collector/bionic" -maxdepth 1 -type f \( -name 'linker' -o -name '*.so' \) \
		-exec cp {} "${PAYLOAD}/bionic/" \; 2>/dev/null || true
fi
# drop pycache to keep it lean
find "${PAYLOAD}/autobot" -name '__pycache__' -type d -prune -exec rm -rf {} + 2>/dev/null || true

# 4) install our config + stage into the pi-gen checkout
cp "${HERE}/config" "${PIGEN_DIR}/config"
rm -rf "${PIGEN_DIR}/stage-autobot"
cp -r "${HERE}/stage-autobot" "${PIGEN_DIR}/stage-autobot"
chmod +x "${PIGEN_DIR}/stage-autobot/prerun.sh" "${PIGEN_DIR}/stage-autobot/00-autobot/"*.sh 2>/dev/null || true
# only export our final image, not the intermediate Lite one
touch "${PIGEN_DIR}/stage2/SKIP_IMAGES"

# 5) build
cd "${PIGEN_DIR}"
MEDIAMTX_VERSION="${MEDIAMTX_VERSION}" ./build-docker.sh

echo
echo "[build] Done. Image(s) in: ${PIGEN_DIR}/deploy/"
ls -lh "${PIGEN_DIR}/deploy/" || true
