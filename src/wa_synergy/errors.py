"""Exceptions for failures that need package-specific handling."""


class SynergyError(Exception):
    """Base class for every error intentionally exposed by the package."""


class ConfigurationError(SynergyError, ValueError):
    """Local credentials, query, or storage configuration is invalid."""


class AuthenticationError(SynergyError):
    """Synergy authentication could not be completed."""


class AuthenticationContractError(AuthenticationError):
    """Required login page or token material was missing."""


class UnsupportedAuthChallenge(AuthenticationError):
    """Authentication requires an unsupported challenge, such as CAPTCHA."""


class OtpError(AuthenticationError):
    """Base class for one-time-passcode failures."""


class OtpMailboxError(OtpError):
    """The dedicated Gmail mailbox could not be accessed safely."""


class OtpTimeoutError(OtpError, TimeoutError):
    """No unambiguous, fresh Synergy OTP arrived before the deadline."""


class OtpParseError(OtpError):
    """A candidate Synergy message did not contain the required OTP format."""


class OtpRejectedError(OtpError):
    """Synergy rejected the submitted OTP."""


class UsageError(SynergyError):
    """Base class for usage retrieval and normalization failures."""


class UsageFetchError(UsageError):
    """A direct usage request failed without indicating session expiry."""


class UsageValidationError(UsageError):
    """A provider response cannot be normalized without guessing."""


class StorageError(SynergyError):
    """Normalized usage could not be persisted transactionally."""
