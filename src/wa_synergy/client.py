"""Serialized ownership of the reusable direct-HTTP Synergy session."""

from __future__ import annotations

from threading import RLock
from types import TracebackType

import httpx

from .auth import _AuthenticationResult, mint_http_credentials
from .config import SynergyCredentials
from .errors import ConfigurationError, SessionExpiredError, UsageFetchError
from .models import UsageInterval, UsageQuery
from .usage import (
    _AuthenticationLost,
    create_http_client,
    fetch_usage,
)


class SynergyClient:
    """Synchronous Synergy client with memory-only direct-HTTP credentials.

    Public operations are serialized because an Aura token, its context, and the
    corresponding ``sid`` cookie form one mutable provider session. Authentication uses
    Playwright only while minting that material; usage always travels through the owned
    :class:`httpx.Client` after the browser has closed.
    """

    __slots__ = (
        "_authentication",
        "_closed",
        "_credentials",
        "_http_client",
        "_interactive_auth",
        "_operation_lock",
    )

    def __init__(
        self,
        *,
        credentials: SynergyCredentials,
        interactive_auth: bool = True,
    ) -> None:
        if not isinstance(credentials, SynergyCredentials):
            raise ConfigurationError("SynergyClient requires SynergyCredentials")
        if not isinstance(interactive_auth, bool):
            raise ConfigurationError("interactive_auth must be boolean")
        self._credentials = credentials
        self._interactive_auth = interactive_auth
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
            interactive=self._interactive_auth,
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
        """Fetch a complete normalized range, reminting credentials at most once."""

        if not isinstance(query, UsageQuery):
            raise ConfigurationError("get_usage requires a UsageQuery")
        with self._operation_lock:
            self._require_open()
            client, authentication = self._current_http_session()
            try:
                return fetch_usage(client, authentication, query)
            except _AuthenticationLost:
                self._discard_http_session()

            client, authentication = self._current_http_session()
            try:
                return fetch_usage(client, authentication, query)
            except _AuthenticationLost:
                self._discard_http_session()
                raise SessionExpiredError(
                    "Synergy direct HTTP session remained invalid after refresh"
                ) from None

    def close(self) -> None:
        """Close and discard all direct-HTTP state; repeated calls are harmless."""

        with self._operation_lock:
            if self._closed:
                return
            self._closed = True
            self._discard_http_session()
