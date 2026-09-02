"""Validated, immutable caller-supplied configuration."""

from dataclasses import dataclass, field

from .errors import ConfigurationError


@dataclass(frozen=True, slots=True)
class SynergyCredentials:
    """Credentials for one dedicated personal Gmail/Synergy account.

    The Synergy login address and both secrets are omitted from ``repr`` so routine
    debugging cannot disclose them. Secrets remain memory-only; this class performs
    no environment-variable loading or persistence.
    """

    email: str = field(repr=False)
    password: str = field(repr=False)
    gmail_app_password: str = field(repr=False)

    def __post_init__(self) -> None:
        if (
            not isinstance(self.email, str)
            or self.email != self.email.strip()
            or any(character.isspace() for character in self.email)
            or self.email.count("@") != 1
        ):
            raise ConfigurationError(
                "Synergy email must be a dedicated personal Gmail address"
            )
        local, _, domain = self.email.partition("@")
        if not local or domain.lower() != "gmail.com":
            raise ConfigurationError(
                "Synergy email must be a dedicated personal Gmail address"
            )
        if not isinstance(self.password, str) or not self.password:
            raise ConfigurationError("Synergy password must be present")
        if not isinstance(self.gmail_app_password, str) or not self.gmail_app_password:
            raise ConfigurationError("Gmail app password must be present")
