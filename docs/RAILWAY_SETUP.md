# DRISHTI Railway Deployment Guide

## Architecture on Railway

```
┌─────────────────────────────────────────────────┐
│                   RAILWAY                        │
│                                                  │
│  ┌──────────┐  ┌──────────┐  ┌──────────────┐  │
│  │ Postgres  │  │  Redis   │  │   Qdrant     │  │
│  │ (plugin)  │  │ (plugin) │  │  (service)   │  │
│  └─────┬─────┘  └────┬─────┘  └──────┬───────┘  │
│        │              │               │          │
│        └──────────────┼───────────────┘          │
│                       │                          │
│                ┌──────┴──────┐                   │
│                │  drishti-api │                   │
│                │  (service)   │                   │
│                └──────┬──────┘                   │
│                       │                          │
└───────────────────────┼──────────────────────────┘
                        │
                   Public URL
                 (mynarrative.in)
```

## Prerequisites

1. GitHub account linked to Railway
2. Railway CLI installed (optional but helpful):
   ```bash
   curl -fsSL https://railway.com/install.sh | sh
   ```

## Step-by-Step Setup

### 1. Create Railway Project

1. Go to [railway.com](https://railway.com) and sign in with GitHub
2. Click **"New Project"** → **"Deploy from GitHub repo"**
3. Select `Shuttler14/mynarrative-drishti`
4. Railway will create a project with your API service

### 2. Add PostgreSQL Plugin

1. In your project, click **"+ New"** → **"Database"** → **"PostgreSQL"**
2. Railway provisions a managed Postgres instance
3. Click on the Postgres service → **"Variables"** tab
4. Copy the `DATABASE_URL` value — you'll need it later
5. The Postgres service gets a private hostname like `postgres.railway.internal:5432`

### 3. Add Redis Plugin

1. Click **"+ New"** → **"Database"** → **"Redis"**
2. Railway provisions a managed Redis instance
3. Click on Redis service → **"Variables"** tab
4. Copy the `REDIS_URL` value

### 4. Add Qdrant Service

1. Click **"+ New"** → **"Docker Image"**
2. Enter image: `qdrant/qdrant:v1.12.0`
3. Name it `qdrant`
4. Go to **"Settings"** → add environment variable:
   - `QDRANT__SERVICE__GRPC_PORT=6334`
5. Go to **"Settings"** → **"Networking"**:
   - Enable **"TCP Proxy"** on port `6333`
   - Or use private networking (recommended): note the internal hostname
6. The internal URL will be `qdrant.railway.internal:6333`

### 5. Configure API Service

1. Click on your API service (the one created from the GitHub repo)
2. Go to **"Settings"**:

#### Build Settings
- **Build Command:** `pip install -r requirements.txt`
- **Start Command:** `sh -c "alembic upgrade head && uvicorn api.main:app --host 0.0.0.0 --port $PORT"`
- **Dockerfile Path:** `Dockerfile`

#### Environment Variables
Click **"Variables"** and add:

```
ENV=production
DEBUG=false

# Database (use Railway plugin variables — reference them)
DATABASE_URL=${{Postgres.DATABASE_URL}}

# Redis (use Railway plugin variable)
REDIS_URL=${{Redis.REDIS_URL}}

# Qdrant (internal Railway URL)
QDRANT_URL=http://qdrant.railway.internal:6333

# JWT — generate a strong secret:
# python3 -c "import secrets; print(secrets.token_urlsafe(64))"
JWT_SECRET=<generate-and-paste-here>

# Shopify
SHOPIFY_DOMAIN=jjdk0v-0c.myshopify.com
SHOPIFY_ACCESS_TOKEN=<your-shopify-storefront-access-token>
SHOPIFY_WEBHOOK_SECRET=<your-shopify-webhook-secret>

# CORS
CORS_ORIGINS=["https://mynarrative.in","https://www.mynarrative.in"]

# VTOE (your Colab tunnel)
VTOE_GPU_URL=https://vton.mynarrative.in

# AWS (for DTF assets)
AWS_ACCESS_KEY_ID=<your-aws-access-key>
AWS_SECRET_ACCESS_KEY=<your-secret>
AWS_REGION=eu-north-1
S3_BUCKET=mynarrative-dtf-bucket

# Observability (optional — add later)
# SENTRY_DSN=
# OTEL_EXPORTER_OTLP_ENDPOINT=
```

**Important:** Railway supports variable references. `${{Postgres.DATABASE_URL}}` automatically links to your Postgres plugin.

### 6. Deploy

1. Push to `main` branch — Railway auto-deploys
2. Check **"Deployments"** tab for build logs
3. Once live, click **"Settings"** → **"Networking"** → **"Generate Domain"**
4. You get a URL like `drishti-api.up.railway.app`
5. Map your custom domain (see step 7)

### 7. Custom Domain (mynarrative.in)

1. In API service → **"Settings"** → **"Networking"** → **"Custom Domain"**
2. Enter `mynarrative.in` (or `api.mynarrative.in`)
3. Railway gives you DNS records to add:
   ```
   Type: CNAME
   Name: @ (or api)
   Value: drishti-api.up.railway.app
   ```
4. Update your DNS provider (Cloudflare/GoDaddy/etc.)
5. Railway auto-provisions SSL certificate

### 8. Run Database Migrations

Railway runs `alembic upgrade head` on startup (it's in the CMD). But if you need to run manually:

1. Go to API service → **"Settings"** → **"Deploy"**
2. Add a **"Custom Start Command"** override:
   ```bash
   sh -c "alembic upgrade head && uvicorn api.main:app --host 0.0.0.0 --port $PORT"
   ```
3. Or use Railway CLI:
   ```bash
   railway run alembic upgrade head
   ```

### 9. Verify

1. Check health endpoint:
   ```bash
   curl https://mynarrative.in/health
   ```
   Should return: `{"status":"ok","service":"Drishti","version":"1.0.0"}`

2. Check docs (if DEBUG=true):
   ```bash
   curl https://mynarrative.in/docs
   ```

## Railway Pricing

| Service | Free Tier | Hobby ($5/mo) |
|---------|-----------|---------------|
| Postgres | 500MB, 100h/mo | 1GB, unlimited |
| Redis | 25MB, 100h/mo | 100MB, unlimited |
| API | 500h/mo | unlimited |
| Qdrant | 1GB RAM | custom |

**Total for hobby:** ~$5-10/mo for Postgres + Redis + API + Qdrant.

## Environment Variables Reference

| Variable | Required | Description |
|----------|----------|-------------|
| `DATABASE_URL` | Yes | Postgres connection string |
| `REDIS_URL` | Yes | Redis connection string |
| `QDRANT_URL` | Yes | Qdrant HTTP endpoint |
| `JWT_SECRET` | Yes | HS256 signing key (min 32 chars) |
| `SHOPIFY_WEBHOOK_SECRET` | Yes | HMAC verification |
| `ENV` | Yes | `production` or `local` |
| `PORT` | Auto | Railway sets this automatically |
| `SENTRY_DSN` | No | Error tracking |
| `VTOE_GPU_URL` | No | Colab GPU endpoint |

## Troubleshooting

**Build fails:**
- Check that `requirements.txt` has all deps
- Railway uses Python 3.12 by default — ensure compatibility

**Database connection fails:**
- Use `${{Postgres.DATABASE_URL}}` variable reference (not hardcoded)
- Ensure Postgres plugin is in the same project

**Qdrant unreachable:**
- Use `qdrant.railway.internal:6333` (private network)
- Do NOT use the public proxy URL for internal service-to-service calls

**JWT validation fails:**
- Ensure `JWT_SECRET` is set and >= 32 characters
- In production mode, placeholder secrets cause startup failure (by design)
