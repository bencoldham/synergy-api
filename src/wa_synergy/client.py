"""Serialized ownership of the reusable direct-HTTP Synergy session."""

from __future__ import annotations

from threading import RLock
from types import TracebackType

import httpx

from .auth import _AuthenticationResult, mint_http_credentials
from .config import SynergyCredentials
from .errors import ConfigurationError, UsageFetchError
from .models import UsageInterval, UsageQuery
from .usage import create_http_client, fetch_usage


class SynergyClient:
    """Log in once, then use one direct HTTP session for usage calls."""

    __slots__ = (
        "_authentication",
        "_closed",
        "_credentials",
        "_headless",
        "_http_client",
        "_operation_lock",
    )

    def __init__(
        self,
        *,
        credentials: SynergyCredentials,
        headless: bool = True,
        interactive_auth: bool | None = None,
    ) -> None:
        if not isinstance(credentials, SynergyCredentials):
            raise ConfigurationError("SynergyClient requires SynergyCredentials")
        if not isinstance(headless, bool):
            raise ConfigurationError("headless must be boolean")
        if interactive_auth is not None:
            if not isinstance(interactive_auth, bool):
                raise ConfigurationError("interactive_auth must be boolean")
            headless = not interactive_auth
        self._credentials = credentials
        self._headless = headless
        self._operation_lock = RLock()
        self._authentication: _AuthenticationResult | None = None
        self._http_client: httpx.Client | None = None
        self._closed = False

    def __enter__(self) -> SynergyClient:
        with self._operation_lock:
            self._require_open()
            return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()

    @property
    def headless(self) -> bool:
        return self._headless

    @property
    def interactive_auth(self) -> bool:
        return not self._headless

    def _require_open(self) -> None:
        if self._closed:
            raise UsageFetchError("SynergyClient is closed")

    def _discard_http_session(self) -> None:
        client = self._http_client
        self._http_client = None
        self._authentication = None
        if client is not None:
            client.close()

    def _mint_http_session(self) -> None:
        authentication = mint_http_credentials(
            self._credentials,
            headless=self._headless,
        )
        client = create_http_client(authentication)
        self._authentication = authentication
        self._http_client = client

    def _current_http_session(self) -> tuple[httpx.Client, _AuthenticationResult]:
        if self._http_client is None or self._authentication is None:
            self._mint_http_session()
        client = self._http_client
        authentication = self._authentication
        if client is None or authentication is None:
            raise UsageFetchError("Direct HTTP session could not be created")
        return client, authentication

    def get_usage(self, query: UsageQuery) -> tuple[UsageInterval, ...]:
        """Fetch usage with the token captured during login."""

        if not isinstance(query, UsageQuery):
            raise ConfigurationError("get_usage requires a UsageQuery")
        with self._operation_lock:
            self._require_open()
            client, authentication = self._current_http_session()
            return fetch_usage(client, authentication, query)
    def get_daily_usage(self, query: UsageQuery) -> tuple[UsageInterval, ...]:
        """Fetch daily usage (hourly/half-hourly intervals) with the token captured during login."""
        if not isinstance(query, UsageQuery):
            raise ConfigurationError("get_daily_usage requires a UsageQuery")
        if query.interval_type != "DAILY":
            query = UsageQuery(
                start=query.start,
                end=query.end,
                account_ids=query.account_ids,
                service_point_ids=query.service_point_ids,
                interval_type="DAILY",
            )
        return self.get_usage(query)


    def close(self) -> None:
        """Close and discard all direct-HTTP state; repeated calls are harmless."""

        with self._operation_lock:
            if self._closed:
                return
            self._closed = True
            self._discard_http_session()
