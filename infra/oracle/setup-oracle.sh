#!/bin/bash
set -euo pipefail

echo "=== Drishti Oracle Cloud Free Tier Setup ==="
echo "Instance: VM.Standard.A1.Flex (4 OCPU ARM, 24GB RAM)"
echo ""

ORACLE_USER=${ORACLE_USER:-"ubuntu"}
ORACLE_HOST=${ORACLE_HOST:-""}

if [ -z "$ORACLE_HOST" ]; then
    echo "Usage: ORACLE_HOST=<ip> ./setup-oracle.sh"
    exit 1
fi

echo "Connecting to $ORACLE_HOST..."

ssh $ORACLE_USER@$ORACLE_HOST << 'REMOTE_SCRIPT'
set -euo pipefail

echo "=== Updating system ==="
sudo apt update && sudo apt upgrade -y

echo "=== Installing Docker ==="
sudo apt install -y docker.io docker-compose-v2
sudo usermod -aG docker $USER
sudo systemctl enable docker
sudo systemctl start docker

echo "=== Installing Docker Compose ==="
sudo apt install -y docker-compose-v2

echo "=== Creating app directory ==="
mkdir -p /opt/drishti
cd /opt/drishti

echo "=== Creating .env ==="
cat > .env << 'ENVFILE'
POSTGRES_PASSWORD=$(openssl rand -hex 32)
JWT_SECRET=$(openssl rand -hex 32)
SHOPIFY_WEBHOOK_SECRET=
SHOPIFY_ACCESS_TOKEN=
AWS_ACCESS_KEY_ID=
AWS_SECRET_ACCESS_KEY=
AWS_REGION=eu-north-1
S3_BUCKET=mynarrative-dtf-bucket
VTOE_GPU_URL=http://localhost:8001
ENVFILE

echo "=== Oracle Cloud setup complete ==="
echo "Next: Upload docker-compose.yml and start services"
REMOTE_SCRIPT

echo ""
echo "=== Setup complete ==="
echo "Next steps:"
echo "1. Upload docker-compose.yml to /opt/drishti/"
echo "2. Upload the Drishti codebase to /opt/drishti/"
echo "3. Run: cd /opt/drishti && docker compose up -d"
echo "4. Set up Cloudflare Tunnel for external access"
