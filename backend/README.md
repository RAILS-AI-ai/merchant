# Merchant Python Backend

FastAPI commerce API — Stripe + UCP + OAuth + RAILS MCP. Drop-in replacement for the original Cloudflare Workers TypeScript backend.

## Quick start

```bash
cd backend
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env

# Initialize API keys
PYTHONPATH=. python scripts/init.py

# Run API
uvicorn app.main:app --reload --port 8000

# Seed demo data
PYTHONPATH=. python scripts/seed.py http://localhost:8000 sk_your_admin_key
```

## RAILS AI marketplace integration

Add your store to `marketplace.config.json` in a RAILS marketplace deployment:

```json
[
  {
    "baseUrl": "http://localhost:8000",
    "platform": "merchant",
    "publicKey": "pk_..."
  }
]
```

Discovery endpoints:
- `GET /.well-known/merchant` — RAILS agent discovery (UCP + MCP)
- `GET /.well-known/ucp` — Universal Commerce Protocol profile
- `POST /api/merchant/mcp` — MCP JSON-RPC commerce tools

## API compatibility

All `/v1/*` REST endpoints match the original TypeScript API. The React admin dashboard and example storefront work unchanged — point them at `http://localhost:8000`.

## Stack

| Component | Technology |
|-----------|------------|
| Runtime | Python 3.12 + FastAPI |
| Database | SQLite (dev) / PostgreSQL (prod) |
| Payments | Stripe |
| Agent protocol | UCP + MCP |
| Identity | OAuth 2.0 + PKCE |
| Scheduler | APScheduler (cart cleanup) |

## Docker Compose (production)

From repo root:

```bash
cp .env.example .env
docker compose up -d --build
```

See [`../DEPLOYMENT.md`](../DEPLOYMENT.md) for full production guide.
