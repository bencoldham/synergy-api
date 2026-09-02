# Test instructions

These rules supplement the repository-level `AGENTS.md`.

- Real Synergy and Gmail access is forbidden outside `tests/live`.
- Default test execution must remain offline and succeed without credentials or environment secrets.
- Fixtures may contain only invented Gmail addresses, IDs, app passwords, OTPs, cookies, Aura tokens/contexts, and usage values.
- Never commit raw captures. Hand-author the smallest fixture that represents the reviewed sanitized schema.
- Use temporary on-disk SQLite databases for persistence behavior; do not prove reopen, permission, transaction, or corruption behavior only with `:memory:`.
- Authentication tests must assert login, OTP, browser-launch, and replay attempt counts so repeated challenge loops cannot regress unnoticed.
- Every secret-related test must use unique Synergy-password and Gmail-app-password sentinels and assert that they are absent from logs, exceptions, and generated files.
- Live tests require `--run-live`, the `live` marker, and `WA_SYNERGY_EMAIL`, `WA_SYNERGY_PASSWORD`, and `WA_SYNERGY_GMAIL_APP_PASSWORD` from the environment. Never add fallback credentials.
- Live tests must log out of Gmail IMAP, close the Playwright page/context/browser immediately after HTTP credential handoff, close `SynergyClient`, and remove temporary databases even after failure.
- Live tests and fixtures must never emit or retain traces, screenshots, video, HAR, storage state, response bodies, mailbox bodies, or copied session values.
