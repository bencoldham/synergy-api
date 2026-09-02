# wa-synergy

Synchronous Python client for retrieving normalized half-hour electricity usage from WA Synergy My Account and optionally persisting it to SQLite.

This is an unofficial client for a provider-owned web contract. It uses Playwright only to authenticate, then closes the browser and performs account discovery and usage retrieval directly with `httpx`. CAPTCHA is not bypassed or solved.

## Requirements

- Python 3.11 or newer
- Chromium installed through Playwright
- A graphical Wayland or X11 session for the default interactive authentication mode
- A dedicated personal `@gmail.com` account used as the Synergy login address
- Google 2-Step Verification and a Gmail app password
- Network access to Synergy My Account and Gmail IMAP

The library supports only Gmail's fixed TLS IMAP endpoint (`imap.gmail.com:993`) and read-only `INBOX`. Google Workspace accounts, alternate mailbox providers, OAuth mailbox access, configurable IMAP servers, and CAPTCHA solving are not supported.

## Installation

Install the package and its Playwright/httpx dependencies, then install Chromium:

```console
python -m pip install .
python -m playwright install chromium
```

For development:

```console
python -m pip install -e '.[dev]'
python -m playwright install chromium
```

On hosts that need Playwright-managed operating-system dependencies, follow Playwright's platform instructions or use `python -m playwright install --with-deps chromium` with the privileges appropriate for that host.

## Dedicated Gmail and Synergy setup

Use a new personal Gmail mailbox only for Synergy. Do not use a mailbox containing unrelated email.

