# Changelog

All notable changes to this project are documented here.

## 1.0.0 (2026-06-15)

### Major rewrite — Python + RAILS AI

Complete refactor from TypeScript/Cloudflare Workers to **Python 3.12 + FastAPI**, aligned with the [RAILS AI](https://railsai.ghe.com/RAILS-AI) agentic commerce platform.

### Added

- **FastAPI backend** (`backend/`) with SQLite (dev) and PostgreSQL (prod)
- **REST API** — full `/v1/*` commerce surface (products, inventory, carts, orders, customers, discounts, webhooks, images)
- **UCP** — Universal Commerce Protocol (`/.well-known/ucp`, `/ucp/v1/checkout-sessions`)
- **OAuth 2.0 + PKCE** — `/.well-known/oauth-authorization-server`, `/oauth/*`
- **MCP endpoint** — `POST /api/merchant/mcp` for RAILS marketplace agent tools
- **RAILS discovery** — `GET /.well-known/merchant`
- **Docker Compose** — PostgreSQL 16 + API container
- **RAILS marketplace adapter** — `merchant` platform in [RAILS-AI/rails-app](https://railsai.ghe.com/RAILS-AI/rails-app)
- Init/seed scripts (`backend/scripts/init.py`, `seed.py`)
- CI workflow (pytest + Docker build)

### Removed

- Cloudflare Workers / Hono / Durable Objects runtime (`src/`)
- Wrangler deployment config
- WebSocket real-time endpoint (planned for a future release)

### Migration from 0.x (TypeScript)

1. Export any data you need from the old deployment
2. Deploy the Python backend (`docker compose up` or `uvicorn`)
3. Run `python scripts/init.py` — API keys must be regenerated
4. Reconnect Stripe via `POST /v1/setup/stripe`
5. Point admin dashboard and storefront to port `8000`

---

## 0.2.0 (2025-01-11) — Legacy TypeScript

> Superseded by 1.0.0. See git history for the Cloudflare Workers implementation.

- Durable Objects + embedded SQLite
- UCP and OAuth 2.0 (TypeScript)
- WebSocket support (not ported to Python in 1.0.0)
