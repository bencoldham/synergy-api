import json
import unittest
from pathlib import Path
from urllib.parse import parse_qs

import httpx

from wa_synergy.auth import _AuthenticationResult
from wa_synergy.errors import AuthenticationContractError, UsageFetchError
from wa_synergy.models import UsageQuery
from wa_synergy.usage import _AuthenticationLost, create_http_client, fetch_usage

_FIXTURE = Path(__file__).parents[1] / "fixtures" / "usage" / "valid.json"
_USAGE_RESPONSE = _FIXTURE.read_text(encoding="utf-8")
_ACCOUNT_ID = "0000000001"
_SERVICE_POINT_ID = "a00000000000000001"
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


def _authentication(*, browser_closed: bool = True) -> _AuthenticationResult:
    return _AuthenticationResult(
        sid="synthetic-sid",
        aura_token="synthetic-aura-token",
        aura_context=_AURA_CONTEXT,
        browser_closed=browser_closed,
    )


def _form(request: httpx.Request) -> dict[str, list[str]]:
    return parse_qs(request.content.decode("ascii"), keep_blank_values=True)


def _message(request: httpx.Request) -> dict[str, object]:
    form = _form(request)
    return json.loads(form["message"][0])


def _discovery_response() -> httpx.Response:
    return httpx.Response(
        200,
        headers={"Content-Type": "application/json;charset=UTF-8"},
        json={
            "actions": [
                {
                    "id": "1;a",
                    "state": "SUCCESS",
                    "returnValue": {
                        "returnValue": {
                            "ActiveServices": [
                                {
                                    "Id": "a00000000000000001",
                                    "value": "a00000000000000001",
                                    "AccountNumber": "0000000001",
                                    "AdditiveField": "ignored",
                                },
                                {
                                    "Id": "a00000000000000002",
                                    "value": "a00000000000000002",
                                    "AccountNumber": "0000000002",
                                },
                            ],
                            "InactiveServices": [],
                            "AdditiveResultField": True,
                        },
                        "AdditiveWrapperField": "ignored",
                    },
                }
            ]
        },
    )


