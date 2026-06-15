# Publishing Checklist — RAILS AI Organization

Use this checklist before publishing **RAILS-AI/merchant** on GitHub Enterprise.

## Repository setup

- [ ] Create repo `RAILS-AI/merchant` on `railsai.ghe.com`
- [ ] Set description: *Open-source commerce backend for Stripe — RAILS AI agent-ready (UCP + MCP)*
- [ ] Enable Issues and Discussions (optional)
- [ ] Add topics: `commerce`, `stripe`, `fastapi`, `ucp`, `mcp`, `rails-ai`, `agentic-commerce`

## Artifacts in this repo

| File | Status | Purpose |
|------|--------|---------|
| `LICENSE` | Ready | MIT — RAILS AI Organization |
| `README.md` | Ready | Quick start, API reference, RAILS integration |
| `CONTRIBUTING.md` | Ready | Contribution guidelines |
| `SECURITY.md` | Ready | Vulnerability reporting |
| `CHANGELOG.md` | Ready | v1.0.0 Python release notes |
| `DEPLOYMENT.md` | Ready | Docker Compose + production |
| `integrations/RAILS.md` | Ready | Two-repo marketplace integration |
| `.github/workflows/ci.yml` | Ready | pytest + Docker build |
| `backend/tests/` | Ready | Smoke test suite |
| `.env.example` | Ready | Environment template (no secrets) |
| `.gitignore` | Ready | Excludes `.venv`, `.env`, `api.js` |

## Pre-publish verification

```bash
# 1. Tests
cd backend && pip install -r requirements.txt -r requirements-dev.txt
PYTHONPATH=. pytest -q

# 2. Docker
docker compose build
docker compose up -d && curl -sf http://localhost:8000/health

# 3. No secrets committed
git grep -E 'sk_live|sk_test_[a-zA-Z0-9]{20,}|whsec_[a-zA-Z0-9]{20,}' -- ':!*.example' ':!.env.example' ':!README.md' ':!DEPLOYMENT.md'
```

## Related repo

Publish or verify the **merchant platform adapter** in **RAILS-AI/rails-app**:

```
app/api/mcp-transport/adapters/merchant/
```

Marketplace users need both repos for conversational checkout.

## CI on GHE

The included workflow uses `ubuntu-latest`. For GHE without hosted runners, register a self-hosted runner on `RAILS-AI/merchant` and change `runs-on` in `.github/workflows/ci.yml` to `self-hosted` (same pattern as rails-app).

## Post-publish

- [ ] Announce in RAILS AI internal docs / `#commerce` channel
- [ ] Link from rails-app README marketplace section
- [ ] Tag release `v1.0.0`
