from __future__ import annotations

import logging
import os
import sqlite3
from pathlib import Path
from unittest.mock import patch

import pytest

from wa_synergy import (
    SynergyClient,
    SynergyCredentials,
    UsageQuery,
    sync_usage_to_db,
)
from wa_synergy import auth as auth_module
from wa_synergy import client as client_module
from wa_synergy.auth import _AuthenticationResult
from wa_synergy.models import UsageInterval
from wa_synergy.otp import GmailOtpMailbox

pytestmark = pytest.mark.live

_LIVE_QUERY = UsageQuery(start="2026-07-01", end="2026-07-02")
_EXPECTED_INTERVALS_PER_SERVICE = 96
_EXPIRED_SID = "deliberately-expired-live-session"


def _live_credentials() -> SynergyCredentials:
    names = (
        "WA_SYNERGY_EMAIL",
        "WA_SYNERGY_PASSWORD",
        "WA_SYNERGY_GMAIL_APP_PASSWORD",
    )
    missing = [name for name in names if not os.environ.get(name)]
    if missing:
        pytest.fail("live test credentials are missing from the environment")
    return SynergyCredentials(
        email=os.environ[names[0]],
        password=os.environ[names[1]],
        gmail_app_password=os.environ[names[2]],
    )


def _database_identity(db_path: Path) -> tuple[int, tuple[str, ...], tuple[str, ...]]:
    with sqlite3.connect(db_path) as connection:
        version = connection.execute("PRAGMA user_version").fetchone()
        row_count = connection.execute(
            "SELECT COUNT(*) FROM usage_intervals"
        ).fetchone()
        accounts = connection.execute(
            "SELECT DISTINCT account_id FROM usage_intervals ORDER BY account_id"
        ).fetchall()
        services = connection.execute(
            "SELECT DISTINCT service_point_id FROM usage_intervals "
            "ORDER BY service_point_id"
        ).fetchall()
    assert version == (1,)
    assert row_count is not None
    return (
        int(row_count[0]),
        tuple(str(row[0]) for row in accounts),
        tuple(str(row[0]) for row in services),
    )


def _captured_output(
    caplog: pytest.LogCaptureFixture, capsys: pytest.CaptureFixture[str]
) -> str:
    streams = capsys.readouterr()
    messages = [record.getMessage() for record in caplog.records]
    messages.extend((streams.out, streams.err))
    return "\n".join(messages)


def test_live_sync_reopen_reauthentication_and_secret_hygiene(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    capsys: pytest.CaptureFixture[str],
) -> None:
    credentials = _live_credentials()
    db_path = tmp_path / "synergy-live.sqlite3"
    mint_calls = 0
    fetch_calls = 0
    observed_secrets = [
        credentials.email,
        credentials.password,
        credentials.gmail_app_password,
    ]

    real_mint = client_module.mint_http_credentials
    real_fetch = client_module.fetch_usage
    real_wait_for_otp = auth_module.wait_for_otp

    def tracked_mint(value: SynergyCredentials) -> _AuthenticationResult:
        nonlocal mint_calls
        mint_calls += 1
        result = real_mint(value)
        observed_secrets.extend((result.sid, result.aura_token, result.aura_context))
        return result

    def tracked_fetch(*args: object, **kwargs: object) -> tuple[UsageInterval, ...]:
        nonlocal fetch_calls
        fetch_calls += 1
        return real_fetch(*args, **kwargs)  # type: ignore[arg-type]

    def tracked_wait_for_otp(
        mailbox: GmailOtpMailbox,
        *,
        timeout: float = 120.0,
        poll_interval: float = 2.0,
    ) -> str:
        otp = real_wait_for_otp(
            mailbox,
            timeout=timeout,
            poll_interval=poll_interval,
        )
        observed_secrets.append(otp)
        return otp

    caplog.set_level(logging.INFO)
    try:
        with (
            patch("wa_synergy.client.mint_http_credentials", tracked_mint),
            patch("wa_synergy.client.fetch_usage", tracked_fetch),
            patch("wa_synergy.auth.wait_for_otp", tracked_wait_for_otp),
            SynergyClient(credentials=credentials) as client,
        ):
            result = sync_usage_to_db(
                client=client,
                query=_LIVE_QUERY,
                db_path=db_path,
            )
            row_count, account_ids, service_point_ids = _database_identity(db_path)

            assert result.inserted == row_count
            assert result.updated == 0
            assert result.unchanged == 0
            assert row_count == _EXPECTED_INTERVALS_PER_SERVICE
            assert len(account_ids) == 1
            assert len(service_point_ids) == 1

            http_client = client._http_client
            assert http_client is not None
            http_client.cookies.set(
                "sid",
                _EXPIRED_SID,
                domain="my.synergy.net.au",
                path="/",
            )
            replay_query = UsageQuery(
                start=_LIVE_QUERY.start,
                end=_LIVE_QUERY.end,
                account_ids=account_ids,
                service_point_ids=service_point_ids,
            )
            mint_before = mint_calls
            fetch_before = fetch_calls
            replayed = client.get_usage(replay_query)

            assert len(replayed) == row_count
            assert mint_calls - mint_before == 1
            assert fetch_calls - fetch_before == 2
    finally:
        captured = _captured_output(caplog, capsys)
        for secret in observed_secrets:
            assert secret not in captured
