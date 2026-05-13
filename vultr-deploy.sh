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
    docker.io docker-compose-plugin curl git \
    certbot python3-certbot-nginx

systemctl enable --now docker

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
docker compose build --pull
docker compose up -d

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
