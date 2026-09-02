import unittest
from dataclasses import FrozenInstanceError

import wa_synergy
from wa_synergy import ConfigurationError, SynergyCredentials, UsageQuery


class SynergyCredentialsTests(unittest.TestCase):
    def test_accepts_a_dedicated_personal_gmail_address(self) -> None:
        credentials = SynergyCredentials(
            email="dedicated.account@gmail.com",
            password="synergy-password-sentinel",
            gmail_app_password="gmail-app-password-sentinel",
        )

        self.assertEqual(credentials.email, "dedicated.account@gmail.com")

    def test_repr_excludes_email_and_both_secrets(self) -> None:
        sentinels = (
            "private-address@gmail.com",
            "synergy-password-sentinel",
            "gmail-app-password-sentinel",
        )
        credentials = SynergyCredentials(
            email=sentinels[0],
            password=sentinels[1],
            gmail_app_password=sentinels[2],
        )

        representation = repr(credentials)
        self.assertEqual(representation, "SynergyCredentials()")
        for sentinel in sentinels:
            self.assertNotIn(sentinel, representation)

    def test_rejects_non_personal_gmail_addresses_without_echoing_input(self) -> None:
        invalid_addresses = (
            "account@example.com",
            "account@googlemail.com",
            "account@gmail.com ",
            "@gmail.com",
            "account",
        )
        for address in invalid_addresses:
            with self.subTest(address=address):
                with self.assertRaises(ConfigurationError) as raised:
                    SynergyCredentials(
                        email=address,
                        password="synergy-password-sentinel",
                        gmail_app_password="gmail-app-password-sentinel",
                    )
                self.assertNotIn(address, str(raised.exception))

    def test_requires_both_secrets_without_echoing_them(self) -> None:
        cases = (
            {"password": "", "gmail_app_password": "gmail-app-password-sentinel"},
            {"password": "synergy-password-sentinel", "gmail_app_password": ""},
        )
        for secrets in cases:
            with self.subTest(secrets=secrets):
                with self.assertRaises(ConfigurationError) as raised:
                    SynergyCredentials(email="dedicated@gmail.com", **secrets)
                message = str(raised.exception)
                self.assertNotIn("synergy-password-sentinel", message)
                self.assertNotIn("gmail-app-password-sentinel", message)

    def test_credentials_are_immutable_and_slotted(self) -> None:
        credentials = SynergyCredentials(
            email="dedicated@gmail.com",
            password="synergy-password-sentinel",
            gmail_app_password="gmail-app-password-sentinel",
        )

        with self.assertRaises(FrozenInstanceError):
            credentials.password = "replacement"  # type: ignore[misc]
        with self.assertRaises((AttributeError, TypeError)):
            credentials.unexpected = True  # type: ignore[attr-defined]


class PublicExportsTests(unittest.TestCase):
    def test_implemented_contract_is_available_from_package_root(self) -> None:
        self.assertIs(wa_synergy.SynergyCredentials, SynergyCredentials)
        self.assertIs(wa_synergy.UsageQuery, UsageQuery)
        self.assertIn("UsageInterval", wa_synergy.__all__)
        self.assertIn("SyncResult", wa_synergy.__all__)
        self.assertIn("SynergyError", wa_synergy.__all__)


if __name__ == "__main__":
    unittest.main()
