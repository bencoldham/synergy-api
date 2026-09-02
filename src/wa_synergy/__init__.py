"""Public API for the WA Synergy client package."""

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
    "SynergyCredentials",
    "SynergyError",
    "UnsupportedAuthChallenge",
    "UsageError",
    "UsageFetchError",
    "UsageInterval",
    "UsageQuery",
    "UsageValidationError",
)
