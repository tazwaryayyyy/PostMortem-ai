#!/usr/bin/env bash
# ============================================================
# PostMortem.ai — Vultr Deployment Script
# Target: Vultr Cloud Compute (Ubuntu 22.04, 2 vCPU / 4 GB)
# Usage:  bash vultr-deploy.sh
# ============================================================
set -euo pipefail

# ---------- Configuration ----------
APP_DIR="/opt/postmortem-ai"
REPO_URL="${REPO_URL:-}"          # optional: your git remote URL
DOMAIN="${DOMAIN:-}"              # optional: your domain for TLS

echo "=== PostMortem.ai Vultr Deployer ==="

# ---------- System packages ----------
apt-get update -qq
apt-get install -y -qq \
    ca-certificates curl gnupg git \
    certbot python3-certbot-nginx

# Ubuntu's docker.io package often does not include the Compose v2 plugin.
# Install Docker from the official Docker apt repository so `docker compose`
# is available and the legacy docker-compose v1 ContainerConfig bug is avoided.
install -m 0755 -d /etc/apt/keyrings
if [[ ! -f /etc/apt/keyrings/docker.asc ]]; then
    curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
    chmod a+r /etc/apt/keyrings/docker.asc
fi

. /etc/os-release
echo \
    "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu ${VERSION_CODENAME} stable" \
    > /etc/apt/sources.list.d/docker.list

apt-get update -qq
apt-get install -y -qq \
    docker-ce docker-ce-cli containerd.io \
    docker-buildx-plugin docker-compose-plugin

systemctl enable --now docker

compose() {
    docker compose "$@"
}

# ---------- App directory ----------
mkdir -p "$APP_DIR"
cd "$APP_DIR"

if [[ -n "$REPO_URL" ]]; then
    git clone --depth 1 "$REPO_URL" . 2>/dev/null || git pull
else
    echo "INFO: REPO_URL not set — deploying from current directory."
    cp -r /vagrant/* . 2>/dev/null || true
fi

# ---------- Validate env file ----------
if [[ ! -f .env ]]; then
    echo "ERROR: .env file not found. Copy .env.example and fill in API keys."
    exit 1
fi

# ---------- Firewall ----------
ufw allow 22/tcp   # SSH
ufw allow 80/tcp   # HTTP
ufw allow 443/tcp  # HTTPS
ufw --force enable

# ---------- Build and launch ----------
echo "=== Building and launching with Docker Compose v2 ==="
compose version
compose down --remove-orphans || true
compose build --pull
compose up -d --force-recreate --remove-orphans

# ---------- TLS (optional) ----------
if [[ -n "$DOMAIN" ]]; then
    echo "=== Requesting Let's Encrypt certificate for $DOMAIN ==="
    certbot --nginx -d "$DOMAIN" --non-interactive --agree-tos -m "admin@${DOMAIN}"
fi

# ---------- Done ----------
PUBLIC_IP=$(curl -sf http://api.ipify.org || echo "YOUR_IP")
echo ""
echo "============================================"
echo "  PostMortem.ai deployed!"
echo "  URL: http://${PUBLIC_IP}"
if [[ -n "$DOMAIN" ]]; then
    echo "  TLS: https://${DOMAIN}"
fi
echo "  Health: curl http://${PUBLIC_IP}/health"
echo "============================================"
