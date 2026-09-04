"""Short-lived Playwright authentication and direct-HTTP credential handoff."""

from __future__ import annotations

import json
import os
import re
import secrets
import subprocess
import time
from contextlib import suppress
from dataclasses import dataclass, field
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
from playwright_stealth import Stealth  # type: ignore[import-untyped]

from .config import SynergyCredentials
from .errors import (
    AuthenticationContractError,
    AuthenticationError,
    OtpRejectedError,
    UnsupportedAuthChallenge,
)
from .otp import gmail_otp_mailbox, wait_for_otp

_LOGIN_URL = "https://my.synergy.net.au/s/login/"
_PORTAL_ORIGIN = "https://my.synergy.net.au"
_AURA_PATH = "/s/sfsites/aura"
_DASHBOARD_PATH = "/s/service-dashboard"
_STATE_TIMEOUT_MS = 30_000
_STATE_POLL_MS = 100
_LOGIN_RETRY_STABILITY_MS = 2_000
_AUTH_MATERIAL_TIMEOUT_MS = 30_000
_AURA_CONTEXT_KEYS = frozenset(
    {"mode", "fwuid", "app", "loaded", "dn", "globals", "uad"}
)
_OTP_SUBMIT_NAME = re.compile(r"^(?:verify|submit|continue)$", re.IGNORECASE)
_WELCOME_NAME = re.compile(r"^Welcome,", re.IGNORECASE)
_AUTH_FAILURE_TEXT = re.compile(
    r"(?:invalid|incorrect|locked|unable to log in|check your (?:email|password))",
    re.IGNORECASE,
)
_CAPTCHA_TEXT = re.compile(r"\bcaptcha\b", re.IGNORECASE)
_RETRYABLE_LOGIN_TEXT = re.compile(
    r"^Sorry, Please refresh the page and try again\.$",
    re.IGNORECASE,
)
_STEALTH = Stealth(
    chrome_runtime=True,
    navigator_languages_override=("en-AU", "en"),
    navigator_platform_override="Linux x86_64",
)


@dataclass(frozen=True, slots=True)
class _AuthenticationResult:
    """Minimum memory-only material required by the direct Aura transport."""

    sid: str = field(repr=False)
    aura_token: str = field(repr=False)
    aura_context: str = field(repr=False)
    service_id: str


@dataclass(slots=True)
class _ObservedAuraCredentials:
    aura_token: str | None = field(default=None, repr=False)
    aura_context: str | None = field(default=None, repr=False)

    def observe(self, request: Request, *, page_url: str) -> None:
        """Retain credentials only from dashboard-originated Aura form requests."""

        if urlsplit(page_url).path.rstrip("/") != _DASHBOARD_PATH:
            return
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


class _AuthState(Enum):
    LOGIN = auto()
    AUTHENTICATED = auto()
    OTP = auto()
    OTP_METHOD = auto()
    REJECTED = auto()
    CAPTCHA = auto()
    RETRYABLE_LOGIN = auto()


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
        page.locator("#otp-field, #otp"),
        page.get_by_role("button", name=_OTP_SUBMIT_NAME),
    )


def _otp_method_controls(page: Page) -> tuple[Locator, Locator]:
    return (
        page.get_by_role("button", name="Email", exact=True),
        page.get_by_role("button", name="SMS", exact=True),
    )


def _is_authenticated(page: Page) -> bool:
    path = urlsplit(page.url).path.rstrip("/")
    if path != _DASHBOARD_PATH:
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
    email_method, sms_method = _otp_method_controls(page)
    if _is_visible(email_method) and _is_visible(sms_method):
        return _AuthState.OTP_METHOD
    if _is_visible(page.get_by_text(_AUTH_FAILURE_TEXT)):
        return _AuthState.REJECTED
    if _is_visible(page.get_by_text(_RETRYABLE_LOGIN_TEXT)):
        return _AuthState.RETRYABLE_LOGIN
    if include_login and all(_is_visible(control) for control in _login_controls(page)):
        return _AuthState.LOGIN
    return None