1. Create a personal `@gmail.com` account.
2. Enable Google Account **2-Step Verification**. Do not configure the account exclusively with security keys, because that prevents app-password use.
3. Open [Google App Passwords](https://myaccount.google.com/apppasswords), create an app password named `wa-synergy`, and save the generated 16-character value. Supply this value to the client, never the normal Google Account password.
4. In Synergy My Account, change both the login and notification email to this dedicated Gmail address and complete Synergy's verification flow.
5. Sign out and back in once to verify that Synergy's one-time-passcode message reaches Gmail `INBOX`.
6. If the message lands in Spam, create a Gmail filter for the exact Synergy sender and select **Never send it to Spam**. The client never reads Spam.

Personal Gmail IMAP is already enabled. Changing the Google Account password revokes existing app passwords; create a replacement and update the runtime secret when rotating it.

Keep credentials in environment variables or a secret manager. The library deliberately performs no environment loading:

```text
WA_SYNERGY_EMAIL=<dedicated Gmail address>
WA_SYNERGY_PASSWORD=<Synergy password>
WA_SYNERGY_GMAIL_APP_PASSWORD=<Google app password>
```

Construct `SynergyCredentials` in application code after reading the chosen secret source. Both password fields and the email address are excluded from its representation.

## Usage

`UsageQuery` uses portal-local (`Australia/Perth`) calendar dates with `[start, end)` semantics: `start` is included and `end` is excluded. Empty account and service-point filters discover and fetch every accessible active electricity service; the client never silently selects the first service.

```python
import os
from pathlib import Path

from wa_synergy import (
    SynergyClient,
    SynergyCredentials,
    UsageQuery,
    sync_usage_to_db,
)

credentials = SynergyCredentials(
    email=os.environ["WA_SYNERGY_EMAIL"],
    password=os.environ["WA_SYNERGY_PASSWORD"],
    gmail_app_password=os.environ["WA_SYNERGY_GMAIL_APP_PASSWORD"],
)
query = UsageQuery(
    start="2026-07-01",
    end="2026-08-01",
    # account_ids=("provider-account-id",),
    # service_point_ids=("provider-service-id",),
)

with SynergyClient(credentials=credentials) as client:
    intervals = client.get_usage(query)

    result = sync_usage_to_db(
        client=client,
        query=query,
        db_path=Path("data/synergy.sqlite3"),
    )

print(len(intervals), result.inserted, result.updated, result.unchanged)
```

`get_usage()` returns an immutable tuple of `UsageInterval` values and has no database side effects. Each interval contains:

- provider account and service-point identifiers preserved as strings;
- optional meter identifier and an explicit tariff/solar `channel`;
- timezone-aware UTC start and end timestamps for one 30-minute interval;
- exact `Decimal` consumption in canonical `kWh`;
- optional provider quality and source-update metadata.

Results are deterministic and sorted. Exact duplicates collapse; conflicting duplicates, unknown channels, malformed records, and contract drift are rejected rather than guessed. Raw provider responses are neither returned nor persisted.

`sync_usage_to_db()` fetches and normalizes the complete range before opening the SQLite write transaction. Its `SyncResult` reports inserted, updated, and unchanged normalized rows.

## Authentication and direct transport

A new `SynergyClient` has no authenticated state. By default, its first usage operation:

1. opens a visible, temporary Chromium instance;
2. opens a certificate-verified, read-only Gmail IMAP connection and records a freshness boundary;
3. submits the Synergy credentials using the login page;
4. if Synergy returns its transient refresh error, reloads the page and refills the credentials so the user can physically click **Log in**;
5. selects the email OTP method, then reads and submits one fresh, exact-match Synergy OTP;
6. extracts the minimum transferable Salesforce `sid`, Aura token, and Aura context from the authenticated dashboard;
7. closes the page, browser context, browser, and IMAP connection; and
8. creates an in-memory `httpx.Client` for direct account discovery and Aura usage requests.

The physical click is required when Synergy rejects browser-dispatched submissions through its CAPTCHA integration. The client does not synthesize human input or weaken browser automation detection. `SynergyClient(..., interactive_auth=False)` uses headless Chromium and a bounded automatic retry, but cannot pass a provider check that requires physical interaction.

No usage request is sent through Playwright, its page, browser context, or request APIs. The reusable HTTP session and Aura material remain in memory and are discarded on close or refresh. The client serializes operations. A recognized expired-session response causes exactly one browser credential remint and one replay of the original direct request; a second expiry raises `SessionExpiredError`.

The browser is a required authentication dependency even though it is never the usage transport. Traces, screenshots, videos, HAR files, storage-state files, and persistent browser profiles are not produced.

### Unsupported CAPTCHA

Synergy can present a provider-controlled CAPTCHA during login. The client does not attempt to evade, automate, or bypass it; it raises `UnsupportedAuthChallenge` immediately. Complete any provider-required account action outside this library and retry only when normal login is available. Unknown authentication challenges and changed portal controls also fail closed.

## SQLite schema

The caller must provide an explicit filesystem path. The parent directory and database are created when absent, with restrictive creation permissions where the platform supports them. Schema versioning uses `PRAGMA user_version`; the current version is `1`.

Table `usage_intervals`:

| Column | SQLite type | Meaning |
|---|---|---|
| `account_id` | `TEXT NOT NULL` | Provider account identifier |
| `service_point_id` | `TEXT NOT NULL` | Provider service identifier |
| `meter_id` | `TEXT` | Provider meter identifier when attributable |
| `channel` | `TEXT NOT NULL` | Provider tariff or solar channel |
| `interval_start_utc` | `TEXT NOT NULL` | Canonical RFC 3339 UTC boundary |
| `interval_end_utc` | `TEXT NOT NULL` | Canonical RFC 3339 UTC boundary |
| `consumption_kwh` | `TEXT NOT NULL` | Canonical exact decimal kWh value |
| `quality` | `TEXT` | Optional provider billing/quality state |
| `source_updated_at_utc` | `TEXT` | Optional provider update timestamp |
| `fetched_at_utc` | `TEXT NOT NULL` | Time this complete batch was fetched |

The unique record identity is `(account_id, service_point_id, COALESCE(meter_id, ''), channel, interval_start_utc, interval_end_utc)`. Syncs use transactional upserts: identical reruns are unchanged, while corrected non-identity values replace the existing row. Any SQL failure rolls back the complete batch. Only normalized columns are stored.

## Error behavior

All intentional library failures derive from `SynergyError` and use safe messages without credentials, OTPs, cookies, Aura material, mailbox bodies, or provider response bodies.

| Error | Meaning |
|---|---|
| `ConfigurationError` | Invalid Gmail credentials shape, query range/filter, client argument, or database path |
| `AuthenticationError` | Synergy rejected authentication |
| `AuthenticationTransportError` | Browser/login transport failed |
| `UnsupportedAuthChallenge` | CAPTCHA or another explicitly unsupported challenge appeared |
| `PortalContractError` / `AuthenticationContractError` | A required provider page or credential-handoff contract changed |
| `OtpMailboxError` | Gmail TLS, login, mailbox selection, or IMAP operation failed |
| `OtpTimeoutError` | No fresh unambiguous OTP arrived before the bounded deadline |
| `OtpParseError` / `OtpRejectedError` | The exact OTP format changed or Synergy rejected the submitted code |
| `SessionExpiredError` | The direct session remained invalid after one remint and replay |
| `AuthorizationError` | Authentication succeeded but the account cannot access the resource |
| `UsageFetchError` | A direct Aura request or HTTP lifecycle operation failed |
| `UsageValidationError` | Provider data could not be validated without guessing |
| `StorageError` | SQLite creation, schema, transaction, or persistence failed |

Ordinary authorization failures, rate limits, bad requests, and server errors are not treated as session expiry and do not trigger repeated logins or OTP requests.

## Security constraints

- Use a dedicated mailbox and keep all three credentials outside the repository.
- Never use the normal Google password; rotate the Gmail app password after Google-password changes or suspected exposure.
- Never log or persist credentials, OTPs, cookies, Aura tokens/contexts, mailbox content, or raw provider responses.
- Do not enable Playwright tracing, screenshots, video, HAR capture, storage state, or a persistent browser profile around authentication.
- Do not serialize `SynergyClient` internals or reuse HTTP/Aura state across processes.
- Close `SynergyClient` with a context manager or `close()` to discard HTTP cookies and authentication material.
- Treat provider protocol changes as contract changes requiring reviewed, invented/sanitized fixtures; never commit raw captures or copied sessions.

## Verification

Default tests are offline and exclude the live marker:

```console
python -m pytest
python -m ruff check src tests
python -m mypy src
```

The live smoke test is destructive only to ephemeral local state but accesses the real Synergy and Gmail services. It is opt-in and requires the dedicated-account environment variables:

```console
python -m pytest --run-live -m live tests/live/test_synergy_live.py
```

The live test uses a temporary SQLite database, verifies that it can be reopened, forces one direct-session expiry, checks the single remint/replay behavior, and checks captured output for observed secrets. It must never be enabled in normal CI.
