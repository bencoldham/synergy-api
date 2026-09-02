from __future__ import annotations

import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

import httpx

from wa_synergy.auth import _AuthenticationResult
from wa_synergy.client import SynergyClient
from wa_synergy.config import SynergyCredentials
from wa_synergy.errors import SessionExpiredError, UsageFetchError
from wa_synergy.models import UsageQuery
from wa_synergy.usage import _AuthenticationLost

_QUERY = UsageQuery(
    start="2026-07-01",
    end="2026-07-02",
    account_ids=("0000000001",),
    service_point_ids=("a00000000000000001",),
)


def _credentials() -> SynergyCredentials:
    return SynergyCredentials(
        email="synthetic.account@gmail.com",
        password="synergy-password-sentinel",
        gmail_app_password="gmail-password-sentinel",
    )


def _authentication(sequence: int) -> _AuthenticationResult:
    return _AuthenticationResult(
        sid=f"synthetic-sid-{sequence}",
        aura_token=f"synthetic-token-{sequence}",
        aura_context="synthetic-context",
        browser_closed=True,
    )


def _http_client() -> httpx.Client:
    return httpx.Client(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(500, request=request)
        )
    )


class SynergyClientTests(unittest.TestCase):
    def test_reuses_one_direct_http_session_and_closes_it(self) -> None:
        direct_client = _http_client()
        with (
            patch(
                "wa_synergy.client.mint_http_credentials",
                return_value=_authentication(1),
            ) as mint,
            patch(
                "wa_synergy.client.create_http_client",
                return_value=direct_client,
            ) as create,
            patch("wa_synergy.client.fetch_usage", return_value=()) as fetch,
            SynergyClient(credentials=_credentials()) as client,
        ):
            self.assertEqual(client.get_usage(_QUERY), ())
            self.assertEqual(client.get_usage(_QUERY), ())

        self.assertEqual(mint.call_count, 1)
        self.assertEqual(create.call_count, 1)
        self.assertEqual(fetch.call_count, 2)
        self.assertIs(fetch.call_args_list[0].args[0], direct_client)
        self.assertIs(fetch.call_args_list[1].args[0], direct_client)
        self.assertTrue(direct_client.is_closed)

    def test_authentication_loss_remints_once_and_replays_once(self) -> None:
        stale_client = _http_client()
        fresh_client = _http_client()
        with (
            patch(
                "wa_synergy.client.mint_http_credentials",
                side_effect=(_authentication(1), _authentication(2)),
            ) as mint,
            patch(
                "wa_synergy.client.create_http_client",
                side_effect=(stale_client, fresh_client),
            ),
            patch(
                "wa_synergy.client.fetch_usage",
                side_effect=(_AuthenticationLost(), ()),
            ) as fetch,
            SynergyClient(credentials=_credentials()) as client,
        ):
            self.assertEqual(client.get_usage(_QUERY), ())
            self.assertTrue(stale_client.is_closed)
            self.assertFalse(fresh_client.is_closed)

        self.assertEqual(mint.call_count, 2)
        self.assertEqual(fetch.call_count, 2)
        self.assertTrue(fresh_client.is_closed)

    def test_second_authentication_loss_raises_without_looping(self) -> None:
        clients = (_http_client(), _http_client())
        with (
            patch(
                "wa_synergy.client.mint_http_credentials",
                side_effect=(_authentication(1), _authentication(2)),
            ) as mint,
            patch(
                "wa_synergy.client.create_http_client",
                side_effect=clients,
            ),
            patch(
                "wa_synergy.client.fetch_usage",
                side_effect=(_AuthenticationLost(), _AuthenticationLost()),
            ) as fetch,
            SynergyClient(credentials=_credentials()) as client,
            self.assertRaises(SessionExpiredError),
        ):
            client.get_usage(_QUERY)

        self.assertEqual(mint.call_count, 2)
        self.assertEqual(fetch.call_count, 2)
        self.assertTrue(all(client.is_closed for client in clients))

    def test_public_operations_are_serialized(self) -> None:
        active = 0
        maximum_active = 0
        state_lock = threading.Lock()

        def fetch(*_args: object) -> tuple[()]:
            nonlocal active, maximum_active
            with state_lock:
                active += 1
                maximum_active = max(maximum_active, active)
            time.sleep(0.02)
            with state_lock:
                active -= 1
            return ()

        with (
            patch(
                "wa_synergy.client.mint_http_credentials",
                return_value=_authentication(1),
            ),
            patch(
                "wa_synergy.client.create_http_client",
                return_value=_http_client(),
            ),
            patch("wa_synergy.client.fetch_usage", side_effect=fetch),
            SynergyClient(credentials=_credentials()) as client,
            ThreadPoolExecutor(max_workers=2) as executor,
        ):
            results = tuple(executor.map(client.get_usage, (_QUERY, _QUERY)))

        self.assertEqual(results, ((), ()))
        self.assertEqual(maximum_active, 1)

    def test_closed_client_rejects_usage_without_authenticating(self) -> None:
        client = SynergyClient(credentials=_credentials())
        client.close()
        with (
            patch("wa_synergy.client.mint_http_credentials") as mint,
            self.assertRaisesRegex(UsageFetchError, "closed"),
        ):
            client.get_usage(_QUERY)
        mint.assert_not_called()


if __name__ == "__main__":
    unittest.main()
