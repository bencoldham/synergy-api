from __future__ import annotations

import html
import json
import sqlite3
import threading
import unittest
from collections.abc import Iterator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
from urllib.parse import parse_qs

from wa_synergy import (
    SynergyClient,
    SynergyCredentials,
    UsageQuery,
    sync_usage_to_db,
)
from wa_synergy import auth as auth_module

_FIXTURE = Path(__file__).parents[1] / "fixtures" / "usage" / "valid.json"
_USAGE_RESPONSE = _FIXTURE.read_text(encoding="utf-8")
_ACCOUNT_ID = "0000000001"
_SERVICE_POINT_ID = "a00000000000000001"
_OTP = "123456"
_AURA_CONTEXT = json.dumps(
    {
        "mode": "PROD",
        "fwuid": "synthetic-fwuid",
        "app": "siteforce:communityApp",
        "loaded": {},
        "dn": [],
        "globals": {},
        "uad": False,
    },
    separators=(",", ":"),
)


class _PortalState:
    def __init__(self) -> None:
        self.base_url = ""
        self.current_sid = ""
        self.login_count = 0
        self.otp_count = 0
        self.expire_next_usage = False
        self.browser_closed = False
        self.browser_open_violations = 0
        self.direct_actions: list[str] = []
        self.direct_ports: list[int] = []

    def handler(self) -> type[BaseHTTPRequestHandler]:
        portal = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, _format: str, *_args: object) -> None:
                return

            def _send(
                self,
                status: int,
                body: str,
                *,
                content_type: str = "text/html; charset=utf-8",
                headers: dict[str, str] | None = None,
            ) -> None:
                encoded = body.encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(encoded)))
                if headers is not None:
                    for name, value in headers.items():
                        self.send_header(name, value)
                self.end_headers()
                self.wfile.write(encoded)

            def do_GET(self) -> None:
                if self.path == "/s/login/":
                    portal.browser_closed = False
                    self._send(
                        200,
                        """<!doctype html><html><body>
                        <form action="/login" method="post">
                          <label for="email">Email</label>
                          <input id="email" name="email" type="text">
                          <label for="password">Password</label>
                          <input id="password" name="password" type="password">
                          <button type="submit">Log in</button>
                        </form>
                        </body></html>""",
                    )
                    return
                self._send(404, "not found", content_type="text/plain")

            def do_POST(self) -> None:
                length = int(self.headers.get("Content-Length", "0"))
                body = self.rfile.read(length).decode("utf-8")
                path = self.path.partition("?")[0]
                if path == "/login":
                    fields = parse_qs(body)
                    if fields.get("email") != ["synthetic.account@gmail.com"]:
                        self._send(403, "rejected", content_type="text/plain")
                        return
                    self._send(
                        200,
                        """<!doctype html><html><body>
                        <form action="/s/service-dashboard" method="post">
                          <label for="otp">One-time passcode</label>
                          <input id="otp" name="otp" type="text">
                          <button type="submit">Verify</button>
                        </form>
                        </body></html>""",
                    )
                    return
                if path == "/s/service-dashboard":
                    fields = parse_qs(body)
                    if fields.get("otp") != [_OTP]:
                        self._send(403, "rejected", content_type="text/plain")
                        return
                    portal.login_count += 1
                    portal.current_sid = f"synthetic-sid-{portal.login_count}"
                    self._send(
                        200,
                        f"""<!doctype html><html><body>
                        <button>Welcome, Synthetic User</button>
                        <button>Switch account</button>
                        <input name="aura.token" value="synthetic-token-{portal.login_count}">
                        <input name="aura.context" value="{html.escape(_AURA_CONTEXT, quote=True)}">
                        </body></html>""",
                        headers={"Set-Cookie": f"sid={portal.current_sid}; Path=/"},
                    )
                    return
                if path == "/s/sfsites/aura":
                    self._handle_aura(body)
                    return
                self._send(404, "not found", content_type="text/plain")

            def _handle_aura(self, body: str) -> None:
                if not portal.browser_closed:
                    portal.browser_open_violations += 1
                portal.direct_ports.append(self.client_address[1])
                fields = parse_qs(body)
                message = json.loads(fields["message"][0])
                action = message["actions"][0]
                controller = action["params"]["classname"]
                cookie = self.headers.get("Cookie", "")
                valid_cookie = f"sid={portal.current_sid}" in cookie
                is_usage = controller.endswith("BusinessProcessDisplayController")
                if not valid_cookie or (is_usage and portal.expire_next_usage):
                    portal.expire_next_usage = False
                    portal.direct_actions.append("expired usage" if is_usage else "expired discovery")
                    nested = json.dumps(
                        {
                            "IPResult": {
                                "success": False,
                                "error": (
                                    "You do not have access to the Apex class named "
                                    f"'{controller.rpartition('.')[2]}'."
                                ),
                            }
                        },
                        separators=(",", ":"),
                    )
                    response = {
                        "actions": [
                            {
                                "id": "1;a",
                                "state": "SUCCESS",
                                "returnValue": {"returnValue": nested},
                            }
                        ]
                    }
                    self._send(
                        200,
                        json.dumps(response, separators=(",", ":")),
                        content_type="application/json",
                    )
                    return
                if controller == "CommunityServiceController":
                    portal.direct_actions.append("discovery")
                    response = {
                        "actions": [
                            {
                                "id": "1;a",
                                "state": "SUCCESS",
                                "returnValue": {
                                    "returnValue": {
                                        "ActiveServices": [
                                            {
                                                "Id": _SERVICE_POINT_ID,
                                                "value": _SERVICE_POINT_ID,
                                                "AccountNumber": _ACCOUNT_ID,
                                            }
                                        ]
                                    }
                                },
                            }
                        ]
                    }
                    self._send(
                        200,
                        json.dumps(response, separators=(",", ":")),
                        content_type="application/json",
                    )
                    return
                portal.direct_actions.append("usage")
                self._send(200, _USAGE_RESPONSE, content_type="application/json")

        return Handler


