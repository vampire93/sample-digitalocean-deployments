#!/usr/bin/env bash
# Fallback deploy: one Droplet, source rsync'd up, built and run with Compose.
#
# WHEN TO USE THIS
#   When App Platform is fighting you and you've decided to stop debugging a
#   build system on the clock. Up in ~4 minutes. Deciding to switch — and saying
#   why — reads better in a review than silently losing 30 minutes.
#
# WHAT YOU GIVE UP
#   No TLS (bare HTTP on the droplet IP — see deploy/tls/ for the Caddy fix),
#   no managed backups, no health-checked rollout. You own patching and restarts.
#
# WHY NO REGISTRY
#   Pulling private images needs registry auth on the box. This path exists for
#   when the registry or the image build is the problem, so it depends on
#   neither: source goes up, the droplet builds it.
set -euo pipefail

REGION=${REGION:-blr1}
SIZE=${SIZE:-s-2vcpu-4gb}
IMAGE=${IMAGE:-docker-20-04}
NAME=${NAME:-scaffold-demo}
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"   # the scaffold/ dir

SSH_KEY=${SSH_KEY:-$(doctl compute ssh-key list --format ID --no-header | head -1)}
[ -n "$SSH_KEY" ] || { echo "No SSH key on the account. Add one: doctl compute ssh-key create" >&2; exit 1; }

echo "==> Creating droplet '$NAME' ($SIZE, $REGION)"
doctl compute droplet create "$NAME" \
  --region "$REGION" --size "$SIZE" --image "$IMAGE" \
  --ssh-keys "$SSH_KEY" \
  --user-data-file "$HERE/deploy/cloud-init.yaml" \
  --wait --format ID,Name,PublicIPv4 || true      # || true so re-runs don't abort

IP=$(doctl compute droplet get "$NAME" --format PublicIPv4 --no-header)
[ -n "$IP" ] || { echo "No IP assigned" >&2; exit 1; }
echo "==> IP: $IP"

echo "==> Waiting for SSH"
for _ in $(seq 1 40); do
  ssh -o StrictHostKeyChecking=accept-new -o ConnectTimeout=5 root@"$IP" true 2>/dev/null && break
  sleep 5
done

# Two separate waits on purpose: the box answers SSH well before cloud-init has
# finished installing Docker. Skipping this is why deploy scripts fail
# intermittently and look flaky.
echo "==> Waiting for cloud-init to finish (Docker install)"
for _ in $(seq 1 40); do
  ssh -o StrictHostKeyChecking=accept-new root@"$IP" \
    "test -f /var/lib/cloud-init-done && docker compose version" >/dev/null 2>&1 && break
  sleep 5
done

echo "==> Syncing source"
rsync -az --delete \
  --exclude node_modules --exclude .git --exclude __pycache__ \
  --exclude .venv --exclude dist --exclude .env \
  "$HERE"/ root@"$IP":/srv/app/

echo "==> Building and starting"
ssh root@"$IP" "cd /srv/app && docker compose up -d --build"

# nginx resolves its upstream once at startup and caches the container IP, so a
# rebuilt backend leaves it pointing at a dead address until it restarts.
ssh root@"$IP" "cd /srv/app && docker compose restart web"

echo "==> Waiting for the API"
for _ in $(seq 1 30); do
  curl -sf "http://$IP:8000/healthz" >/dev/null 2>&1 && break
  sleep 5
done

echo
echo "=========================================="
echo "  Dashboard : http://$IP:5173"
echo "  API       : http://$IP:8000/api/v1"
echo "=========================================="
echo "==> Smoke tests"
curl -s "http://$IP:8000/healthz"; echo
curl -s "http://$IP:8000/api/job-types" | head -c 200; echo
echo
echo "TLS:      bash deploy/tls/enable-tls.sh   (Caddy + sslip.io, no domain needed)"
echo "Teardown: doctl compute droplet delete $NAME -f"
