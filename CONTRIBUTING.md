# Contributing to Merchant

Thank you for contributing to **Merchant** — the open-source commerce backend for the [RAILS AI](https://railsai.ghe.com/RAILS-AI) agentic commerce platform.

## Getting started

```bash
git clone https://railsai.ghe.com/RAILS-AI/merchant.git
cd merchant/backend
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt -r requirements-dev.txt
cp .env.example .env
PYTHONPATH=. python scripts/init.py
PYTHONPATH=. uvicorn app.main:app --reload --port 8000
```

## Development workflow

1. **Fork** the repository on GitHub Enterprise (`railsai.ghe.com`)
2. **Create a branch** from `main`: `git checkout -b feat/your-feature`
3. **Make changes** — keep diffs focused; match existing Python/FastAPI style
4. **Run tests**: `cd backend && PYTHONPATH=. pytest -q`
5. **Open a pull request** against `main` with a clear description

## Code guidelines

- Python 3.12+; type hints where practical
- Match patterns in `backend/app/routers/` and `backend/app/lib/`
- API responses must stay compatible with existing `/v1/*` clients (admin dashboard, example store, RAILS marketplace adapter)
- No secrets in code or commits — use `.env` / environment variables
- Prefer minimal, focused changes over large refactors

## Testing

```bash
cd backend
pip install -r requirements-dev.txt
PYTHONPATH=. pytest -v
```

CI runs the same suite on every push to `main`.

## Project structure

| Path | Purpose |
|------|---------|
| `backend/app/` | FastAPI application |
| `backend/scripts/` | Init and seed utilities |
| `admin/` | React admin dashboard |
| `example/` | Vanilla JS demo storefront |
| `integrations/` | RAILS marketplace integration docs |

## RAILS marketplace adapter

The `merchant` platform adapter for conversational commerce lives in the **[RAILS-AI/rails-app](https://railsai.ghe.com/RAILS-AI/rails-app)** repository:

```
app/api/mcp-transport/adapters/merchant/
```

Changes that affect marketplace integration should be coordinated across both repos. See [`integrations/RAILS.md`](integrations/RAILS.md).

## Questions

- **Issues**: [RAILS-AI/merchant/issues](https://railsai.ghe.com/RAILS-AI/merchant/issues)
- **Security**: see [SECURITY.md](SECURITY.md) — do not file public issues for vulnerabilities