@contextmanager
def _synthetic_portal() -> Iterator[_PortalState]:
    portal = _PortalState()
    server = ThreadingHTTPServer(("127.0.0.1", 0), portal.handler())
    server.daemon_threads = True
    portal.base_url = f"http://127.0.0.1:{server.server_port}"
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield portal
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


class LocalEndToEndTests(unittest.TestCase):
    def test_browser_otp_handoff_direct_reuse_reauth_and_sqlite_sync(self) -> None:
        credentials = SynergyCredentials(
            email="synthetic.account@gmail.com",
            password="synergy-password-sentinel",
            gmail_app_password="gmail-password-sentinel",
        )
        discovered_query = UsageQuery(start="2026-07-01", end="2026-07-02")
        explicit_query = UsageQuery(
            start="2026-07-01",
            end="2026-07-02",
            account_ids=(_ACCOUNT_ID,),
            service_point_ids=(_SERVICE_POINT_ID,),
        )

        with _synthetic_portal() as portal, TemporaryDirectory() as directory:
            db_path = Path(directory) / "usage.sqlite3"
            original_close = auth_module._close_browser

            @contextmanager
            def fake_mailbox(_credentials: SynergyCredentials) -> Iterator[object]:
                yield object()

            def fake_wait_for_otp(_mailbox: object) -> str:
                portal.otp_count += 1
                return _OTP

            def tracking_close(*args: object) -> None:
                original_close(*args)  # type: ignore[arg-type]
                portal.browser_closed = True

            with (
                patch("wa_synergy.auth._LOGIN_URL", f"{portal.base_url}/s/login/"),
                patch("wa_synergy.auth._PORTAL_ORIGIN", portal.base_url),
                patch("wa_synergy.usage._PORTAL_ORIGIN", portal.base_url),
                patch("wa_synergy.auth.gmail_otp_mailbox", fake_mailbox),
                patch("wa_synergy.auth.wait_for_otp", fake_wait_for_otp),
                patch("wa_synergy.auth._close_browser", tracking_close),
                SynergyClient(
                    credentials=credentials,
                    interactive_auth=False,
                ) as client,
            ):
                first = sync_usage_to_db(
                    client=client,
                    query=discovered_query,
                    db_path=db_path,
                )
                reused = client.get_usage(explicit_query)
                portal.expire_next_usage = True
                replayed = sync_usage_to_db(
                    client=client,
                    query=explicit_query,
                    db_path=db_path,
                )

            self.assertEqual((first.inserted, first.updated, first.unchanged), (4, 0, 0))
            self.assertEqual(len(reused), 4)
            self.assertEqual(
                (replayed.inserted, replayed.updated, replayed.unchanged),
                (0, 0, 4),
            )
            self.assertEqual(portal.login_count, 2)
            self.assertEqual(portal.otp_count, 2)
            self.assertEqual(portal.browser_open_violations, 0)
            self.assertEqual(
                portal.direct_actions,
                ["discovery", "usage", "usage", "expired usage", "usage"],
            )
            self.assertEqual(len(set(portal.direct_ports[:4])), 1)
            self.assertNotEqual(portal.direct_ports[3], portal.direct_ports[4])

            with sqlite3.connect(db_path) as connection:
                rows = connection.execute(
                    """SELECT account_id, service_point_id, channel, consumption_kwh
                       FROM usage_intervals
                       ORDER BY interval_start_utc, channel"""
                ).fetchall()
            self.assertEqual(len(rows), 4)
            self.assertEqual({row[0] for row in rows}, {_ACCOUNT_ID})
            self.assertEqual({row[1] for row in rows}, {_SERVICE_POINT_ID})
            self.assertEqual(
                {row[2] for row in rows},
                {"OFF_PEAK", "PEAK", "VAL_SOLAR"},
            )
            self.assertEqual({row[3] for row in rows}, {"0", "0.045", "0.1", "1.23"})


if __name__ == "__main__":
    unittest.main()
