"""Short-lived Playwright authentication and direct-HTTP credential handoff."""

from __future__ import annotations

import json
import re
import time
from contextlib import suppress
from dataclasses import dataclass, field, replace
from enum import Enum, auto
from urllib.parse import parse_qs, urlsplit

from playwright.sync_api import (
    Browser,
    BrowserContext,
    Locator,
    Page,
    Playwright,
    Request,
    sync_playwright,
)
from playwright.sync_api import (
    Error as PlaywrightError,
)
from playwright.sync_api import (
    TimeoutError as PlaywrightTimeoutError,
)

from .config import SynergyCredentials
from .errors import (
    AuthenticationContractError,
    AuthenticationError,
    AuthenticationTransportError,
    OtpRejectedError,
    UnsupportedAuthChallenge,
)
from .otp import gmail_otp_mailbox, wait_for_otp

_LOGIN_URL = "https://my.synergy.net.au/s/login/"
_PORTAL_ORIGIN = "https://my.synergy.net.au"
_AURA_PATH = "/s/sfsites/aura"
_STATE_TIMEOUT_MS = 30_000
_STATE_POLL_MS = 100
_AUTH_MATERIAL_TIMEOUT_MS = 5_000
_AURA_CONTEXT_KEYS = frozenset(
    {"mode", "fwuid", "app", "loaded", "dn", "globals", "uad"}
)
_OTP_NAME = re.compile(
    r"^(?:one[- ]time(?: passcode)?|passcode|verification code|security code|otp)$",
    re.IGNORECASE,
)
_OTP_SUBMIT_NAME = re.compile(r"^(?:verify|submit|continue)$", re.IGNORECASE)
_WELCOME_NAME = re.compile(r"^Welcome,", re.IGNORECASE)
_AUTH_FAILURE_TEXT = re.compile(
    r"(?:invalid|incorrect|locked|unable to log in|check your (?:email|password))",
    re.IGNORECASE,
)
_CAPTCHA_TEXT = re.compile(
    r"(?:captcha|Sorry, Please refresh the page and try again\.)",
    re.IGNORECASE,
)


@dataclass(frozen=True, slots=True)
class _AuthenticationResult:
    """Minimum memory-only material required by the direct Aura transport."""

    sid: str = field(repr=False)
    aura_token: str = field(repr=False)
    aura_context: str = field(repr=False)
    browser_closed: bool = field(default=False, repr=False)


@dataclass(slots=True)
class _ObservedAuraCredentials:
    aura_token: str | None = field(default=None, repr=False)
    aura_context: str | None = field(default=None, repr=False)

    def observe(self, request: Request) -> None:
        """Retain only credentials from a same-origin Aura form request."""

        try:
            parsed_url = urlsplit(request.url)
            if (
                request.method != "POST"
                or parsed_url.scheme != "https"
                or parsed_url.hostname != "my.synergy.net.au"
                or parsed_url.path != _AURA_PATH
                or request.post_data is None
            ):
                return
            fields = parse_qs(request.post_data, keep_blank_values=True)
            tokens = fields.get("aura.token", [])
            contexts = fields.get("aura.context", [])
            if len(tokens) == 1 and len(contexts) == 1:
                self.aura_token = tokens[0]
                self.aura_context = contexts[0]
        except (AttributeError, TypeError, UnicodeError, ValueError):
            return


class _AuthState(Enum):
    LOGIN = auto()
    AUTHENTICATED = auto()
    OTP = auto()
    REJECTED = auto()
    CAPTCHA = auto()


def _is_visible(locator: Locator) -> bool:
    return any(locator.nth(index).is_visible() for index in range(locator.count()))


def _login_controls(page: Page) -> tuple[Locator, Locator, Locator]:
    return (
        page.get_by_label("Email", exact=True),
        page.get_by_label("Password", exact=True),
        page.get_by_role("button", name="Log in", exact=True),
    )


def _otp_controls(page: Page) -> tuple[Locator, Locator]:
    return (
        page.get_by_role("textbox", name=_OTP_NAME),
        page.get_by_role("button", name=_OTP_SUBMIT_NAME),
    )


