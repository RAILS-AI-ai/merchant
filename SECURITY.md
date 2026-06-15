# Security Policy

## Supported versions

| Version | Supported |
|---------|-----------|
| 1.0.x   | Yes       |
| 0.x (TypeScript / Cloudflare) | No — end of life |

## Reporting a vulnerability

**Do not** open a public GitHub issue for security vulnerabilities.

Report privately to the RAILS AI security team:

- **Email**: security@rails.ai
- **GHE**: [RAILS-AI/merchant security advisories](https://railsai.ghe.com/RAILS-AI/merchant/security/advisories) (if enabled on your instance)

Include:

- Description of the vulnerability
- Steps to reproduce
- Impact assessment
- Suggested fix (if any)

We aim to acknowledge reports within **72 hours** and provide a remediation timeline within **14 days** for confirmed issues.

## Security practices for operators

### API keys

- `pk_...` — public; safe for storefronts and RAILS marketplace config
- `sk_...` — admin; **never** expose in client-side code, git, or logs
- Rotate keys by re-running `scripts/init.py` on a fresh deployment (no key rotation API yet)

### Stripe

- Store `STRIPE_SECRET_KEY` and `STRIPE_WEBHOOK_SECRET` in environment variables only
- Use `POST /v1/setup/stripe` to persist keys in the database config table — protect database access accordingly
- Verify Stripe webhooks via the `stripe-signature` header (enforced in `/v1/webhooks/stripe`)

### Production deployment

- Change default Postgres credentials in `docker-compose.yml` before exposing to the internet
- Set `ENVIRONMENT=production` — disables `/docs` and `/openapi.json`
- Configure `CORS_ORIGINS` to your admin and marketplace domains only (do not use wildcards in production)
- Run behind HTTPS (reverse proxy or load balancer)
- Keep dependencies updated: `pip install -r requirements.txt --upgrade`

### OAuth

- PKCE (`S256`) is required for authorization code flow
- Magic-link auth is displayed on-screen in development — integrate an email provider before production customer OAuth

### Rate limiting

In-memory rate limits apply per API key/IP. For high-traffic production, add edge rate limiting (nginx, Cloudflare, API gateway).

## Scope

This policy covers the **merchant** repository (`backend/`, `admin/`, `example/`). The RAILS marketplace adapter in **RAILS-AI/rails-app** has its own security surface — see that repo's `backend/docs/SECURITY.md`.