def _wait_for_state(
    page: Page,
    allowed: frozenset[_AuthState],
    *,
    include_login: bool = False,
    stable_login_as_retryable: bool = False,
    timeout_ms: int | None = None,
) -> _AuthState:
    effective_timeout_ms = _STATE_TIMEOUT_MS if timeout_ms is None else timeout_ms
    deadline = time.monotonic() + (effective_timeout_ms / 1_000)
    login_visible_since: float | None = None
    while True:
        now = time.monotonic()
        state = _detect_state(
            page,
            include_login=include_login or stable_login_as_retryable,
        )
        if stable_login_as_retryable and state is _AuthState.LOGIN:
            if login_visible_since is None:
                login_visible_since = now
            elif (now - login_visible_since) * 1_000 >= _LOGIN_RETRY_STABILITY_MS:
                return _AuthState.RETRYABLE_LOGIN
            state = None
        else:
            login_visible_since = None
        if state in allowed:
            assert state is not None
            return state
        remaining_ms = int((deadline - now) * 1_000)
        if remaining_ms <= 0:
            expected = ", ".join(sorted(state.name for state in allowed))
            raise AuthenticationContractError(
                f"Timed out at {page.url!r}; expected Synergy state: {expected}"
            )
        page.wait_for_timeout(min(_STATE_POLL_MS, remaining_ms))


def _move_pointer(page: Page, control: Locator) -> tuple[float, float]:
    box = control.bounding_box()
    if box is None:
        raise AuthenticationContractError("Synergy login control is not actionable")
    target_x = box["x"] + box["width"] * (0.35 + secrets.randbelow(31) / 100)
    target_y = box["y"] + box["height"] * (0.35 + secrets.randbelow(31) / 100)
    start_x = 20.0 + secrets.randbelow(181)
    start_y = 20.0 + secrets.randbelow(181)
    control_x = (start_x + target_x) / 2 + secrets.randbelow(81) - 40
    control_y = (start_y + target_y) / 2 + secrets.randbelow(81) - 40
    page.mouse.move(start_x, start_y)
    steps = 20 + secrets.randbelow(16)
    for step in range(1, steps + 1):
        progress = step / steps
        inverse = 1 - progress
        x = (
            inverse * inverse * start_x
            + 2 * inverse * progress * control_x
            + progress * progress * target_x
        )
        y = (
            inverse * inverse * start_y
            + 2 * inverse * progress * control_y
            + progress * progress * target_y
        )
        page.mouse.move(x, y)
        page.wait_for_timeout(8 + secrets.randbelow(18))
    return target_x, target_y


def _human_click(page: Page, control: Locator) -> None:
    _move_pointer(page, control)
    page.wait_for_timeout(80 + secrets.randbelow(181))
    page.mouse.down()
    page.wait_for_timeout(70 + secrets.randbelow(131))
    page.mouse.up()


def _human_fill(page: Page, control: Locator, value: str) -> None:
    _human_click(page, control)
    control.press("Control+A")
    control.press("Backspace")
    control.press_sequentially(value, delay=45 + secrets.randbelow(46))
    page.wait_for_timeout(100 + secrets.randbelow(301))


def _submit_login(page: Page, credentials: SynergyCredentials) -> None:
    email, password, submit = _login_controls(page)
    if not all(_is_visible(control) for control in (email, password, submit)):
        raise AuthenticationContractError("Synergy login controls changed")
    _human_fill(page, email, credentials.email)
    _human_fill(page, password, credentials.password)
    _human_click(page, submit)


def _submit_repeated_login(page: Page, credentials: SynergyCredentials) -> None:
    email, password, submit = _login_controls(page)
    if not all(_is_visible(control) for control in (email, password, submit)):
        raise AuthenticationContractError("Synergy login controls changed")
    _human_fill(page, email, credentials.email)
    _human_fill(page, password, credentials.password)
    _move_pointer(page, submit)
    for _attempt in range(12):
        page.mouse.down()
        page.wait_for_timeout(60 + secrets.randbelow(91))
        page.mouse.up()
        page.wait_for_timeout(180 + secrets.randbelow(221))
        if urlsplit(page.url).path.rstrip("/") != "/s/login":
            return


