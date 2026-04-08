#!/bin/sh
# Entrypoint: clone/pull all chain repos, then start the app.
#
# Requires:
#   GITEA_TOKEN  — a Gitea personal access token with read access to hashiraio/*
#
# Repos are stored on a persistent volume at /opt/repos so they survive
# container restarts and redeployments.

set -e

BASE_URL="https://version.btcfi.wtf/hashiraio"
REPOS_DIR="/opt/repos"

# Map: local directory name → repo slug on Gitea
# Format: "local_name:gitea_slug:branch"
REPOS="
cobi-v2:cobi-v2:staging
bit-ponder:bit-ponder:staging
bitcoin-watcher:bitcoin-watcher:feat/rollout2
btc-relayer:btc-relayer:staging
evm-executor:evm-executor:staging
garden-evm-watcher:garden-evm-watcher:staging
evm-swapper-relay:evm-swapper-relay:staging
garden-contract-hub:garden-contract-hub:main
solana-executor:solana-executor:staging
solana-watcher:solana-watcher:staging
solana-relayer:solana-relayer:staging
solana-native-swaps:solana-native-swaps:dev
solana-spl-swaps:solana-spl-swaps:dev
"

if [ -z "$GITEA_TOKEN" ]; then
  echo "[entrypoint] WARNING: GITEA_TOKEN not set — skipping repo sync. Specialist tools will not work."
else
  mkdir -p "$REPOS_DIR"

  echo "$REPOS" | while IFS=: read -r local_name slug branch; do
    # Skip empty lines
    [ -z "$local_name" ] && continue

    dest="$REPOS_DIR/$local_name"
    clone_url="https://x-access-token:${GITEA_TOKEN}@version.btcfi.wtf/hashiraio/${slug}.git"

    if [ -d "$dest/.git" ]; then
      echo "[entrypoint] Pulling $local_name ($branch)..."
      git -C "$dest" fetch --quiet origin "$branch" 2>/dev/null || true
      git -C "$dest" checkout --quiet "$branch" 2>/dev/null || true
      git -C "$dest" merge --ff-only "origin/$branch" 2>/dev/null || echo "[entrypoint] WARNING: ff-only merge failed for $local_name, skipping"
    else
      echo "[entrypoint] Cloning $local_name ($branch)..."
      git clone --quiet --depth=1 --branch "$branch" "$clone_url" "$dest" || echo "[entrypoint] WARNING: clone failed for $local_name"
    fi
  done

  echo "[entrypoint] Repo sync complete."
fi

echo "[entrypoint] Starting Garden RCA Agent..."
exec python -m uvicorn main:app --host 0.0.0.0 --port "${PORT:-8000}"
