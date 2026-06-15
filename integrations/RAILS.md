# RAILS AI Marketplace Integration

Merchant (this repo) and the RAILS marketplace (rails-app) work as a **two-repo integration**.

## Repositories

| Repository | GHE URL | Purpose |
|------------|---------|---------|
| **merchant** | `https://railsai.ghe.com/RAILS-AI/merchant` | Commerce API you deploy and own |
| **rails-app** | `https://railsai.ghe.com/RAILS-AI/rails-app` | Marketplace UI + platform adapters |

## Marketplace adapter (rails-app)

The `merchant` platform adapter is **not in this repo**. It lives in rails-app:

```
rails-app/app/api/mcp-transport/adapters/merchant/
├── api.ts      # REST client — calls your /v1/* API with pk_ key
└── index.ts    # PlatformAdapter — cart, products, Stripe checkout
```

When you open-source or update merchant, coordinate API contract changes with this adapter in rails-app.

## Connect your store

### Option A — marketplace.config.json (rails-app)

```json
[
  {
    "baseUrl": "https://your-store.example.com",
    "platform": "merchant",
    "name": "My Store",
    "publicKey": "pk_your_public_key"
  }
]
```

### Option B — Store editor UI

1. Run rails-app: `npm run dev`
2. Open the marketplace store editor
3. Select platform **Merchant (RAILS OSS)**
4. Enter your store URL and `pk_...` public key

## What the adapter calls

| Marketplace action | Merchant API |
|--------------------|--------------|
| Browse products | `GET /v1/products` |
| Create cart | `POST /v1/carts` |
| Add to cart | `POST /v1/carts/{id}/items` |
| Checkout | `POST /v1/carts/{id}/checkout` → Stripe URL |

## Agent-native endpoints (merchant repo)

These are served directly by your merchant deployment — no rails-app required:

| Endpoint | Protocol |
|----------|----------|
| `GET /.well-known/merchant` | RAILS discovery |
| `GET /.well-known/ucp` | Universal Commerce Protocol |
| `POST /api/merchant/mcp` | MCP JSON-RPC tools |
| `GET /.well-known/oauth-authorization-server` | OAuth 2.0 |

## Local end-to-end test

```bash
# Terminal 1 — merchant API
cd merchant/backend && PYTHONPATH=. uvicorn app.main:app --port 8000

# Terminal 2 — rails-app marketplace
cd rails-app && npm run dev
```

Add `http://localhost:8000` with platform `merchant` and your `pk_...` key.
