#!/usr/bin/env bash
# Entrypoint for the unified Autobot container (native mode on the Pi).
# Reads Home Assistant add-on options (/data/options.json) if present, otherwise uses env vars.
# Robot secrets are NEVER baked into the image: they come from the add-on config / env / .env.
set -e
OPT=/data/options.json

if [ -f "$OPT" ] && command -v jq >/dev/null 2>&1; then
  export EBO_LICENSE=$(jq -r '.license_key // empty' "$OPT")
  export EBO_UID=$(jq -r '.uid // empty' "$OPT")
  export EBO_AUTHKEY=$(jq -r '.authkey // empty' "$OPT")
  export EBO_IDENTITY=$(jq -r '.av_identity // empty' "$OPT")
  export EBO_TOKEN=$(jq -r '.av_token // empty' "$OPT")
  export EBO_STREAM_USER=$(jq -r '.stream_user // "ebo"' "$OPT")
  export EBO_STREAM_PASS=$(jq -r '.stream_pass // empty' "$OPT")
  export EBO_MQTT_HOST=$(jq -r '.mqtt_host // empty' "$OPT")
  export EBO_MQTT_PORT=$(jq -r '.mqtt_port // 1883' "$OPT")
  export EBO_MQTT_USER=$(jq -r '.mqtt_user // empty' "$OPT")
  export EBO_MQTT_PASS=$(jq -r '.mqtt_pass // empty' "$OPT")
  export AUTOBOT_AI_BASE_URL=$(jq -r '.ai_base_url // empty' "$OPT")
  export AUTOBOT_AI_API_KEY=$(jq -r '.ai_api_key // empty' "$OPT")
  export AUTOBOT_AI_MODEL=$(jq -r '.ai_model // empty' "$OPT")
fi

if [ "${AUTOBOT_ROBOT_LINK:-native}" = "native" ]; then
  : "${EBO_LICENSE:?missing license_key}"; : "${EBO_UID:?missing uid}"; : "${EBO_AUTHKEY:?missing authkey}"
  : "${EBO_IDENTITY:?missing av_identity}"; : "${EBO_TOKEN:?missing av_token}"
  : "${EBO_STREAM_USER:=ebo}"; : "${EBO_STREAM_PASS:?missing stream_pass (RTSP password)}"
fi

cd /app
# A Docker container already provides an isolated PID namespace (low PIDs), which the 32-bit bionic libc
# requires (pid <= 65535). No 'unshare' needed inside a container.
exec python3 -m autobot
