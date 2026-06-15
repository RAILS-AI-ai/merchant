# Production Deployment

## Docker Compose (recommended)

```bash
cp .env.example .env
# Set MERCHANT_URL to your public URL (e.g. https://store.example.com)
# Set STRIPE_SECRET_KEY and STRIPE_WEBHOOK_SECRET

docker compose up -d --build
```

### First-time setup

```bash
# Create API keys (direct DB write inside container)
docker compose exec api python scripts/init.py

# Connect Stripe (if not set in .env)
curl -X POST http://localhost:8000/v1/setup/stripe \
  -H "Authorization: Bearer sk_..." \
  -H "Content-Type: application/json" \
  -d '{"stripe_secret_key":"sk_live_...","stripe_webhook_secret":"whsec_..."}'
```

### Stripe webhooks

Point Stripe to: `https://your-domain/v1/webhooks/stripe`

### Volumes

| Volume | Purpose |
|--------|---------|
| `pgdata` | PostgreSQL data |
| `merchant_storage` | Uploaded images + SQLite fallback |

### Environment variables

| Variable | Default | Description |
|----------|---------|-------------|
| `DATABASE_URL` | `postgresql://merchant:merchant@db:5432/merchant` | Set by compose |
| `MERCHANT_URL` | `http://localhost:8000` | Public API URL |
| `IMAGES_URL` | `{MERCHANT_URL}/v1/images` | Image CDN base |
| `STORE_NAME` | `My Store` | Display name |
| `CORS_ORIGINS` | localhost:3000 | Admin/marketplace origins |
| `STRIPE_SECRET_KEY` | — | Stripe API key |
| `STRIPE_WEBHOOK_SECRET` | — | Stripe webhook signing secret |

## RAILS AI marketplace

Add your deployed store to the RAILS marketplace `marketplace.config.json`:

```json
[
  {
    "baseUrl": "https://store.example.com",
    "platform": "merchant",
    "name": "My Store",
    "publicKey": "pk_..."
  }
]
```

The marketplace `merchant` adapter calls your REST `/v1/*` API using the public key.

## Managed PostgreSQL

For AWS RDS, Supabase, or Neon — set `DATABASE_URL` and run a single API container:

```bash
docker build -t merchant-api ./backend
docker run -p 8000:8000 \
  -e DATABASE_URL=postgresql://user:pass@host:5432/merchant \
  -e MERCHANT_URL=https://store.example.com \
  -e STRIPE_SECRET_KEY=sk_live_... \
  merchant-api
```

Schema is auto-created on startup via SQLAlchemy `create_all`. For production migrations, add Alembic.

## Health checks

- `GET /health` — liveness
- `GET /` — service info
