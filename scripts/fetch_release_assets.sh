#!/usr/bin/env bash
# Fetch FreeBo's big binaries (kept OUT of git) from a GitHub Release into ./release-staging/.
# Configure: FREEBO_RELEASE_REPO=owner/freebo  FREEBO_RELEASE_TAG=latest (default).
# Uses the `gh` CLI if available (works with private repos), else falls back to curl on public assets.
set -euo pipefail

REPO="${FREEBO_RELEASE_REPO:-}"
TAG="${FREEBO_RELEASE_TAG:-latest}"
DEST="release-staging"
# Asset names we know about (skipped silently if a release doesn't have them):
ASSETS=(
  "freebo-cred-collector-rola.apk"
  "freebo-cred-collector-ebohome.apk"
  "freebo-wheelhouse-$(uname -s)-$(uname -m).tar.gz"
)

if [[ -z "$REPO" ]]; then
  echo "Set FREEBO_RELEASE_REPO=owner/repo (and optionally FREEBO_RELEASE_TAG)." >&2
  exit 2
fi
mkdir -p "$DEST"

if command -v gh >/dev/null 2>&1; then
  echo "[fetch] gh release download $TAG from $REPO -> $DEST/"
  # download everything on the release; --clobber so re-runs refresh
  gh release download "$([[ "$TAG" == latest ]] && echo "" || echo "$TAG")" \
     --repo "$REPO" --dir "$DEST" --clobber || {
       echo "[fetch] no matching release, or nothing to download." >&2; }
else
  echo "[fetch] gh not found; trying public asset URLs via curl"
  base="https://github.com/$REPO/releases/${TAG:+download/$TAG}"
  [[ "$TAG" == latest ]] && base="https://github.com/$REPO/releases/latest/download"
  for a in "${ASSETS[@]}"; do
    echo "  - $a"
    curl -fsSL "$base/$a" -o "$DEST/$a" || echo "    (skip: not present)"
  done
fi
echo "[fetch] done -> $DEST/"
ls -lh "$DEST" 2>/dev/null || true