def _is_authenticated(page: Page) -> bool:
    path = urlsplit(page.url).path.rstrip("/")
    if path != "/s/service-dashboard":
        return False
    return _is_visible(page.get_by_role("button", name=_WELCOME_NAME)) or _is_visible(
        page.get_by_role("button", name="Switch account", exact=True)
    )


def _detect_state(page: Page, *, include_login: bool) -> _AuthState | None:
    if _is_visible(page.get_by_text(_CAPTCHA_TEXT)):
        return _AuthState.CAPTCHA
    if _is_authenticated(page):
        return _AuthState.AUTHENTICATED
    otp_input, otp_submit = _otp_controls(page)
    if _is_visible(otp_input) and _is_visible(otp_submit):
        return _AuthState.OTP
    if _is_visible(page.get_by_text(_AUTH_FAILURE_TEXT)):
        return _AuthState.REJECTED
    if include_login and all(_is_visible(control) for control in _login_controls(page)):
        return _AuthState.LOGIN
    return None


def _wait_for_state(
    page: Page,
    allowed: frozenset[_AuthState],
    *,
    include_login: bool = False,
) -> _AuthState:
    deadline = time.monotonic() + (_STATE_TIMEOUT_MS / 1_000)
    while True:
        state = _detect_state(page, include_login=include_login)
        if state in allowed:
            assert state is not None
            return state
        remaining_ms = int((deadline - time.monotonic()) * 1_000)
        if remaining_ms <= 0:
            raise AuthenticationContractError(
                "Synergy authentication reached an unknown portal state"
            )
        page.wait_for_timeout(min(_STATE_POLL_MS, remaining_ms))


def _submit_login(page: Page, credentials: SynergyCredentials) -> None:
    email, password, submit = _login_controls(page)
    if not all(_is_visible(control) for control in (email, password, submit)):
        raise AuthenticationContractError("Synergy login controls changed")
    email.fill(credentials.email)
    password.fill(credentials.password)
    submit.click()


def _submit_otp(page: Page, otp: str) -> None:
    otp_input, submit = _otp_controls(page)
    if not (_is_visible(otp_input) and _is_visible(submit)):
        raise AuthenticationContractError("Synergy OTP controls changed")
    otp_input.fill(otp)
    submit.click()


def _valid_aura_material(token: str | None, context: str | None) -> bool:
    if not token or token.lower() in {"null", "undefined"} or not context:
        return False
    try:
        decoded = json.loads(context)
    except (json.JSONDecodeError, TypeError):
        return False
    return isinstance(decoded, dict) and decoded.keys() >= _AURA_CONTEXT_KEYS


def _dom_aura_material(page: Page) -> tuple[str, str] | None:
    token_input = page.locator('input[name="aura.token"]')
    context_input = page.locator('input[name="aura.context"]')
    if token_input.count() == 0 or context_input.count() == 0:
        return None
    token = token_input.first.input_value()
    context = context_input.first.input_value()
    if _valid_aura_material(token, context):
        return token, context
    return None


def _wait_for_aura_material(
    page: Page, observed: _ObservedAuraCredentials
) -> tuple[str, str]:
    deadline = time.monotonic() + (_AUTH_MATERIAL_TIMEOUT_MS / 1_000)
    while True:
        if _valid_aura_material(observed.aura_token, observed.aura_context):
            assert observed.aura_token is not None
            assert observed.aura_context is not None
            return observed.aura_token, observed.aura_context
        dom_material = _dom_aura_material(page)
        if dom_material is not None:
            return dom_material
        remaining_ms = int((deadline - time.monotonic()) * 1_000)
        if remaining_ms <= 0:
            raise AuthenticationContractError(
                "Synergy did not expose the required Aura credentials"
            )
        page.wait_for_timeout(min(_STATE_POLL_MS, remaining_ms))


def _sid_cookie(context: BrowserContext) -> str:
    values: set[str] = set()
    for cookie in context.cookies(_PORTAL_ORIGIN):
        value = cookie.get("value")
        if cookie.get("name") == "sid" and isinstance(value, str) and value:
            values.add(value)
    if len(values) != 1:
        raise AuthenticationContractError(
            "Synergy did not expose one transferable session cookie"
        )
    return values.pop()


