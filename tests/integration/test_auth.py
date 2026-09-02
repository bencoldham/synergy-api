from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path
import unittest
from dataclasses import dataclass
from typing import Any
from unittest.mock import patch
from urllib.parse import urlencode

from playwright.sync_api import Error as PlaywrightError

from wa_synergy.auth import _is_visible, mint_http_credentials
from wa_synergy.config import SynergyCredentials
from wa_synergy.errors import (
    AuthenticationContractError,
    AuthenticationError,
    AuthenticationTransportError,
    OtpRejectedError,
    OtpTimeoutError,
    UnsupportedAuthChallenge,
)

_AURA_CONTEXT = json.dumps(
    {
        "mode": "PROD",
        "fwuid": "synthetic-framework-id",
        "app": "siteforce:communityApp",
        "loaded": {},
        "dn": [],
        "globals": {},
        "uad": False,
    },
    separators=(",", ":"),
)


def _name_matches(expected: object, actual: str) -> bool:
    if isinstance(expected, str):
        return expected == actual
    if isinstance(expected, re.Pattern):
        return expected.search(actual) is not None
    return False


@dataclass
class FakeRequest:
    url: str
    method: str
    post_data: str | None


class FakeLocator:
    def __init__(
        self,
        page: FakePage,
        kind: str,
        name: object,
    ) -> None:
        self.page = page
        self.kind = kind
        self.name = name

    @property
    def first(self) -> FakeLocator:
        return self

    def nth(self, _index: int) -> FakeLocator:
        return self

    def count(self) -> int:
        return 1 if self._exists() else 0

    def is_visible(self) -> bool:
        return self._exists()

    def _exists(self) -> bool:
        state = self.page.state
        if self.kind == "label":
            return state in {"login", "transient-login-error"} and self.name in {
                "Email",
                "Password",
            }
        if self.kind == "role-button":
            if state in {"login", "transient-login-error"}:
                return _name_matches(self.name, "Log in")
            if state == "authenticated":
                return _name_matches(
                    self.name, "Welcome, Synthetic User"
                ) or _name_matches(self.name, "Switch account")
            if state == "otp":
                return _name_matches(self.name, "Verify")
            if state == "otp-method":
                return _name_matches(self.name, "Email") or _name_matches(
                    self.name, "SMS"
                )
            return False
        if self.kind == "role-textbox":
            return state == "otp" and _name_matches(self.name, "Passcode")
        if self.kind == "otp-input":
            return state == "otp"
        if self.kind == "text":
            texts = {
                "captcha": "CAPTCHA challenge",
                "transient-login-error": "Sorry, Please refresh the page and try again.",
                "rejected": "Invalid email or password",
            }
            return (
                state in texts
                and isinstance(self.name, re.Pattern)
                and bool(self.name.search(texts[state]))
            )
        if self.kind == "aura-token":
            return self.page.dom_material and state == "authenticated"
        if self.kind == "aura-context":
            return self.page.dom_material and state == "authenticated"
        return False

    def fill(self, value: str) -> None:
        self.page.fills.append((self.kind, self.name, value))
    def hover(self) -> None:
        self.page.calls.append(("hover", (self.kind, self.name)))


    def click(self, **kwargs: object) -> None:
        self.page.calls.append(("click-options", kwargs))
        self.page.clicks.append((self.kind, self.name))
        if self.kind != "role-button":
            return
        if self.page.state in {"login", "transient-login-error"} and _name_matches(
            self.name, "Log in"
        ):
            self.page.login_submissions += 1
            if self.page.login_submissions == 1:
                self.page.state = self.page.after_login
            else:
                self.page.state = (
                    self.page.after_login_retry
                    if self.page.after_login_retry is not None
                    else self.page.after_login
                )
            self.page._emit_aura_credentials_if_authenticated()
        elif self.page.state == "otp-method" and _name_matches(self.name, "Email"):
            self.page.otp_method_submissions += 1
            self.page.state = "otp"
        elif self.page.state == "otp" and _name_matches(self.name, "Verify"):
            self.page.otp_submissions += 1
            self.page.state = self.page.after_otp
            self.page._emit_aura_credentials_if_authenticated()

    def input_value(self) -> str:
        if self.kind == "aura-token":
            return "synthetic-aura-token"
        if self.kind == "aura-context":
            return _AURA_CONTEXT
        raise AssertionError(f"unexpected input_value call for {self.kind}")


