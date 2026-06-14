# DNS Setup for vton.mynarrative.in

## Option A: Named Cloudflare Tunnel (Recommended)

This gives you a stable URL that survives tunnel restarts.

### Step 1: Create a named tunnel
```bash
# On Colab or any machine with cloudflared
cloudflared tunnel login
cloudflared tunnel create drishti-vton
```

### Step 2: Configure the tunnel
```yaml
# ~/.cloudflared/config.yml
tunnel: <TUNNEL_ID>
credentials-file: /root/.cloudflared/<TUNNEL_ID>.json

ingress:
  - hostname: vton.mynarrative.in
    service: http://localhost:8001
    originRequest:
      noTLSVerify: true
  - service: http_status:404
```

### Step 3: Route DNS
```bash
cloudflared tunnel route dns drishti-vton vton.mynarrative.in
```

### Step 4: Run the tunnel
```bash
cloudflared tunnel run drishti-vton
```

### Step 5: Update Colab notebook
In cell 5, replace the cloudflared command with:
```python
tunnel_proc = subprocess.Popen(
    ['cloudflared', 'tunnel', 'run', 'drishti-vton'],
    stdout=subprocess.PIPE, stderr=subprocess.STDOUT
)
```

---

## Option B: Quick Tunnel + CNAME (Simpler)

If you don't need a stable URL, use the quick tunnel and update DNS manually.

### Step 1: Get the tunnel URL from Colab
The notebook already outputs something like:
```
https://cambridge-homeless-beyond-outer.trycloudflare.com
```

### Step 2: Create a CNAME record in Cloudflare
1. Go to Cloudflare Dashboard → mynarrative.in → DNS
2. Add record:
   - Type: `CNAME`
   - Name: `vton`
   - Target: `cambridge-homeless-beyond-outer.trycloudflare.com`
   - Proxy status: Proxied (orange cloud)
3. Save

### Step 3: Update when tunnel changes
When Colab restarts, the tunnel URL changes. Update the CNAME.

---

## Option C: Oracle Cloud Static (Most Stable)

Run cloudflared on your Oracle Cloud instance with a named tunnel.

### On Oracle Cloud:
```bash
# Install cloudflared
wget -q https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-arm64 -O /usr/local/bin/cloudflared
chmod +x /usr/local/bin/cloudflared

# Login and create tunnel
cloudflared tunnel login
cloudflared tunnel create drishti-vton

# Configure
cat > /root/.cloudflared/config.yml << EOF
tunnel: <TUNNEL_ID>
credentials-file: /root/.cloudflared/<TUNNEL_ID>.json

ingress:
  - hostname: vton.mynarrative.in
    service: http://localhost:8001
    originRequest:
      noTLSVerify: true
  - service: http_status:404
EOF

# Route DNS
cloudflared tunnel route dns drishti-vton vton.mynarrative.in

# Run as service
cloudflared service install
systemctl start cloudflared
```

### On Colab:
The Colab VTOE server connects to Oracle Cloud's cloudflared via SSH tunnel:
```bash
# On Colab, forward port 8001 to Oracle Cloud
ssh -R 8001:localhost:8001 ubuntu@<ORACLE_IP> -N &
```

---

## Recommendation

**Option A** (Named Cloudflare Tunnel) is best because:
- Stable URL (`vton.mynarrative.in` never changes)
- Free (Cloudflare free tier)
- Automatic HTTPS
- Works with Colab (just restart tunnel on session start)

Set it up once, and the Colab notebook just needs to run `cloudflared tunnel run drishti-vton`.
