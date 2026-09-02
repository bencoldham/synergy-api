# Repository instructions

## Supported commands

```console
python -m pip install -e '.[dev]'
python -m playwright install chromium
python -m pytest
python -m ruff check src tests
python -m mypy src
```

Run the real-service smoke test only with an explicit opt-in and dedicated-account secrets:

```console
python -m pytest --run-live -m live tests/live/test_synergy_live.py
```

## Architecture

- The core package must never import Home Assistant.
- `SynergyClient` is the only class that owns an HTTP session. Prefer functions and immutable data classes elsewhere.
- Playwright is restricted to login, optional OTP submission, and minting transferable direct-HTTP credentials. Close the page, context, browser, and Playwright before any usage request.
- Authentication and credential handoff belong in `auth.py`; Gmail-only IMAP logic belongs in `otp.py`; direct Aura transport and normalization belong in `usage.py`; SQLite logic belongs in `storage.py`.
- `usage.py` must not import Playwright or accept Playwright page, browser context, or request objects.
- Do not add transport, repository, authentication-provider, or mailbox-provider interfaces without a real second implementation. Gmail host, port, TLS, and `INBOX` are fixed internal behavior.
- Session expiration permits exactly one Playwright credential remint and one direct HTTP replay.
- `get_usage()` has no database side effects. `sync_usage_to_db()` is the explicit fetch-and-persist operation.

## Data and security invariants

- Query ranges use portal-local `[start, end)` dates.
- Normalized timestamps are UTC-aware, quantities are exact `Decimal` kWh values, provider identifiers remain strings, and output ordering is deterministic.
- Reject conflicting duplicates and schema drift rather than guessing, clamping, rounding, or silently merging a new channel.
- Never log, commit, or persist Synergy credentials, Gmail app passwords, OTPs, cookies, Aura tokens/contexts, mailbox bodies, raw provider responses, HARs, traces, screenshots, videos, Playwright storage state, or browser profiles.
- Provider fixtures must be hand-authored from a minimal sanitized schema and manually reviewed. Never commit raw captures or copied request/session values.
- Keep direct HTTP and browser authentication state memory-only and discard it on close or reauthentication.
- CAPTCHA and unfamiliar authentication challenges must fail closed; never bypass them.

## Testing

- Default tests must be offline and require no secrets.
- Real Synergy and Gmail access is allowed only in `tests/live` with `--run-live` and a dedicated personal Gmail/Synergy account.
- Provider protocol changes require refreshing the minimal sanitized fixtures and affected contract tests.
- Behavioral fixes require a focused reproduction and verification. Run the formatter/linter/type checker once after implementation, not between parallel edits.
