"""Public exception hierarchy for :mod:`wa_synergy`.

Exception messages must describe only the failed operation or contract state. Provider
responses, credentials, OTPs, cookies, Aura material, and mailbox content must never be
attached to these exceptions.
"""


class SynergyError(Exception):
    """Base class for every error intentionally exposed by the package."""


class ConfigurationError(SynergyError, ValueError):
    """Local credentials, query, or storage configuration is invalid."""


class AuthenticationError(SynergyError):
    """Synergy authentication could not be completed."""


class AuthenticationTransportError(AuthenticationError):
    """The login service could not be reached safely."""


class PortalContractError(SynergyError):
    """The provider no longer matches a required, previously observed contract."""


class AuthenticationContractError(AuthenticationError, PortalContractError):
    """Required direct-HTTP authentication material could not be minted."""


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


class SessionExpiredError(AuthenticationError):
    """The direct HTTP session remained invalid after one credential refresh."""


class AuthorizationError(SynergyError):
    """The authenticated account cannot access the requested resource."""


class UsageError(SynergyError):
    """Base class for usage retrieval and normalization failures."""


class UsageFetchError(UsageError):
    """A direct usage request failed without indicating session expiry."""


class UsageValidationError(UsageError):
    """A provider response cannot be normalized without guessing."""


class StorageError(SynergyError):
    """Normalized usage could not be persisted transactionally."""
