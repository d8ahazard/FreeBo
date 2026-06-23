#!/bin/bash -e
# Install Autobot into the image (arm64). comitup itself is installed/configured by the 04-comitup substage
# of the proven base; here we add the app, its venv, mediamtx, and the systemd service. Run scripts execute
# with this substage dir as CWD, so `files/...` is relative and `on_chroot` runs inside the image rootfs.

MEDIAMTX_VERSION="${MEDIAMTX_VERSION:-1.19.0}"
PAYLOAD="files/payload"

install -d "${ROOTFS_DIR}/opt/autobot" "${ROOTFS_DIR}/opt/ebo" "${ROOTFS_DIR}/etc/autobot"

# app payload
cp -r "${PAYLOAD}/autobot"          "${ROOTFS_DIR}/opt/autobot/autobot"
cp    "${PAYLOAD}/requirements.txt" "${ROOTFS_DIR}/opt/autobot/requirements.txt"
if [ -d "${PAYLOAD}/webui-dist" ]; then
	install -d "${ROOTFS_DIR}/opt/autobot/webui/dist"
	cp -r "${PAYLOAD}/webui-dist/." "${ROOTFS_DIR}/opt/autobot/webui/dist/"
fi
if [ -d "${PAYLOAD}/bionic" ]; then
	cp -r "${PAYLOAD}/bionic" "${ROOTFS_DIR}/opt/ebo/bionic"
fi
cp "${ROOTFS_DIR}/opt/autobot/autobot/robot/native/mediamtx.template.yml" "${ROOTFS_DIR}/opt/ebo/mediamtx.template.yml" || true

# system config (comitup.conf here points the AP name/mdns at Autobot; the package is from 04-comitup)
install -m 0644 files/autobot.service "${ROOTFS_DIR}/etc/systemd/system/autobot.service"
install -m 0644 files/comitup.conf    "${ROOTFS_DIR}/etc/comitup.conf"
install -m 0644 files/autobot.env     "${ROOTFS_DIR}/etc/autobot/autobot.env"

on_chroot << EOF
set -e
curl -fsSL "https://github.com/bluenviron/mediamtx/releases/download/v${MEDIAMTX_VERSION}/mediamtx_v${MEDIAMTX_VERSION}_linux_arm64.tar.gz" \
	| tar xz -C /opt/ebo mediamtx
chmod +x /opt/ebo/mediamtx || true

python3 -m venv /opt/autobot/venv
# Use PyPI directly (not piwheels): under qemu-user emulation piwheels' TLS is flaky (SSLZeroReturnError),
# which makes pip waste minutes retrying. PyPI ships aarch64 manylinux wheels for our deps.
/opt/autobot/venv/bin/pip install --no-cache-dir --retries 10 --timeout 120 --index-url https://pypi.org/simple --upgrade pip
/opt/autobot/venv/bin/pip install --no-cache-dir --retries 10 --timeout 120 --index-url https://pypi.org/simple -r /opt/autobot/requirements.txt

systemctl enable avahi-daemon || true
systemctl enable autobot.service
EOF

echo "[05-autobot] Autobot installed (service: autobot.service, UI on :8200, mDNS: autobot.local)"
