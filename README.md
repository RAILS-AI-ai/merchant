# Merchant UI Agentic Commerce Infrastructure/Gateway 

**The Open-source Agentic Commerce backend for Stripe with Bring a Stripe key(BYOK) paradigm and Start selling any Product Catalogue from your Shopify retail storefront to AI Agents. Start Selling and make $$$ **

A lightweight, API-first backend for products, inventory, checkout, and orders — built in **Python (FastAPI)** for the **[RAILS AI](https://www.userails.ai/#)** agentic commerce platform (UCP + MCP + OAuth 2.0).

**Organization:** https://www.userails.ai/# 

## Quick Start

```bash
# 1. Clone & setup
git clone https://railsai.ghe.com/RAILS-AI/merchant.git
cd merchant/backend
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env

# 2. Initialize (creates API keys)
PYTHONPATH=. python scripts/init.py

# 3. Start the API
PYTHONPATH=. uvicorn app.main:app --reload --port 8000

# 4. Seed demo data (optional)
PYTHONPATH=. python scripts/seed.py http://localhost:8000 sk_your_admin_key

# 5. Connect Stripe
curl -X POST http://localhost:8000/v1/setup/stripe \
  -H "Authorization: Bearer sk_your_admin_key" \
  -H "Content-Type: application/json" \
  -d '{"stripe_secret_key":"sk_test_..."}'

# 6. Admin dashboard
cd ../admin && npm install && npm run dev
```

## RAILS AI Marketplace Integration

This backend is agent-ready for the RAILS AI conversational marketplace. Two repos work together:

| Repository | Role |
|------------|------|
| **[RAILS-AI/merchant](https://railsai.ghe.com/RAILS-AI/merchant)** (this repo) | Commerce API — products, carts, Stripe checkout, UCP, MCP |
| **[RAILS-AI/rails-app]( For RAILS AI Infra with Advanced B2B Merchant Features buy Enteprise License ** | Marketplace UI + `merchant` platform adapter |

### Connect to the marketplace

In **rails-app**, add to `marketplace.config.json` or the store editor UI:

```json
[
  {
    "baseUrl": "http://localhost:8000",
    "platform": "merchant",
    "name": "My Store",
    "publicKey": "pk_..."
  }
]
```

The adapter source lives at:

```
rails-app/app/api/mcp-transport/adapters/merchant/
├── api.ts      # REST client for /v1/*
└── index.ts    # PlatformAdapter (cart, checkout, product search)
```

See [`integrations/RAILS.md`](integrations/RAILS.md) for the full integration guide.

**Agent discovery endpoints (this repo):**
- `GET /.well-known/merchant` — RAILS agent discovery (UCP + MCP)
- `GET /.well-known/ucp` — Universal Commerce Protocol profile
- `POST /api/merchant/mcp` — MCP JSON-RPC commerce tools
- `GET /.well-known/oauth-authorization-server` — OAuth 2.0 for customer identity

See [`backend/README.md`](backend/README.md) for full Python backend docs.

## Production (Docker Compose + PostgreSQL)

```bash
# 1. Configure environment
cp .env.example .env
# Edit STRIPE_SECRET_KEY, MERCHANT_URL, etc.

# 2. Start PostgreSQL + API
npm run docker:up
# or: docker compose up -d --build

# 3. Initialize API keys (first run — writes to Postgres)
docker compose exec api python scripts/init.py

# 4. Seed demo data (optional)
docker compose exec api python scripts/seed.py http://localhost:8000 sk_your_admin_key
```

Services:
- **API** → `http://localhost:8000`
- **PostgreSQL** → `localhost:5433` (user/pass/db: `merchant`)

See [`DEPLOYMENT.md`](DEPLOYMENT.md) for production notes.

## API Reference

All endpoints require `Authorization: Bearer <key>` header.

- `pk_...` → Public key. Can create carts and checkout.
- `sk_...` → Admin key. Full access to everything.

### Products (admin)

```bash
# List products (with pagination)
GET /v1/products?limit=20&cursor=...&status=active

# Get single product
GET /v1/products/{id}

# Create product
POST /v1/products
{"title": "T-Shirt", "description": "Premium cotton tee"}

# Update product
PATCH /v1/products/{id}
{"title": "Updated Title", "status": "draft"}

# Delete product (fails if variants have been ordered)
DELETE /v1/products/{id}

# Add variant
POST /v1/products/{id}/variants
{"sku": "TEE-BLK-M", "title": "Black / M", "price_cents": 2999}

# Update variant
PATCH /v1/products/{id}/variants/{variantId}
{"price_cents": 3499}

# Delete variant (fails if ordered)
DELETE /v1/products/{id}/variants/{variantId}
```

### Inventory (admin)

```bash
# List inventory (with pagination)
GET /v1/inventory?limit=100&cursor=...&low_stock=true

# Get single SKU
GET /v1/inventory?sku=TEE-BLK-M

# Adjust inventory
POST /v1/inventory/{sku}/adjust
{"delta": 100, "reason": "restock"}
# reason: restock | correction | damaged | return
```

**Query params:**

- `limit` — Max items per page (default 100, max 500)
- `cursor` — Pagination cursor (SKU of last item)
- `low_stock` — Filter items with ≤10 available

### Checkout (public)

```bash
# Create cart
POST /v1/carts
{"customer_email": "buyer@example.com"}

# Get cart
GET /v1/carts/{id}

# Add items to cart (replaces existing items)
POST /v1/carts/{id}/items
{"items": [{"sku": "TEE-BLK-M", "qty": 2}]}

# Checkout → returns Stripe URL
POST /v1/carts/{id}/checkout
{
  "success_url": "https://...",
  "cancel_url": "https://...",
  "collect_shipping": true,
  "shipping_countries": ["US", "CA", "GB"]
}
```

**Checkout options:**

- `collect_shipping` — Enable shipping address collection
- `shipping_countries` — Allowed countries (default: `["US"]`)
- `shipping_options` — Custom shipping rates (optional, has sensible defaults)

Automatic tax calculation is enabled via Stripe Tax.

### Customers (admin)

```bash
# List customers (with pagination and search)
GET /v1/customers?limit=20&cursor=...&search=john@example.com

# Get customer with addresses
GET /v1/customers/{id}

# Get customer's order history
GET /v1/customers/{id}/orders

# Update customer
PATCH /v1/customers/{id}
{"name": "John Doe", "phone": "+1234567890"}

# Add address
POST /v1/customers/{id}/addresses
{"line1": "123 Main St", "city": "NYC", "postal_code": "10001"}

# Delete address
DELETE /v1/customers/{id}/addresses/{addressId}
```

Customers are automatically created from Stripe checkout sessions (guest checkout by email).

### Orders (admin)

```bash
# List orders (with pagination and filters)
GET /v1/orders?limit=20&cursor=...&status=shipped&email=customer@example.com

# Get order details
GET /v1/orders/{id}

# Update order status/tracking
PATCH /v1/orders/{id}
{"status": "shipped", "tracking_number": "1Z999...", "tracking_url": "https://..."}

# Refund order
POST /v1/orders/{id}/refund
{"amount_cents": 1000}  # optional, omit for full refund

# Create test order (skips Stripe, for testing)
POST /v1/orders/test
{"customer_email": "test@example.com", "items": [{"sku": "TEE-BLK-M", "qty": 1}]}
```

**Order statuses:** `pending` → `paid` → `processing` → `shipped` → `delivered` | `refunded` | `canceled`

### Images (admin)

```bash
# Upload image
POST /v1/images
Content-Type: multipart/form-data
file: <image file>
# Returns: {"url": "...", "key": "..."}

# Delete image
DELETE /v1/images/{key}
```

### Setup (admin)

```bash
# Connect Stripe
POST /v1/setup/stripe
{"stripe_secret_key": "sk_...", "stripe_webhook_secret": "whsec_..."}
```

### Outbound Webhooks (admin)

```bash
# List webhooks
GET /v1/webhooks

# Create webhook
POST /v1/webhooks
{"url": "https://your-server.com/webhook", "events": ["order.created", "order.shipped"]}

# Get webhook (includes recent deliveries)
GET /v1/webhooks/{id}

# Update webhook
PATCH /v1/webhooks/{id}
{"events": ["*"], "status": "paused"}

# Rotate secret
POST /v1/webhooks/{id}/rotate-secret

# Delete webhook
DELETE /v1/webhooks/{id}
```

**Events:** `order.created`, `order.updated`, `order.shipped`, `order.refunded`, `inventory.low`

**Wildcards:** `order.*` or `*` for all events

Payloads are signed with HMAC-SHA256. Verify with the `X-Merchant-Signature` header.

## UCP (Universal Commerce Protocol)

Merchant implements the [Universal Commerce Protocol](https://ucp.dev) for AI agent-to-commerce interoperability. UCP enables AI agents to discover, browse, and transact with any UCP-compliant merchant through a standard protocol.

### UCP Discovery

```bash
# Get UCP profile with capabilities, services, and payment handlers
GET /.well-known/ucp
```

Response includes:
- **Capabilities**: `dev.ucp.shopping.checkout`, `dev.ucp.common.identity_linking`, `dev.ucp.shopping.order`
- **Services**: REST endpoints for shopping operations
- **Payment Handlers**: Stripe Checkout (redirect-based)

### UCP Checkout Flow (for AI agents)

```bash
# 1. Create checkout session
POST /ucp/v1/checkout-sessions
{
  "currency": "USD",
  "line_items": [
    {"item": {"id": "TEE-BLK-M"}, "quantity": 2}
  ],
  "buyer": {"email": "buyer@example.com"}
}

# 2. Complete checkout (returns Stripe redirect URL)
POST /ucp/v1/checkout-sessions/{id}/complete
{
  "payment_data": {
    "handler_id": "stripe_checkout",
    "success_url": "https://your-app.com/success",
    "cancel_url": "https://your-app.com/cancel"
  }
}

# 3. Agent presents continue_url to user for payment
```

### UCP Checkout Session Lifecycle

| Status | Description |
|--------|-------------|
| `incomplete` | Session created, items may have validation errors |
| `requires_escalation` | Human interaction needed (payment redirect) |
| `ready_for_complete` | Session can be completed |
| `complete_in_progress` | Payment processing |
| `completed` | Order created successfully |
| `canceled` | Session canceled |

### UCP Endpoints

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/.well-known/ucp` | Profile discovery |
| POST | `/ucp/v1/checkout-sessions` | Create checkout |
| GET | `/ucp/v1/checkout-sessions/:id` | Get checkout |
| PUT | `/ucp/v1/checkout-sessions/:id` | Update checkout |
| POST | `/ucp/v1/checkout-sessions/:id/complete` | Complete checkout |
| DELETE | `/ucp/v1/checkout-sessions/:id` | Cancel checkout |

All UCP responses include a `ucp` envelope with version and active capabilities.

## OAuth 2.0 (for platforms)

Merchant supports OAuth 2.0 for platforms to act on behalf of customers. **Zero configuration required** — works out of the box.

### Discovery

```bash
GET /.well-known/oauth-authorization-server
```

### Authorization Flow (PKCE required)

```bash
# 1. Redirect user to authorize
GET /oauth/authorize?
  client_id=your-app&
  redirect_uri=https://your-app.com/callback&
  response_type=code&
  scope=openid%20profile%20checkout&
  code_challenge=BASE64URL(SHA256(verifier))&
  code_challenge_method=S256&
  state=random-state

# 2. User authenticates via magic link (email)

# 3. Exchange code for tokens
POST /oauth/token
Content-Type: application/x-www-form-urlencoded

grant_type=authorization_code&
code=AUTH_CODE&
redirect_uri=https://your-app.com/callback&
client_id=your-app&
code_verifier=ORIGINAL_VERIFIER
```

### Scopes

| Scope | Access |
|-------|--------|
| `openid` | Verify identity |
| `profile` | Name and email |
| `checkout` | Create orders on behalf of user |
| `orders.read` | View order history |
| `orders.write` | Manage orders |
| `addresses.read` | Access saved addresses |
| `addresses.write` | Manage addresses |

### Using Access Tokens

```bash
curl https://your-store.com/v1/orders \
  -H "Authorization: Bearer ACCESS_TOKEN"
```

Tokens work alongside API keys — existing integrations are unaffected.

## Stripe Webhooks

Set your Stripe webhook endpoint to `https://your-domain/v1/webhooks/stripe`

Events handled:

- `checkout.session.completed` → Creates order, deducts inventory

For local development:

```bash
stripe listen --forward-to localhost:8000/v1/webhooks/stripe
```

## Rate Limiting

All endpoints return rate limit headers:

- `X-RateLimit-Limit` — Requests allowed per window
- `X-RateLimit-Remaining` — Requests remaining
- `X-RateLimit-Reset` — Unix timestamp when window resets

Limits are configurable in `backend/app/lib/rate_limit.py`.

## Admin Dashboard

```bash
cd admin && npm install && npm run dev
```

Connect with API URL `http://localhost:8000` and admin key (`sk_...`). Features: orders, inventory, products, webhooks.

## Example Store

Vanilla JS demo storefront. See [`example/README.md`](example/README.md).

```bash
cd example && npm install && npm run dev
cp src/api.example.js src/api.js   # set PUBLIC_KEY=pk_...
```

Open http://localhost:3000

## Architecture

```
backend/
├── app/
│   ├── main.py           # FastAPI entrypoint
│   ├── config.py         # Settings
│   ├── db/models.py      # SQLAlchemy models
│   ├── deps/auth.py      # API key + OAuth auth
│   ├── routers/          # REST, UCP, OAuth, MCP
│   ├── services/         # Business logic
│   └── workers/          # APScheduler jobs
├── scripts/init.py       # API key bootstrap
└── scripts/seed.py       # Demo data
```

## Stack

| Component | Technology |
| --------- | ------------ |
| Runtime   | Python 3.12 + FastAPI |
| Database  | SQLite (dev) / PostgreSQL (prod) |
| Payments  | Stripe |
| Agent protocols | UCP + MCP (RAILS AI) |
| Identity  | OAuth 2.0 + PKCE |
| Images    | Local filesystem (Docker volume) |
| Scheduler | APScheduler (cart cleanup) |

## Scaling

PostgreSQL is recommended for production (`docker compose up`). Use `schema-postgres.sql` as reference for managed database setups. Horizontal scaling: run multiple API containers behind a load balancer with shared Postgres and image storage.

## License

MIT — Copyright (c) 2025-2026 RAILS AI Organization. See [LICENSE](LICENSE).

## Contributing
Open Source code contributions, PR's and raising issues or Enhancements are welcomed.

## Citation

If you use this work, please cite:

```bibtex
@misc{sheriff2026beyond,
  title         = {Beyond Malicious Pixels: Visual Prompt Injection in Agentic Commerce},
  author        = {Sheriff, Akram},
  year          = {2026},
  eprint        = {XXXX.XXXXX},
  archivePrefix = {arXiv},
  primaryClass  = {cs.AI},
  note          = {RAILS AI Agentic Security Research},
  url           = {https://arxiv.org/abs/XXXX.XXXXX}
}
```

**Plain-text:**
Akram Sheriff. *Beyond Malicious Pixels: Visual Prompt Injection in Agentic Commerce.* arXiv preprint arXiv:XXXX.XXXXX, 2026.

See [CONTRIBUTING.md](CONTRIBUTING.md). Security reports: [SECURITY.md](SECURITY.md).
