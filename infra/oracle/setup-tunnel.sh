#!/bin/bash
set -euo pipefail

echo "=== Cloudflare Tunnel Setup for Drishti ==="

if ! command -v cloudflared &> /dev/null; then
    echo "Installing cloudflared..."
    wget -q https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-arm64 -O /usr/local/bin/cloudflared
    chmod +x /usr/local/bin/cloudflared
fi

echo "=== Authenticate with Cloudflare ==="
echo "Run: cloudflared tunnel login"
echo "This will open a browser to authenticate"
echo ""

read -p "Press Enter after authentication..."

echo "=== Creating tunnel ==="
cloudflared tunnel create drishti

TUNNEL_ID=$(cloudflared tunnel list | grep drishti | awk '{print $1}')
echo "Tunnel ID: $TUNNEL_ID"

echo "=== Configuring tunnel ==="
cat > /root/.cloudflared/config.yml << EOF
tunnel: $TUNNEL_ID
credentials-file: /root/.cloudflared/$TUNNEL_ID.json

ingress:
  - hostname: api.mynarrative.in
    service: http://localhost:8000
    originRequest:
      noTLSVerify: true
  - hostname: vton.mynarrative.in
    service: http://localhost:8001
      originRequest:
        noTLSVerify: true
  - service: http_status:404
EOF

echo "=== Adding DNS records ==="
cloudflared tunnel route dns $TUNNEL_ID api.mynarrative.in
cloudflared tunnel route dns $TUNNEL_ID vton.mynarrative.in

echo "=== Starting tunnel ==="
cloudflared service install

echo "=== Tunnel setup complete ==="
echo "API: https://api.mynarrative.in"
echo "VTON: https://vton.mynarrative.in"