def _submit_otp_method(page: Page) -> None:
    email_method, sms_method = _otp_method_controls(page)
    if not (_is_visible(email_method) and _is_visible(sms_method)):
        raise AuthenticationContractError("Synergy OTP method controls changed")
    email_method.click()


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


def _wait_for_aura_material(
    page: Page, observed: _ObservedAuraCredentials
) -> tuple[str, str]:
    deadline = time.monotonic() + (_AUTH_MATERIAL_TIMEOUT_MS / 1_000)
    while True:
        if _valid_aura_material(observed.aura_token, observed.aura_context):
            assert observed.aura_token is not None
            assert observed.aura_context is not None
            return observed.aura_token, observed.aura_context
        remaining_ms = int((deadline - time.monotonic()) * 1_000)
        if remaining_ms <= 0:
            raise AuthenticationContractError(
                f"Authenticated page {page.url!r} did not issue a dashboard Aura "
                f"request with valid credentials within "
                f"{_AUTH_MATERIAL_TIMEOUT_MS} ms"
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
            f"Expected one non-empty sid cookie for {_PORTAL_ORIGIN}, "
            f"found {len(values)}"
        )
    return values.pop()


def _authentication_result(
    page: Page,
    context: BrowserContext,
    observed: _ObservedAuraCredentials,
) -> _AuthenticationResult:
    token, aura_context = _wait_for_aura_material(page, observed)
    service_ids = parse_qs(urlsplit(page.url).query).get("c__service", [])
    if len(service_ids) != 1 or not service_ids[0]:
        raise AuthenticationContractError(
            f"Authenticated dashboard URL has no single c__service value: {page.url!r}"
        )
    return _AuthenticationResult(
        sid=_sid_cookie(context),
        aura_token=token,
        aura_context=aura_context,
        service_id=service_ids[0],
    )


def _browser_user_agent(playwright: Playwright) -> str:
    raw = subprocess.check_output(
        [playwright.chromium.executable_path, "--version"],
        text=True,
    )
    version = raw.strip().split()[-1]
    return (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
        f"Chrome/{version} Safari/537.36"
    )


