# DRISHTI Pre-Launch Security Checklist

## CRITICAL (must-fix before go-live)

- [ ] **User.role column exists** — verified in `api/models/schema.py:32`
- [ ] **RateLimitMiddleware registered** — verified in `api/main.py:45`
- [ ] **HMAC fail-closed** — `SHOPIFY_WEBHOOK_SECRET` required in production
- [ ] **No hardcoded passwords** — `drishti_secret` removed from defaults
- [ ] **JWT_SECRET strong** — validated at startup (>=32 chars, non-placeholder)

## HIGH (must-fix before go-live)

- [ ] **Replace hand-rolled JWT with python-jose** — `python-jose` is in requirements.txt but unused
- [ ] **Replace SHA-256 with bcrypt** — `passlib[bcrypt]` is in requirements but unused
- [ ] **Add auth to non-public endpoints** — catalog, look, vton, pricing
- [ ] **Add BOLA protection** — ownership checks on session/look/vton resources
- [ ] **Add quality_score column to VTONJob** — currently missing, causes AttributeError

## MEDIUM (should-fix before go-live)

- [ ] **CORS origins configurable via env var** — currently hardcoded
- [ ] **Remove localhost CORS in production** — conditional on ENV
- [ ] **Add HMAC to completion webhooks** — `/dtf-completion`, `/vton-completion`
- [ ] **Implement OTP delivery** — currently stubbed (never sent)
- [ ] **Shorter JWT expiry** — 7 days is long; consider 15-60 min + refresh tokens

## LOW (nice-to-have)

- [ ] **Add Retry-After headers** to 429 responses
- [ ] **Add per-endpoint rate limits** — stricter on OTP/auth endpoints
- [ ] **Request body size limits** — global middleware (1MB JSON, 10MB uploads)
- [ ] **SSRF protection** — allowlist image URLs in catalog sync

## Automated Security Checks (CI)

| Check | Tool | Frequency |
|-------|------|-----------|
| Dependency scanning | pip-audit | Every PR + weekly |
| Secret scanning | gitleaks | Every PR + weekly |
| SAST | CodeQL | Every PR + weekly |
| Container scanning | Trivy | Every PR |
| Auth integration tests | pytest | Every PR |

## Manual Pre-Launch Tasks

1. **Penetration test** — hire external firm for API pen test
2. **Load test on staging** — run k6 suite, capture real GPU p50/p95
3. **Verify Sentry in prod** — set SENTRY_DSN, trigger test error, confirm alert
4. **Verify Grafana dashboards** — import dashboards, confirm metrics flow
5. **Rotate all secrets** — generate fresh JWT_SECRET, SHOPIFY_WEBHOOK_SECRET, DB passwords
6. **Review .gitignore** — ensure .env, *.pem, __pycache__ are excluded
7. **Set up alerts** — PagerDuty/OpsGenie for 5xx spike, GPU OOM, queue depth > 10
8. **Document runbooks** — incident response, rollback, secret rotation

## Security Contacts

- Report vulnerabilities to: security@mynarrative.in
- Emergency rotation: see `docs/runbooks/secret-rotation.md`
