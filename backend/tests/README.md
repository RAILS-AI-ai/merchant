# Tests

Smoke tests for the Merchant API. Run from `backend/`:

```bash
pip install -r requirements-dev.txt
PYTHONPATH=. pytest -q
```

CI runs the same suite via `.github/workflows/ci.yml`.