class FakePage:
    def __init__(
        self,
        *,
        initial: str = "login",
        after_login: str = "authenticated",
        after_otp: str = "authenticated",
        after_login_retry: str | None = None,
        emit_material: bool = True,
        dom_material: bool = False,
    ) -> None:
        self.state = initial
        self.after_login = after_login
        self.after_otp = after_otp
        self.after_login_retry = after_login_retry
        self.emit_material = emit_material
        self.dom_material = dom_material
        self.url = (
            "https://my.synergy.net.au/s/service-dashboard"
            if initial == "authenticated"
            else "https://my.synergy.net.au/s/login/"
        )
        self.closed = False
        self.calls: list[tuple[str, object]] = []
        self.fills: list[tuple[str, object, str]] = []
        self.clicks: list[tuple[str, object]] = []
        self.login_submissions = 0
        self.otp_method_submissions = 0
        self.otp_submissions = 0
        self._request_observer: Any = None

    def on(self, event: str, observer: Any) -> None:
        if event != "request":
            raise AssertionError(f"unexpected event subscription: {event}")
        self.calls.append(("on", event))
        self._request_observer = observer

    def goto(self, url: str, **kwargs: object) -> None:
        self.calls.append(("goto", (url, kwargs)))

    def get_by_label(self, name: object, **kwargs: object) -> FakeLocator:
        self.calls.append(("get_by_label", (name, kwargs)))
        return FakeLocator(self, "label", name)

    def get_by_role(self, role: str, *, name: object, **kwargs: object) -> FakeLocator:
        self.calls.append(("get_by_role", (role, name, kwargs)))
        return FakeLocator(self, f"role-{role}", name)

    def get_by_text(self, text: object) -> FakeLocator:
        self.calls.append(("get_by_text", text))
        return FakeLocator(self, "text", text)

    def locator(self, selector: str) -> FakeLocator:
        self.calls.append(("locator", selector))
        kinds = {
            'input[name="aura.token"]': "aura-token",
            'input[name="aura.context"]': "aura-context",
            "#otp-field, #otp": "otp-input",
        }
        return FakeLocator(self, kinds.get(selector, "missing"), selector)

    def wait_for_timeout(self, milliseconds: int) -> None:
        time.sleep(milliseconds / 1_000)
        if self.state == "transient-login-error":
            self.state = "login"

    def close(self) -> None:
        self.closed = True

    def _emit_aura_credentials_if_authenticated(self) -> None:
        if self.state != "authenticated":
            return
        self.url = "https://my.synergy.net.au/s/service-dashboard"
        if self.emit_material and self._request_observer is not None:
            self._request_observer(
                FakeRequest(
                    url="https://my.synergy.net.au/s/sfsites/aura?aura.ApexAction.execute=1",
                    method="POST",
                    post_data=urlencode(
                        {
                            "aura.token": "synthetic-aura-token",
                            "aura.context": _AURA_CONTEXT,
                            "message": '{"actions":[{"descriptor":"bootstrap"}]}',
                        }
                    ),
                )
            )


class FakeContext:
    def __init__(self, page: FakePage, *, sid: str | None = "synthetic-sid") -> None:
        self.page = page
        self.sid = sid
        self.closed = False
        self.cookie_urls: list[str] = []
        self.init_scripts: list[str] = []

    def add_init_script(self, script: str) -> None:
        self.init_scripts.append(script)


    def new_page(self) -> FakePage:
        return self.page

    def cookies(self, url: str) -> list[dict[str, object]]:
        self.cookie_urls.append(url)
        cookies: list[dict[str, object]] = [
            {"name": "analytics", "value": "ignored"},
            {"name": "sid_Client", "value": "ignored"},
        ]
        if self.sid is not None:
            cookies.append({"name": "sid", "value": self.sid})
        return cookies

    def close(self) -> None:
        self.closed = True


class FakeBrowser:
    def __init__(self, context: FakeContext) -> None:
        self.context = context
        self.closed = False
        self.context_kwargs: list[dict[str, object]] = []

    def new_context(self, **kwargs: object) -> FakeContext:
        self.context_kwargs.append(kwargs)
        return self.context

    def close(self) -> None:
        self.closed = True


class FakeChromium:
    def __init__(self, browser: FakeBrowser) -> None:
        self.browser = browser
        self.launch_kwargs: list[dict[str, object]] = []
        self.launch_error: Exception | None = None
        self.persistent_context_calls: list[tuple[str, dict[str, object]]] = []

    def launch(self, **kwargs: object) -> FakeBrowser:
        self.launch_kwargs.append(kwargs)
        if self.launch_error is not None:
            raise self.launch_error
        return self.browser
    def launch_persistent_context(
        self,
        user_data_dir: str,
        **kwargs: object,
    ) -> FakeContext:
        self.persistent_context_calls.append((user_data_dir, kwargs))
        if self.launch_error is not None:
            raise self.launch_error
        return self.browser.context