def _authentication_result(
    page: Page,
    context: BrowserContext,
    observed: _ObservedAuraCredentials,
) -> _AuthenticationResult:
    token, aura_context = _wait_for_aura_material(page, observed)
    return _AuthenticationResult(
        sid=_sid_cookie(context),
        aura_token=token,
        aura_context=aura_context,
    )


def _close_browser(
    page: Page | None,
    context: BrowserContext | None,
    browser: Browser | None,
) -> None:
    if page is not None:
        with suppress(PlaywrightError):
            page.close()
    if context is not None:
        with suppress(PlaywrightError):
            context.close()
    if browser is not None:
        with suppress(PlaywrightError):
            browser.close()


def _mint_with_playwright(
    playwright: Playwright, credentials: SynergyCredentials
) -> _AuthenticationResult:
    browser: Browser | None = None
    context: BrowserContext | None = None
    page: Page | None = None
    observed = _ObservedAuraCredentials()
    try:
        browser = playwright.chromium.launch(headless=True)
        context = browser.new_context()
        page = context.new_page()
        page.on("request", lambda request: observed.observe(request))
        page.goto(_LOGIN_URL, wait_until="domcontentloaded", timeout=_STATE_TIMEOUT_MS)

        initial_state = _wait_for_state(
            page,
            frozenset(
                {
                    _AuthState.LOGIN,
                    _AuthState.AUTHENTICATED,
                    _AuthState.OTP,
                    _AuthState.CAPTCHA,
                }
            ),
            include_login=True,
        )
        if initial_state is _AuthState.CAPTCHA:
            raise UnsupportedAuthChallenge("Synergy presented an unsupported CAPTCHA")
        if initial_state is _AuthState.OTP:
            raise AuthenticationContractError(
                "Synergy requested an OTP before a freshness boundary was captured"
            )
        if initial_state is _AuthState.AUTHENTICATED:
            return _authentication_result(page, context, observed)

        with gmail_otp_mailbox(credentials) as mailbox:
            _submit_login(page, credentials)
            post_login_state = _wait_for_state(
                page,
                frozenset(
                    {
                        _AuthState.AUTHENTICATED,
                        _AuthState.OTP,
                        _AuthState.REJECTED,
                        _AuthState.CAPTCHA,
                    }
                ),
            )
            if post_login_state is _AuthState.CAPTCHA:
                raise UnsupportedAuthChallenge(
                    "Synergy presented an unsupported CAPTCHA"
                )
            if post_login_state is _AuthState.REJECTED:
                raise AuthenticationError("Synergy rejected the supplied credentials")
            if post_login_state is _AuthState.OTP:
                otp = wait_for_otp(mailbox)
                _submit_otp(page, otp)
                try:
                    post_otp_state = _wait_for_state(
                        page,
                        frozenset(
                            {
                                _AuthState.AUTHENTICATED,
                                _AuthState.REJECTED,
                                _AuthState.CAPTCHA,
                            }
                        ),
                    )
                except AuthenticationContractError:
                    otp_input, otp_submit = _otp_controls(page)
                    if _is_visible(otp_input) and _is_visible(otp_submit):
                        raise OtpRejectedError(
                            "Synergy rejected the one-time passcode"
                        ) from None
                    raise
                if post_otp_state is _AuthState.CAPTCHA:
                    raise UnsupportedAuthChallenge(
                        "Synergy presented an unsupported CAPTCHA"
                    )
                if post_otp_state is _AuthState.REJECTED:
                    raise OtpRejectedError("Synergy rejected the one-time passcode")

            return _authentication_result(page, context, observed)
    finally:
        _close_browser(page, context, browser)


def mint_http_credentials(credentials: SynergyCredentials) -> _AuthenticationResult:
    """Mint direct-HTTP credentials, closing all Playwright state before return."""

    if not isinstance(credentials, SynergyCredentials):
        raise AuthenticationError("Synergy authentication requires valid credentials")
    try:
        with sync_playwright() as playwright:
            result = _mint_with_playwright(playwright, credentials)
        return replace(result, browser_closed=True)
    except (
        AuthenticationError,
        AuthenticationContractError,
        UnsupportedAuthChallenge,
    ):
        raise
    except (PlaywrightTimeoutError, PlaywrightError, OSError):
        raise AuthenticationTransportError(
            "Synergy authentication could not reach the login service"
        ) from None