def _mint_with_playwright(
    playwright: Playwright,
    credentials: SynergyCredentials,
    *,
    headless: bool,
) -> _AuthenticationResult:
    browser: Browser | None = None
    context: BrowserContext | None = None
    observed = _ObservedAuraCredentials()
    try:
        user_agent = _browser_user_agent(playwright)
        launch_args = [
            "--disable-blink-features=AutomationControlled",
            f"--user-agent={user_agent}",
        ]
        if not headless:
            wayland_display = os.environ.get("WAYLAND_DISPLAY")
            if wayland_display:
                launch_args.append("--ozone-platform=wayland")
        browser = playwright.chromium.launch(
            headless=headless,
            args=launch_args,
        )
        context = browser.new_context(
            user_agent=user_agent,
            locale="en-AU",
            timezone_id="Australia/Perth",
            viewport={"width": 1365, "height": 768},
        )
        _STEALTH.apply_stealth_sync(context)
        page = context.new_page()
        page.on(
            "request",
            lambda request: observed.observe(request, page_url=page.url),
        )
        page.goto(_LOGIN_URL, wait_until="domcontentloaded", timeout=_STATE_TIMEOUT_MS)

        initial_state = _wait_for_state(
            page,
            frozenset(
                {
                    _AuthState.LOGIN,
                    _AuthState.AUTHENTICATED,
                    _AuthState.OTP,
                    _AuthState.OTP_METHOD,
                    _AuthState.CAPTCHA,
                }
            ),
            include_login=True,
        )
        if initial_state is _AuthState.CAPTCHA:
            raise UnsupportedAuthChallenge("Synergy presented an unsupported CAPTCHA")
        if initial_state in {_AuthState.OTP, _AuthState.OTP_METHOD}:
            raise AuthenticationContractError(
                "Synergy requested an OTP before a freshness boundary was captured"
            )
        if initial_state is _AuthState.AUTHENTICATED:
            return _authentication_result(page, context, observed)

        with gmail_otp_mailbox(credentials) as mailbox:
            _submit_login(page, credentials)
            post_login_states = frozenset(
                {
                    _AuthState.AUTHENTICATED,
                    _AuthState.OTP,
                    _AuthState.OTP_METHOD,
                    _AuthState.REJECTED,
                    _AuthState.CAPTCHA,
                }
            )
            post_login_state = _wait_for_state(
                page,
                post_login_states | {_AuthState.RETRYABLE_LOGIN},
                stable_login_as_retryable=True,
            )
            if post_login_state is _AuthState.RETRYABLE_LOGIN:
                page.reload(
                    wait_until="domcontentloaded",
                    timeout=_STATE_TIMEOUT_MS,
                )
                retry_state = _wait_for_state(
                    page,
                    frozenset({_AuthState.LOGIN, _AuthState.CAPTCHA}),
                    include_login=True,
                )
                if retry_state is _AuthState.CAPTCHA:
                    post_login_state = retry_state
                else:
                    _submit_repeated_login(page, credentials)
                    post_login_state = _wait_for_state(
                        page,
                        post_login_states,
                    )
            if post_login_state is _AuthState.CAPTCHA:
                raise UnsupportedAuthChallenge(
                    "Synergy presented an unsupported CAPTCHA"
                )
            if post_login_state is _AuthState.REJECTED:
                messages = [
                    locator.inner_text().strip()
                    for index in range(page.get_by_text(_AUTH_FAILURE_TEXT).count())
                    if (
                        (
                            locator := page.get_by_text(_AUTH_FAILURE_TEXT).nth(index)
                        ).is_visible()
                    )
                    and locator.inner_text().strip()
                ]
                detail = " | ".join(messages) or "failure indicator was visible"
                raise AuthenticationError(
                    f"Synergy rejected login at {page.url!r}: {detail}"
                )
            if post_login_state is _AuthState.OTP_METHOD:
                _submit_otp_method(page)
                post_login_state = _wait_for_state(
                    page,
                    frozenset(
                        {
                            _AuthState.OTP,
                            _AuthState.REJECTED,
                            _AuthState.CAPTCHA,
                        }
                    ),
                )
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
                except AuthenticationContractError as exc:
                    otp_input, otp_submit = _otp_controls(page)
                    if _is_visible(otp_input) and _is_visible(otp_submit):
                        raise OtpRejectedError(
                            f"Synergy OTP controls remained visible after submission at "
                            f"{page.url!r}: {exc}"
                        ) from exc
                    raise
                if post_otp_state is _AuthState.CAPTCHA:
                    raise UnsupportedAuthChallenge(
                        "Synergy presented an unsupported CAPTCHA"
                    )
                if post_otp_state is _AuthState.REJECTED:
                    raise OtpRejectedError("Synergy rejected the one-time passcode")

            return _authentication_result(page, context, observed)
    finally:
        if context is not None:
            with suppress(PlaywrightError):
                context.close()
        if browser is not None:
            with suppress(PlaywrightError):
                browser.close()


def mint_http_credentials(
    credentials: SynergyCredentials,
    *,
    headless: bool = True,
) -> _AuthenticationResult:
    """Mint direct-HTTP credentials, closing all Playwright state before return."""

    if not isinstance(credentials, SynergyCredentials):
        raise AuthenticationError("Synergy authentication requires valid credentials")
    if not isinstance(headless, bool):
        raise AuthenticationError("Synergy headless parameter must be boolean")
    try:
        with sync_playwright() as playwright:
            return _mint_with_playwright(
                playwright,
                credentials,
                headless=headless,
            )
    except PlaywrightError as exc:
        raise AuthenticationError(
            f"Synergy browser authentication failed: {exc}"
        ) from exc
