# Autobot — the unified app. Runs on the Raspberry Pi (aarch64). The host kernel must allow 32-bit ARM
# execution (default on Raspberry Pi OS / HA OS); the native bridge is a 32-bit Android binary run through
# the bundled bionic linker. Only H.265 passthrough + a JPEG snapshot tap are done here, so a generic
# ffmpeg (no HW codecs) is enough. (For hardware-free dev on a PC, set AUTOBOT_ROBOT_LINK=mock and just run
# `python -m autobot` — no Docker needed.)
ARG BUILD_FROM=python:3.11-slim-bookworm
FROM ${BUILD_FROM}

RUN apt-get update && apt-get install -y --no-install-recommends \
        ffmpeg jq curl ca-certificates espeak-ng \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

# mediamtx (RTSP/WebRTC/HLS server), placed where the native link expects it ($EBO_DIR).
ARG MEDIAMTX_VERSION=1.19.0
RUN set -e; \
    case "$(dpkg --print-architecture)" in \
      arm64) MT=arm64 ;; armhf) MT=armv7 ;; amd64) MT=amd64 ;; *) MT=arm64 ;; \
    esac; \
    mkdir -p /opt/ebo; \
    curl -fsSL "https://github.com/bluenviron/mediamtx/releases/download/v${MEDIAMTX_VERSION}/mediamtx_v${MEDIAMTX_VERSION}_linux_${MT}.tar.gz" \
      | tar xz -C /opt/ebo mediamtx

# Application code (the whole package) + the run script.
COPY autobot/ /app/autobot/
COPY webui/dist/ /app/webui/dist/
COPY run.sh /app/run.sh

# Native runtime assets the link expects under $EBO_DIR (/opt/ebo):
#   - ebo_bridge (native TUTK binary; build with autobot/robot/native/build_bridge.sh)
#   - mediamtx.template.yml
COPY autobot/robot/native/ebo_bridge /opt/ebo/ebo_bridge
COPY autobot/robot/native/mediamtx.template.yml /opt/ebo/mediamtx.template.yml

# Vendor files (NOT in git; provided by the collector). The bundled bionic runtime is AOSP (Apache-2.0);
# the TUTK .so and ioctl9930.bin are device-specific and captured from your own app/device.
COPY vendor/lib/        /opt/ebo/lib/
COPY vendor/bionic/     /opt/ebo/bionic/
COPY vendor/ioctl9930.bin /opt/ebo/ioctl9930.bin

RUN chmod +x /app/run.sh /opt/ebo/ebo_bridge /opt/ebo/bionic/linker /opt/ebo/mediamtx

ENV EBO_DIR=/opt/ebo AUTOBOT_HOST=0.0.0.0 AUTOBOT_PORT=8200 AUTOBOT_ROBOT_LINK=native EBO_TALK=1
# 8200 web UI/API · 8554 RTSP · 8889 WebRTC · 8888 HLS
EXPOSE 8200 8554 8889 8888
CMD ["/app/run.sh"]