class FakePlaywright:
    def __init__(self, chromium: FakeChromium) -> None:
        self.chromium = chromium


class FakePlaywrightManager:
    def __init__(self, playwright: FakePlaywright) -> None:
        self.playwright = playwright
        self.exited = False

    def __enter__(self) -> FakePlaywright:
        return self.playwright

    def __exit__(self, *args: object) -> None:
        self.exited = True


class FakeMailboxManager:
    def __init__(self) -> None:
        self.entered = 0
        self.exited = 0
        self.mailbox = object()

    def __enter__(self) -> object:
        self.entered += 1
        return self.mailbox

    def __exit__(self, *args: object) -> None:
        self.exited += 1


class AuthenticationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.synergy_password = "SYNERGY-AUTH-PASSWORD-SENTINEL"
        self.gmail_password = "GMAIL-AUTH-PASSWORD-SENTINEL"
        self.otp = "742915"
        self.credentials = SynergyCredentials(
            email="dedicated.synthetic@gmail.com",
            password=self.synergy_password,
            gmail_app_password=self.gmail_password,
        )

    def harness(
        self,
        *,
        initial: str = "login",
        after_login: str = "authenticated",
        after_otp: str = "authenticated",
        after_login_retry: str | None = None,
        emit_material: bool = True,
        dom_material: bool = False,
        sid: str | None = "synthetic-sid",
    ) -> tuple[
        FakePage,
        FakeContext,
        FakeBrowser,
        FakeChromium,
        FakePlaywrightManager,
        FakeMailboxManager,
    ]:
        page = FakePage(
            initial=initial,
            after_login=after_login,
            after_otp=after_otp,
            after_login_retry=after_login_retry,
            emit_material=emit_material,
            dom_material=dom_material,
        )
        context = FakeContext(page, sid=sid)
        browser = FakeBrowser(context)
        chromium = FakeChromium(browser)
        manager = FakePlaywrightManager(FakePlaywright(chromium))
        mailbox = FakeMailboxManager()
        return page, context, browser, chromium, manager, mailbox

    def mint(
        self,
        manager: FakePlaywrightManager,
        mailbox: FakeMailboxManager,
        *,
        interactive: bool = False,
    ) -> Any:
        with (
            patch("wa_synergy.auth.sync_playwright", return_value=manager),
            patch("wa_synergy.auth.gmail_otp_mailbox", return_value=mailbox),
            patch("wa_synergy.auth.wait_for_otp", return_value=self.otp) as otp_wait,
        ):
            result = mint_http_credentials(
                self.credentials,
                interactive=interactive,
            )
        return result, otp_wait

    def test_visibility_uses_any_matching_element_not_only_the_first(self) -> None:
        class VisibilityLocator:
            def __init__(self, visibility: tuple[bool, ...], index: int = 0) -> None:
                self.visibility = visibility
                self.index = index

            def count(self) -> int:
                return len(self.visibility)

            def nth(self, index: int) -> VisibilityLocator:
                return VisibilityLocator(self.visibility, index)

            def is_visible(self) -> bool:
                return self.visibility[self.index]

        self.assertTrue(_is_visible(VisibilityLocator((False, True))))  # type: ignore[arg-type]
        self.assertFalse(_is_visible(VisibilityLocator((False, False))))  # type: ignore[arg-type]
        self.assertFalse(_is_visible(VisibilityLocator(())))  # type: ignore[arg-type]

    def test_primary_login_without_otp_mints_only_direct_http_material(self) -> None:
        page, context, browser, chromium, manager, mailbox = self.harness()

        result, otp_wait = self.mint(manager, mailbox)

        self.assertEqual(result.sid, "synthetic-sid")
        self.assertEqual(result.aura_token, "synthetic-aura-token")
        self.assertEqual(result.aura_context, _AURA_CONTEXT)
        self.assertTrue(result.browser_closed)
        self.assertNotIn("synthetic-sid", repr(result))
        self.assertNotIn("synthetic-aura-token", repr(result))
        self.assertEqual(page.login_submissions, 1)
        self.assertEqual(page.otp_submissions, 0)
        otp_wait.assert_not_called()
        self.assertEqual((mailbox.entered, mailbox.exited), (1, 1))
        self.assertEqual(chromium.launch_kwargs, [{"headless": True}])
        self.assertEqual(browser.context_kwargs, [{}])
        self.assertEqual(context.cookie_urls, ["https://my.synergy.net.au"])
        self.assertEqual(
            [call for call in page.calls if call[0] == "goto"],
            [
                (
                    "goto",
                    (
                        "https://my.synergy.net.au/s/login/",
                        {"wait_until": "domcontentloaded", "timeout": 30_000},
                    ),
                )
            ],
        )
        self.assertTrue(page.closed)
        self.assertTrue(context.closed)
        self.assertTrue(browser.closed)
        self.assertTrue(manager.exited)

    def test_interactive_authentication_launches_a_visible_browser(self) -> None:
        _page, context, browser, chromium, manager, mailbox = self.harness()

        with (
            patch.dict(os.environ, {"WAYLAND_DISPLAY": ""}),
            patch(
                "wa_synergy.auth._browser_profile_path",
                return_value=Path("/synthetic/profile"),
            ),
            patch(
                "wa_synergy.auth._submit_humanized_login",
                side_effect=lambda page, _credentials: page.get_by_role(
                    "button",
                    name="Log in",
                    exact=True,
                ).click(),
            ),
        ):
            result, _otp_wait = self.mint(
                manager,
                mailbox,
                interactive=True,
            )

        self.assertEqual(result.sid, "synthetic-sid")
        self.assertEqual(chromium.launch_kwargs, [])
        self.assertEqual(
            chromium.persistent_context_calls,
            [
                (
                    "/synthetic/profile",
                    {
                        "channel": "chrome",
                        "headless": False,
                        "args": ["--disable-blink-features=AutomationControlled"],
                        "locale": "en-AU",
                        "timezone_id": "Australia/Perth",
                        "viewport": {"width": 1365, "height": 768},
                    },
                )
            ],
        )
        self.assertEqual(browser.context_kwargs, [])
        self.assertEqual(len(context.init_scripts), 1)
        self.assertIn("webdriver", context.init_scripts[0])


    def test_transient_login_integration_error_is_retried_once(self) -> None:
        page, _context, _browser, _chromium, manager, mailbox = self.harness(
            after_login="transient-login-error",
            after_login_retry="authenticated",
        )

        with patch("wa_synergy.auth._LOGIN_RETRY_STABILITY_MS", 0):
            result, otp_wait = self.mint(manager, mailbox)
        self.assertEqual(result.sid, "synthetic-sid")
        self.assertEqual(page.login_submissions, 2)
        self.assertEqual(page.otp_submissions, 0)
        otp_wait.assert_not_called()

    def test_otp_is_fetched_and_submitted_once_only_when_challenged(self) -> None:
        page, _context, _browser, _chromium, manager, mailbox = self.harness(
            after_login="otp"
        )

        result, otp_wait = self.mint(manager, mailbox)

        self.assertEqual(result.sid, "synthetic-sid")
        otp_wait.assert_called_once_with(mailbox.mailbox)
        self.assertEqual(page.login_submissions, 1)
        self.assertEqual(page.otp_submissions, 1)
        submitted_values = [value for _kind, _name, value in page.fills]
        self.assertIn(self.otp, submitted_values)
        self.assertEqual((mailbox.entered, mailbox.exited), (1, 1))

    def test_email_otp_method_is_selected_before_fetching_code(self) -> None:
        page, _context, _browser, _chromium, manager, mailbox = self.harness(
            after_login="otp-method"
        )

        result, otp_wait = self.mint(manager, mailbox)

        self.assertEqual(result.sid, "synthetic-sid")
        self.assertEqual(page.otp_method_submissions, 1)
        self.assertEqual(page.otp_submissions, 1)
        otp_wait.assert_called_once_with(mailbox.mailbox)

    def test_existing_authenticated_page_avoids_gmail_and_login(self) -> None:
        page, _context, _browser, _chromium, manager, mailbox = self.harness(
            initial="authenticated",
            emit_material=False,
            dom_material=True,
        )

        result, otp_wait = self.mint(manager, mailbox)

        self.assertEqual(result.sid, "synthetic-sid")
        self.assertEqual(page.login_submissions, 0)
        self.assertEqual(mailbox.entered, 0)
        otp_wait.assert_not_called()

    def test_invalid_credentials_close_every_browser_object(self) -> None:
        page, context, browser, _chromium, manager, mailbox = self.harness(
            after_login="rejected"
        )

        with (
            patch("wa_synergy.auth.sync_playwright", return_value=manager),
            patch("wa_synergy.auth.gmail_otp_mailbox", return_value=mailbox),
            self.assertRaises(AuthenticationError) as raised,
        ):
            mint_http_credentials(self.credentials)

        self.assertNotIn(self.synergy_password, str(raised.exception))
        self.assertNotIn(self.gmail_password, str(raised.exception))
        self.assertTrue(page.closed)
        self.assertTrue(context.closed)
        self.assertTrue(browser.closed)
        self.assertEqual((mailbox.entered, mailbox.exited), (1, 1))

    def test_rejected_otp_is_not_retried(self) -> None:
        page, _context, _browser, _chromium, manager, mailbox = self.harness(
            after_login="otp", after_otp="rejected"
        )

        with (
            patch("wa_synergy.auth.sync_playwright", return_value=manager),
            patch("wa_synergy.auth.gmail_otp_mailbox", return_value=mailbox),
            patch("wa_synergy.auth.wait_for_otp", return_value=self.otp) as otp_wait,
            self.assertRaises(OtpRejectedError),
        ):
            mint_http_credentials(self.credentials)

        otp_wait.assert_called_once_with(mailbox.mailbox)
        self.assertEqual(page.otp_submissions, 1)

    def test_otp_timeout_propagates_and_closes_everything(self) -> None:
        page, context, browser, _chromium, manager, mailbox = self.harness(
            after_login="otp"
        )

        with (
            patch("wa_synergy.auth.sync_playwright", return_value=manager),
            patch("wa_synergy.auth.gmail_otp_mailbox", return_value=mailbox),
            patch(
                "wa_synergy.auth.wait_for_otp",
                side_effect=OtpTimeoutError("No fresh OTP arrived"),
            ) as otp_wait,
            self.assertRaises(OtpTimeoutError),
        ):
            mint_http_credentials(self.credentials)

        otp_wait.assert_called_once_with(mailbox.mailbox)
        self.assertEqual(page.otp_submissions, 0)
        self.assertTrue(page.closed)
        self.assertTrue(context.closed)
        self.assertTrue(browser.closed)
        self.assertEqual(mailbox.exited, 1)

    def test_captcha_before_or_after_submission_is_never_bypassed(self) -> None:
        cases = {
            "initial": {"initial": "captcha"},
            "after login": {"after_login": "captcha"},
        }
        for name, options in cases.items():
            with self.subTest(name=name):
                page, _context, _browser, _chromium, manager, mailbox = self.harness(
                    **options
                )
                with (
                    patch("wa_synergy.auth.sync_playwright", return_value=manager),
                    patch("wa_synergy.auth.gmail_otp_mailbox", return_value=mailbox),
                    self.assertRaises(UnsupportedAuthChallenge),
                ):
                    mint_http_credentials(self.credentials)
                self.assertLessEqual(page.login_submissions, 1)
                self.assertEqual(page.otp_submissions, 0)

    def test_unknown_portal_state_is_a_safe_contract_error(self) -> None:
        page, context, browser, _chromium, manager, mailbox = self.harness(
            initial="unknown"
        )

        with (
            patch("wa_synergy.auth.sync_playwright", return_value=manager),
            patch("wa_synergy.auth.gmail_otp_mailbox", return_value=mailbox),
            patch("wa_synergy.auth._STATE_TIMEOUT_MS", 1),
            self.assertRaises(AuthenticationContractError) as raised,
        ):
            mint_http_credentials(self.credentials)

        self.assertIn("unknown portal state", str(raised.exception))
        self.assertTrue(page.closed)
        self.assertTrue(context.closed)
        self.assertTrue(browser.closed)
        self.assertEqual(mailbox.entered, 0)

    def test_missing_session_material_fails_closed(self) -> None:
        page, context, browser, _chromium, manager, mailbox = self.harness(sid=None)

        with (
            patch("wa_synergy.auth.sync_playwright", return_value=manager),
            patch("wa_synergy.auth.gmail_otp_mailbox", return_value=mailbox),
            self.assertRaises(AuthenticationContractError) as raised,
        ):
            mint_http_credentials(self.credentials)

        self.assertNotIn("synthetic-aura-token", str(raised.exception))
        self.assertTrue(page.closed)
        self.assertTrue(context.closed)
        self.assertTrue(browser.closed)

    def test_playwright_start_failure_is_safe_transport_error(self) -> None:
        _page, _context, _browser, chromium, manager, _mailbox = self.harness()
        chromium.launch_error = PlaywrightError(
            f"browser transcript {self.synergy_password} {self.gmail_password}"
        )

        with (
            patch("wa_synergy.auth.sync_playwright", return_value=manager),
            self.assertRaises(AuthenticationTransportError) as raised,
        ):
            mint_http_credentials(self.credentials)

        self.assertEqual(
            str(raised.exception),
            "Synergy authentication could not reach the login service",
        )
        self.assertNotIn(self.synergy_password, str(raised.exception))
        self.assertNotIn(self.gmail_password, str(raised.exception))


if __name__ == "__main__":
    unittest.main()
