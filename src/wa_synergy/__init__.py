"""Public API for the WA Synergy client package."""

from .client import SynergyClient
from .config import SynergyCredentials
from .errors import (
    AuthenticationContractError,
    AuthenticationError,
    AuthenticationTransportError,
    AuthorizationError,
    ConfigurationError,
    OtpError,
    OtpMailboxError,
    OtpParseError,
    OtpRejectedError,
    OtpTimeoutError,
    PortalContractError,
    SessionExpiredError,
    StorageError,
    SynergyError,
    UnsupportedAuthChallenge,
    UsageError,
    UsageFetchError,
    UsageValidationError,
)
from .models import SyncResult, UsageInterval, UsageQuery
from .sync import sync_usage_to_db

__all__ = (
    "AuthenticationContractError",
    "AuthenticationError",
    "AuthenticationTransportError",
    "AuthorizationError",
    "ConfigurationError",
    "OtpError",
    "OtpMailboxError",
    "OtpParseError",
    "OtpRejectedError",
    "OtpTimeoutError",
    "PortalContractError",
    "SessionExpiredError",
    "StorageError",
    "SyncResult",
    "SynergyClient",
    "SynergyCredentials",
    "SynergyError",
    "UnsupportedAuthChallenge",
    "UsageError",
    "UsageFetchError",
    "UsageInterval",
    "UsageQuery",
    "UsageValidationError",
    "sync_usage_to_db",
)