class DirectAuraTransportTests(unittest.TestCase):
    def test_explicit_service_uses_minimal_direct_chart_action(self) -> None:
        requests: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            return httpx.Response(
                200,
                headers={"Content-Type": "application/json"},
                text=_USAGE_RESPONSE,
            )

        authentication = _authentication()
        query = UsageQuery(
            start="2026-07-01",
            end="2026-07-02",
            account_ids=(_ACCOUNT_ID,),
            service_point_ids=(_SERVICE_POINT_ID,),
        )
        with create_http_client(
            authentication,
            transport=httpx.MockTransport(handler),
        ) as client:
            intervals = fetch_usage(client, authentication, query)

        self.assertEqual(len(intervals), 4)
        self.assertEqual(len(requests), 1)
        request = requests[0]
        self.assertEqual(request.method, "POST")
        self.assertEqual(request.url.path, "/s/sfsites/aura")
        self.assertEqual(
            dict(request.url.params),
            {"aura.ApexAction.execute": "1"},
        )
        self.assertEqual(
            request.headers["Content-Type"],
            "application/x-www-form-urlencoded;charset=UTF-8",
        )
        self.assertEqual(request.headers["Cookie"], "sid=synthetic-sid")
        for absent_header in (
            "origin",
            "referer",
            "user-agent",
            "sec-fetch-site",
            "x-request-id",
            "traceparent",
        ):
            self.assertNotIn(absent_header, request.headers)

        form = _form(request)
        self.assertEqual(
            set(form),
            {"message", "aura.context", "aura.token"},
        )
        self.assertEqual(form["aura.context"], [_AURA_CONTEXT])
        self.assertEqual(form["aura.token"], ["synthetic-aura-token"])

        action = _message(request)["actions"][0]
        self.assertEqual(
            action["descriptor"],
            "aura://ApexActionController/ACTION$execute",
        )
        self.assertEqual(
            set(action),
            {"id", "descriptor", "params"},
        )
        apex = action["params"]
        self.assertEqual(
            apex,
            {
                "namespace": "",
                "classname": "vlocity_cmt.BusinessProcessDisplayController",
                "method": "GenericInvoke2NoCont",
                "params": apex["params"],
                "cacheable": False,
                "isContinuation": False,
            },
        )
        nested = apex["params"]
        self.assertEqual(
            set(nested),
            {"input", "options", "sClassName", "sMethodName"},
        )
        self.assertEqual(nested["options"], "{}")
        self.assertEqual(
            nested["sClassName"],
            "vlocity_cmt.IntegrationProcedureService",
        )
        self.assertEqual(nested["sMethodName"], "MyAccount_ChartData")
        self.assertEqual(
            json.loads(nested["input"]),
            {
                "IntervalType": "DAILY",
                "ChartType": "INTERVAL_DATA",
                "ServiceId": _SERVICE_POINT_ID,
                "StartDate": "20260701",
                "EndDate": "20260701",
                "PeriodStartDate": "20260701",
                "PeriodEndDate": "20260701",
                "Device": [],
            },
        )

    def test_discovery_iterates_every_active_service(self) -> None:
        requests: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            if len(requests) == 1:
                return _discovery_response()
            return httpx.Response(
                200,
                headers={"Content-Type": "application/json"},
                text=_USAGE_RESPONSE,
            )

        authentication = _authentication()
        with create_http_client(
            authentication,
            transport=httpx.MockTransport(handler),
        ) as client:
            intervals = fetch_usage(
                client,
                authentication,
                UsageQuery(start="2026-07-01", end="2026-07-02"),
            )

        self.assertEqual(len(requests), 3)
        discovery = _message(requests[0])["actions"][0]["params"]
        self.assertEqual(discovery["classname"], "CommunityServiceController")
        self.assertEqual(discovery["method"], "getLinkedServices")
        self.assertEqual(discovery["params"], {})
        requested_services = {
            json.loads(_message(request)["actions"][0]["params"]["params"]["input"])[
                "ServiceId"
            ]
            for request in requests[1:]
        }
        self.assertEqual(
            requested_services,
            {"a00000000000000001", "a00000000000000002"},
        )
        self.assertEqual(
            {
                (interval.account_id, interval.service_point_id)
                for interval in intervals
            },
            {
                ("0000000001", "a00000000000000001"),
                ("0000000002", "a00000000000000002"),
            },
        )

    def test_open_browser_material_is_rejected_before_network_access(self) -> None:
        requests = 0

        def handler(_request: httpx.Request) -> httpx.Response:
            nonlocal requests
            requests += 1
            return httpx.Response(500)

        with self.assertRaises(AuthenticationContractError):
            create_http_client(
                _authentication(browser_closed=False),
                transport=httpx.MockTransport(handler),
            )
        self.assertEqual(requests, 0)

    def test_http_failure_is_safe_and_does_not_include_response(self) -> None:
        secret_body = "raw-response-sentinel"

        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(429, text=secret_body)

        authentication = _authentication()
        with (
            create_http_client(
                authentication,
                transport=httpx.MockTransport(handler),
            ) as client,
            self.assertRaises(UsageFetchError) as raised,
        ):
            fetch_usage(
                client,
                authentication,
                UsageQuery(
                    start="2026-07-01",
                    end="2026-07-02",
                    account_ids=(_ACCOUNT_ID,),
                    service_point_ids=(_SERVICE_POINT_ID,),
                ),
            )
        self.assertNotIn(secret_body, str(raised.exception))
        self.assertIn("HTTP 429", str(raised.exception))

    def test_captured_authentication_loss_signatures_are_internal_signals(self) -> None:
        invalid_session = json.dumps(
            {
                "actions": [
                    {
                        "id": "1;a",
                        "state": "SUCCESS",
                        "returnValue": {
                            "returnValue": json.dumps(
                                {
                                    "IPResult": {
                                        "success": False,
                                        "error": (
                                            "You do not have access to the Apex class "
                                            "named 'BusinessProcessDisplayController'."
                                        ),
                                    }
                                }
                            )
                        },
                    }
                ]
            }
        )
        responses = (
            httpx.Response(401),
            httpx.Response(302, headers={"Location": "/s/login/"}),
            httpx.Response(
                200,
                headers={"Content-Type": "text/html"},
                text=(
                    '<a href="/s/login/">Log in</a>'
                    '<input name="Email"><input name="Password">'
                ),
            ),
            httpx.Response(
                200,
                headers={"Content-Type": "application/json"},
                text="invalid-aura-token",
            ),
            httpx.Response(
                200,
                headers={"Content-Type": "application/json"},
                text=invalid_session,
            ),
        )
        query = UsageQuery(
            start="2026-07-01",
            end="2026-07-02",
            account_ids=(_ACCOUNT_ID,),
            service_point_ids=(_SERVICE_POINT_ID,),
        )

        for response in responses:
            with (
                self.subTest(status=response.status_code, body=response.text[:20]),
                create_http_client(
                    _authentication(),
                    transport=httpx.MockTransport(
                        lambda _request, captured=response: captured
                    ),
                ) as client,
                self.assertRaises(_AuthenticationLost),
            ):
                fetch_usage(client, _authentication(), query)

    def test_ordinary_forbidden_response_does_not_signal_session_expiry(self) -> None:
        authentication = _authentication()
        with (
            create_http_client(
                authentication,
                transport=httpx.MockTransport(
                    lambda _request: httpx.Response(
                        403,
                        headers={"Content-Type": "application/json"},
                        json={"error": "forbidden"},
                    )
                ),
            ) as client,
            self.assertRaisesRegex(UsageFetchError, "HTTP 403"),
        ):
            fetch_usage(
                client,
                authentication,
                UsageQuery(
                    start="2026-07-01",
                    end="2026-07-02",
                    account_ids=(_ACCOUNT_ID,),
                    service_point_ids=(_SERVICE_POINT_ID,),
                ),
            )


if __name__ == "__main__":
    unittest.main()
