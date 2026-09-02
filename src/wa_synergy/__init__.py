"""Public API for the WA Synergy client package."""

from .client import SynergyClient
from .config import SynergyCredentials
from .errors import (
    AuthenticationContractError,
    AuthenticationError,
    ConfigurationError,
    OtpError,
    OtpMailboxError,
    OtpParseError,
    OtpRejectedError,
    OtpTimeoutError,
    StorageError,
    SynergyError,
    UnsupportedAuthChallenge,
    UsageError,
    UsageFetchError,
    UsageValidationError,
)
from .models import SyncResult, UsageInterval, UsageQuery
from .sync import find_missing_date_ranges, sync_daily_usage_to_db, sync_usage_to_db

__all__ = (
    "AuthenticationContractError",
    "AuthenticationError",
    "ConfigurationError",
    "OtpError",
    "OtpMailboxError",
    "OtpParseError",
    "OtpRejectedError",
    "OtpTimeoutError",
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
    "find_missing_date_ranges",
    "sync_daily_usage_to_db",
    "sync_usage_to_db",
)
