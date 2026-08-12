# Secrets Management Plan — AI Trading Platform

## 1. Problem

Right now, `ALPACA_API_KEY`, `ALPACA_SECRET_KEY`, `FINNHUB_API_KEY`, `NEWSAPI_KEY`, and `DISCORD_WEBHOOK_URL` live in a local `.env` file on one machine. This has three problems for a two-person team:

- Every teammate has to manually recreate the same `.env` by hand, copy-pasting secrets over chat/email — exactly the risk we flagged earlier in this project.
- There's no single place to rotate a leaked or expiring key — you'd have to message everyone to update their local file.
- There's no audit trail of who has access to what, or when a secret was last changed.

The fix: move secrets into a dedicated secrets manager that exposes an API/SDK, and have the application fetch secrets at runtime (or at process startup) instead of reading a static `.env` file.

## 2. Tool choice: Doppler

**Recommendation: [Doppler](https://www.doppler.com)** — free for up to 3 users, which comfortably covers a 2-person team with room to add a third collaborator later.

| Requirement | Doppler fit |
|---|---|
| API to retrieve secrets programmatically | Yes — REST API + official Python SDK (`doppler-sdk`) + CLI |
| Team sharing without pasting secrets in chat | Yes — invite by email, role-based access |
| Local dev workflow | `doppler run -- python src/smoke_test.py` injects secrets as env vars, no code change needed |
| Free tier | Free for 3 users, unlimited secrets, unlimited projects |
| Setup time | Near-zero ops — hosted, no infrastructure to run |
| Environments (dev/staging/prod-like separation) | Built-in: Doppler has "configs" per environment (e.g. `dev`, `paper`, `live`) |

**Alternative considered: [Infisical](https://infisical.com)** — open-source (MIT), self-hostable, free for up to 5 users, adds SSH/cert management. Slightly more setup overhead since self-hosting means running the service yourself (or using their hosted free tier, which is comparable to Doppler). Worth switching to later if the project outgrows Doppler's free tier or you want everything self-hosted for a portfolio/resume story. For now, Doppler's zero-ops setup is the better fit for a fast-moving 2-person project.

Sources consulted: [Infisical vs Doppler comparison](https://securityboulevard.com/2026/07/infisical-vs-doppler-which-secrets-manager-is-right-for-your-team/), [Doppler review 2026](https://devopsboys.com/blog/doppler-secrets-manager-review-2026), [secrets tools comparison](https://guptadeepak.com/top-5-secrets-management-tools-hashicorp-vault-aws-doppler-infisical-and-azure-key-vault-compared/).

## 3. Target architecture

```
Doppler (cloud, hosted)
  └── Project: smc-trading-agent
        └── Config: dev  (each teammate's local environment)
              ├── ALPACA_API_KEY
              ├── ALPACA_SECRET_KEY
              ├── ALPACA_BASE_URL
              ├── FINNHUB_API_KEY
              ├── NEWSAPI_KEY
              └── DISCORD_WEBHOOK_URL
        └── Config: prod (reserved for Phase 10 go-live, separate real-money-safe keys)

Local machine (each teammate)
  └── doppler CLI authenticated → `doppler run -- <command>`
        injects the above as real environment variables for that process only,
        nothing written to disk, nothing committed to git.

Application code
  └── os.getenv("ALPACA_API_KEY") — UNCHANGED. The app still just reads
      environment variables; Doppler is what populates them at runtime.
```

Key point: this is a low-disruption change. `smoke_test.py` and every future module still call `os.getenv(...)` exactly as they do today — only *how* those environment variables get set changes, from "read a local `.env` file" to "injected by Doppler at process start."

## 4. Setup steps

### One-time (whoever sets up the Doppler project — you)

1. Sign up at dashboard.doppler.com (free).
2. Create a project: `smc-trading-agent`.
3. Create a config named `dev`.
4. Add the 6 secrets (`ALPACA_API_KEY`, `ALPACA_SECRET_KEY`, `ALPACA_BASE_URL`, `FINNHUB_API_KEY`, `NEWSAPI_KEY`, `DISCORD_WEBHOOK_URL`) with your existing values, either via the dashboard or:
   ```bash
   doppler secrets set ALPACA_API_KEY --config dev
   ```
5. Invite your teammate to the Doppler project (Doppler dashboard → Project → Access → Invite by email). They get read/write access without ever seeing the raw values pasted in Slack/email — Doppler handles the sharing.

### Per-teammate (each person, once)

```bash
# Install the Doppler CLI (macOS)
brew install dopplerhq/cli/doppler

# Authenticate (opens a browser to log in / accept the invite)
doppler login

# Link this local repo folder to the Doppler project + config
cd AI-Trading-Platform
doppler setup   # select project "smc-trading-agent", config "dev"
```

After `doppler setup`, no `.env` file is needed on that machine at all. Delete the local `.env` (it stays gitignored regardless, but Doppler makes it unnecessary).

### Running the app

Instead of:
```bash
python src/smoke_test.py
```

Run:
```bash
doppler run -- python src/smoke_test.py
```

Doppler injects the secrets as real process environment variables for the duration of that command only — `os.getenv("ALPACA_API_KEY")` in the code sees them exactly as it did with `.env`, so **no application code changes required**.

## 5. Programmatic access (the "hit the API to retrieve it" path)

For cases where you want secrets fetched inside Python directly (e.g., a deployed dashboard on Streamlit Cloud where you can't run `doppler run`), use the Doppler REST API / SDK with a **service token** (scoped, read-only, revocable — not your personal login):

```bash
pip install doppler-sdk
```

```python
# src/config/secrets.py
import os
from doppler_sdk import DopplerSDK

def load_secrets_from_doppler() -> None:
    """Fetch secrets from Doppler and populate os.environ, so the rest
    of the app can keep using os.getenv() unchanged."""
    doppler_token = os.environ["DOPPLER_TOKEN"]  # the one credential you still set manually (service token)
    sdk = DopplerSDK()
    sdk.set_access_token(doppler_token)

    secrets = sdk.secrets.list(project="smc-trading-agent", config="dev").secrets
    for key, value in secrets.items():
        os.environ[key] = value["computed"]
```

Call `load_secrets_from_doppler()` once at the very top of `src/smoke_test.py` (and later, `src/scheduler/run_loop.py` and `src/dashboard/app.py`) before any other code reads `os.getenv(...)`.

This means the **only** secret you ever need to configure manually per-deployment is `DOPPLER_TOKEN` itself (a single service token, scoped read-only to the `dev` or `prod` config) — set once as an environment variable in Streamlit Cloud's secrets manager, your CI, or wherever the app runs. Every other credential comes from the API call above.

## 6. Migration checklist

- [ ] Create Doppler account + `smc-trading-agent` project + `dev` config
- [ ] Move the 6 existing `.env` values into Doppler
- [ ] Invite teammate to the Doppler project
- [ ] Both teammates run `doppler login` + `doppler setup` locally
- [ ] Update `src/smoke_test.py` to call `load_secrets_from_doppler()` (or simply run it via `doppler run --`)
- [ ] Add `src/config/secrets.py` helper module
- [ ] Add `doppler-sdk` to `requirements.txt`
- [ ] Delete local `.env` files once Doppler is confirmed working (keep `.env.example` for reference/onboarding)
- [ ] Document the new `doppler run -- <command>` workflow in `README.md`
- [ ] When Phase 10 (go-live) arrives, create a separate `prod` config in Doppler with real-money-safe keys, isolated from `dev`

## 7. Non-goals / out of scope for now

- Secret rotation automation — manual rotation via Doppler dashboard is fine at this scale.
- Dynamic/short-lived secrets (Vault-style) — unnecessary complexity for a 2-person paper-trading project.
- Self-hosting (Infisical) — revisit only if Doppler's free tier becomes limiting.
