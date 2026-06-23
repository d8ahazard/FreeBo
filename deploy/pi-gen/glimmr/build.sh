#!/usr/bin/env bash
# Build the Autobot arm64 Pi image using a PROVEN arm64 pi-gen base (the Glimmr-x64 fork, which already
# solves the Docker-Desktop/WSL2 pain: amd64 debian:bookworm builder + qemu-aarch64 ':F' fix-binary binfmt
# registration + comitup). We layer an Autobot install substage on top and strip the Glimmr-specific bits.
#
# Prereqs in WSL2 (Debian/Ubuntu) with Docker Desktop integration enabled:
#   sudo apt-get install -y qemu-user-static binfmt-support rsync git
#   (build the web UI first: cd webui && npm install && npm run build)
#
# Usage (from the repo root copied onto the WSL Linux filesystem):
#   GLIMMR_PIGEN=/mnt/e/dev/Glimmr/Glimmr-image-gen-x64 bash deploy/pi-gen/glimmr/build.sh
# Output: $IMG_DIR/deploy/*-autobot.img(.xz)
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"            # deploy/pi-gen/glimmr
REPO_ROOT="$(cd "${HERE}/../../.." && pwd)"
GLIMMR_PIGEN="${GLIMMR_PIGEN:-/mnt/e/dev/Glimmr/Glimmr-image-gen-x64}"
IMG_DIR="${IMG_DIR:-${HOME}/autobot-img}"
STATIC="${HERE}/../stage-autobot/00-autobot/files"   # reuse autobot.service/comitup.conf/autobot.env

[ -d "${REPO_ROOT}/webui/dist" ] || { echo "[build] ERROR: build the web UI first (cd webui && npm install && npm run build)"; exit 1; }
command -v qemu-aarch64-static >/dev/null || { echo "[build] ERROR: install qemu-user-static"; exit 1; }
[ -d "${GLIMMR_PIGEN}" ] || { echo "[build] ERROR: arm64 pi-gen base not found at ${GLIMMR_PIGEN} (set GLIMMR_PIGEN)"; exit 1; }

echo "[build] base: ${GLIMMR_PIGEN}"
echo "[build] out:  ${IMG_DIR}"
rm -rf "${IMG_DIR}"
rsync -a --exclude work --exclude deploy --exclude .git "${GLIMMR_PIGEN}/" "${IMG_DIR}/"
cd "${IMG_DIR}"

# Autobot config (image name, hostname, first-boot user)
cp "${HERE}/config" config

# Drop Glimmr/Pi5-specific substages; keep base + networking + comitup (00,01,02-net,03-set-timezone,04-comitup)
rm -rf stage2/03-accept-mathematica-eula stage2/05-glimmr stage2/06-pi5-ws281x

# First-boot user must match config (autobot/autobot)
HASH="$(openssl passwd -6 autobot)"
printf 'autobot:%s\n' "${HASH}" > stage2/04-comitup/files/userconf.txt

# Autobot install substage (runs after comitup)
S=stage2/05-autobot
rm -rf "${S}"; mkdir -p "${S}/files/payload"
cp "${HERE}/05-autobot/01-packages" "${S}/01-packages"
cp "${HERE}/05-autobot/02-run.sh"   "${S}/02-run.sh"
cp "${STATIC}/autobot.service" "${S}/files/autobot.service"
cp "${STATIC}/comitup.conf"    "${S}/files/comitup.conf"
cp "${STATIC}/autobot.env"     "${S}/files/autobot.env"
chmod +x "${S}/02-run.sh"

# Assemble the app payload
cp -r "${REPO_ROOT}/autobot"          "${S}/files/payload/autobot"
cp    "${REPO_ROOT}/requirements.txt" "${S}/files/payload/requirements.txt"
cp -r "${REPO_ROOT}/webui/dist"       "${S}/files/payload/webui-dist"
if [ -d "${REPO_ROOT}/collector/bionic" ]; then
	mkdir -p "${S}/files/payload/bionic"
	find "${REPO_ROOT}/collector/bionic" -maxdepth 1 -type f \( -name linker -o -name '*.so' \) \
		-exec cp {} "${S}/files/payload/bionic/" \; 2>/dev/null || true
fi
find "${S}/files/payload/autobot" -name __pycache__ -type d -prune -exec rm -rf {} + 2>/dev/null || true

# Build. GIT_HASH is normally derived from git, but our copied base has no .git, so set it explicitly.
export GIT_HASH="${GIT_HASH:-autobot}"
docker rm -v pigen_work 2>/dev/null || true
./build-docker.sh

echo
echo "[build] Done. Image(s):"
ls -lh "${IMG_DIR}/deploy/" || true
