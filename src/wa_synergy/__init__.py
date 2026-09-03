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
    SessionExpiredError,
    SynergyError,
    UnsupportedAuthChallenge,
    UsageError,
    UsageFetchError,
    UsageValidationError,
)
from .models import SyncResult, UsageInterval, UsageQuery
from .statistics import HourlyStatistic, build_hourly_import_statistics
from .storage import get_usage_intervals
from .sync import sync_usage_to_db

__all__ = (
    "AuthenticationContractError",
    "AuthenticationError",
    "ConfigurationError",
    "HourlyStatistic",
    "OtpError",
    "OtpMailboxError",
    "OtpParseError",
    "OtpRejectedError",
    "OtpTimeoutError",
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
    "build_hourly_import_statistics",
    "get_usage_intervals",
    "sync_usage_to_db",
)
