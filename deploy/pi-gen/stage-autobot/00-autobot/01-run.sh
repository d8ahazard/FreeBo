#!/bin/bash -e
# Install Autobot into the image: app + venv, mediamtx, comitup (headless wifi), and a systemd service.
# Runs on the host with ${ROOTFS_DIR} pointing at the image rootfs; `on_chroot` runs commands inside it.
# The app payload is assembled into files/payload/ by build-wsl.sh before the build.

# pi-gen runs *-run.sh with the working directory set to this step dir, so `files/...` is relative.
MEDIAMTX_VERSION="${MEDIAMTX_VERSION:-1.19.0}"
PAYLOAD="files/payload"

# --- copy the app payload into /opt/autobot ---
install -d "${ROOTFS_DIR}/opt/autobot" "${ROOTFS_DIR}/opt/ebo" "${ROOTFS_DIR}/etc/autobot"
cp -r "${PAYLOAD}/autobot"          "${ROOTFS_DIR}/opt/autobot/autobot"
cp    "${PAYLOAD}/requirements.txt" "${ROOTFS_DIR}/opt/autobot/requirements.txt"
if [ -d "${PAYLOAD}/webui-dist" ]; then
	install -d "${ROOTFS_DIR}/opt/autobot/webui/dist"
	cp -r "${PAYLOAD}/webui-dist/." "${ROOTFS_DIR}/opt/autobot/webui/dist/"
fi
# Bundled bionic runtime for the native TUTK bridge (AOSP, Apache-2.0). The TUTK .so + ioctl9930.bin +
# ebo_bridge binary are device-specific and provisioned at runtime (collector), not baked into the image.
if [ -d "${PAYLOAD}/bionic" ]; then
	cp -r "${PAYLOAD}/bionic" "${ROOTFS_DIR}/opt/ebo/bionic"
fi
cp "${ROOTFS_DIR}/opt/autobot/autobot/robot/native/mediamtx.template.yml" "${ROOTFS_DIR}/opt/ebo/mediamtx.template.yml" || true

# --- system config files ---
install -m 0644 "files/autobot.service" "${ROOTFS_DIR}/etc/systemd/system/autobot.service"
install -m 0644 "files/comitup.conf"    "${ROOTFS_DIR}/etc/comitup.conf"
install -m 0644 "files/autobot.env"     "${ROOTFS_DIR}/etc/autobot/autobot.env"

# --- mediamtx (RTSP/WebRTC/HLS) for arm64, into EBO_DIR ---
on_chroot << EOF
set -e
curl -fsSL "https://github.com/bluenviron/mediamtx/releases/download/v${MEDIAMTX_VERSION}/mediamtx_v${MEDIAMTX_VERSION}_linux_arm64.tar.gz" \
	| tar xz -C /opt/ebo mediamtx
chmod +x /opt/ebo/mediamtx || true
EOF

# --- Python venv + dependencies ---
on_chroot << 'EOF'
set -e
python3 -m venv /opt/autobot/venv
/opt/autobot/venv/bin/pip install --no-cache-dir --upgrade pip
/opt/autobot/venv/bin/pip install --no-cache-dir -r /opt/autobot/requirements.txt
EOF

# --- comitup: AP + captive portal for headless wifi onboarding ---
on_chroot << 'EOF'
set -e
curl -fsSL https://davesteele.github.io/comitup/latest/davesteele-comitup-apt-source_latest.deb -o /tmp/comitup-repo.deb
apt-get install -y /tmp/comitup-repo.deb
apt-get update
apt-get install -y comitup
rm -f /tmp/comitup-repo.deb
# comitup drives wifi through NetworkManager; make sure NM owns the interfaces and dhcpcd is out of the way.
systemctl enable NetworkManager || true
systemctl disable dhcpcd 2>/dev/null || true
systemctl enable comitup || true
systemctl enable comitup-web 2>/dev/null || true
EOF

# --- enable Autobot + mDNS ---
on_chroot << 'EOF'
set -e
systemctl enable avahi-daemon || true
systemctl enable autobot.service
EOF

echo "[stage-autobot] Autobot installed. Service: autobot.service; wifi onboarding: comitup."
