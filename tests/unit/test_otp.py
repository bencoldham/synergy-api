from __future__ import annotations

import imaplib
import ssl
import unittest
from email import policy
from email.message import EmailMessage
from pathlib import Path
from unittest.mock import patch

from wa_synergy.config import SynergyCredentials
from wa_synergy.errors import OtpMailboxError, OtpParseError, OtpTimeoutError
from wa_synergy.otp import gmail_otp_mailbox, wait_for_otp

_FIXTURES = Path(__file__).parents[1] / "fixtures" / "otp"
_EXPECTED_SUBJECT = "Your Synergy online one-time passcode"


def _fixture(name: str) -> bytes:
    return (_FIXTURES / name).read_bytes()


def _matching_message(body: str) -> bytes:
    return (
        "From: Synergy <info@synergy.net.au>\n"
        f"Subject: {_EXPECTED_SUBJECT}\n"
        "Date: Sat, 08 Aug 2026 04:00:00 +0000\n"
        "Message-ID: <synthetic-otp@example.invalid>\n"
        "MIME-Version: 1.0\n"
        'Content-Type: text/plain; charset="utf-8"\n'
        "\n"
        f"{body}"
    ).encode()


def _multipart_encoded_message(code: str) -> bytes:
    message = EmailMessage()
    message["From"] = "Synergy <info@synergy.net.au>"
    message["To"] = "dedicated.account@gmail.com"
    message["Subject"] = _EXPECTED_SUBJECT
    message["Date"] = "Sat, 08 Aug 2026 04:03:00 +0000"
    message["Message-ID"] = "<encoded@example.invalid>"
    message.set_content(
        f"Your passcode follows.\n\n{code}\n",
        cte="quoted-printable",
    )
    message.add_alternative(
        f"<html><body><p>Your passcode follows.</p><p>{code}</p></body></html>",
        subtype="html",
        cte="base64",
    )
    return message.as_bytes(policy=policy.SMTP)


def _header_bytes(document: bytes) -> bytes:
    for separator in (b"\r\n\r\n", b"\n\n"):
        header, found, _body = document.partition(separator)
        if found:
            return header + separator
    return document


class FakeImap:
    def __init__(
        self,
        messages: dict[int, bytes] | None = None,
        *,
        uid_next: int = 8,
        search_results: list[list[int]] | None = None,
    ) -> None:
        self.messages = messages or {}
        self.uid_next = uid_next
        self.uid_validity = 42
        self.search_results = search_results
        self.search_count = 0
        self.fetches: list[tuple[int, str]] = []
        self.login_arguments: tuple[str, str] | None = None
        self.select_arguments: tuple[str, bool] | None = None
        self.logged_out = False
        self.login_error: Exception | None = None
        self.select_status = "OK"

    def login(self, email: str, password: str) -> tuple[str, list[bytes]]:
        self.login_arguments = (email, password)
        if self.login_error is not None:
            raise self.login_error
        return "OK", [b"authenticated"]

    def select(self, folder: str, readonly: bool = False) -> tuple[str, list[bytes]]:
        self.select_arguments = (folder, readonly)
        return self.select_status, [str(len(self.messages)).encode()]

    def response(self, name: str) -> tuple[str, list[bytes]]:
        values = {
            "UIDVALIDITY": self.uid_validity,
            "UIDNEXT": self.uid_next,
        }
        return name, [str(values[name]).encode()]

    def uid(self, command: str, *arguments: object) -> tuple[str, list[object]]:
        if command == "search":
            self.search_count += 1
            self.assert_search_arguments(arguments)
            if self.search_results is None:
                result = sorted(self.messages)
            else:
                index = min(self.search_count - 1, len(self.search_results) - 1)
                result = self.search_results[index]
            return "OK", [" ".join(str(uid) for uid in result).encode()]
        if command == "fetch":
            uid = int(str(arguments[0]))
            query = str(arguments[1])
            self.fetches.append((uid, query))
            document = self.messages[uid]
            if "HEADER.FIELDS" in query:
                document = _header_bytes(document)
            return "OK", [(b"synthetic fetch metadata", document), b")"]
        raise AssertionError(f"unexpected UID command: {command}")

    def assert_search_arguments(self, arguments: tuple[object, ...]) -> None:
        if arguments != (None, f"UID {self.uid_next}:*"):
            raise AssertionError(f"unexpected search arguments: {arguments!r}")

    def logout(self) -> tuple[str, list[bytes]]:
        self.logged_out = True
        return "BYE", [b"logout"]


class FakeClock:
    def __init__(self) -> None:
        self.now = 100.0
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.now

    def sleep(self, duration: float) -> None:
        self.sleeps.append(duration)
        self.now += duration


class OtpTests(unittest.TestCase):
    def setUp(self) -> None:
        self.synergy_password = "SYNERGY-PASSWORD-SENTINEL"
        self.gmail_password = "GMAIL-APP-PASSWORD-SENTINEL"
        self.credentials = SynergyCredentials(
            email="dedicated.account@gmail.com",
            password=self.synergy_password,
            gmail_app_password=self.gmail_password,
        )

    def open_and_wait(
        self,
        fake: FakeImap,
        *,
        timeout: float = 1.0,
        poll_interval: float = 0.25,
        clock: FakeClock | None = None,
    ) -> str:
        active_clock = clock or FakeClock()
        with (
            patch("wa_synergy.otp.imaplib.IMAP4_SSL", return_value=fake),
            patch("wa_synergy.otp.time.monotonic", active_clock.monotonic),
            patch("wa_synergy.otp.time.sleep", active_clock.sleep),
            gmail_otp_mailbox(self.credentials) as mailbox,
        ):
            return wait_for_otp(
                mailbox,
                timeout=timeout,
                poll_interval=poll_interval,
            )

    def test_uses_fixed_verified_tls_read_only_inbox_and_logs_out(self) -> None:
        fake = FakeImap()
        with (
            patch(
                "wa_synergy.otp.imaplib.IMAP4_SSL",
                return_value=fake,
            ) as constructor,
            gmail_otp_mailbox(self.credentials) as mailbox,
        ):
            self.assertEqual((mailbox.uid_validity, mailbox.uid_next), (42, 8))
            self.assertIsNotNone(mailbox.authentication_started_at.tzinfo)
            self.assertNotIn(self.gmail_password, repr(mailbox))

        constructor.assert_called_once()
        args, kwargs = constructor.call_args
        self.assertEqual(args, ("imap.gmail.com", 993))
        tls_context = kwargs["ssl_context"]
        self.assertEqual(tls_context.verify_mode, ssl.CERT_REQUIRED)
        self.assertTrue(tls_context.check_hostname)
        self.assertEqual(
            fake.login_arguments,
            ("dedicated.account@gmail.com", self.gmail_password),
        )
        self.assertNotIn(self.synergy_password, fake.login_arguments)
        self.assertEqual(fake.select_arguments, ("INBOX", True))
        self.assertTrue(fake.logged_out)

    def test_plain_text_otp_and_header_first_allowlist_filtering(self) -> None:
        fake = FakeImap(
            {
                8: _fixture("valid_plain.eml"),
                9: _fixture("unrelated.eml"),
            }
        )

        code = self.open_and_wait(fake)

        self.assertEqual(code, "482731")
        self.assertEqual([uid for uid, _query in fake.fetches], [9, 8, 8])
        self.assertIn("HEADER.FIELDS", fake.fetches[0][1])
        self.assertIn("HEADER.FIELDS", fake.fetches[1][1])
        self.assertEqual(fake.fetches[2][1], "(BODY.PEEK[])")
        self.assertTrue(all("BODY.PEEK" in query for _uid, query in fake.fetches))

    def test_html_only_otp(self) -> None:
        fake = FakeImap({8: _fixture("valid_html.eml")})

        self.assertEqual(self.open_and_wait(fake), "193604")

    def test_multipart_quoted_printable_and_base64_same_code(self) -> None:
        fake = FakeImap({8: _multipart_encoded_message("731905")})

        self.assertEqual(self.open_and_wait(fake), "731905")

    def test_uidnext_boundary_excludes_stale_matching_mail(self) -> None:
        fake = FakeImap(
            {
                7: _fixture("valid_plain.eml"),
                8: _fixture("unrelated.eml"),
            },
            uid_next=8,
        )

        with self.assertRaises(OtpTimeoutError):
            self.open_and_wait(fake, timeout=0.5, poll_interval=0.25)

        self.assertNotIn(7, [uid for uid, _query in fake.fetches])

    def test_newest_matching_uid_is_selected(self) -> None:
        older = _matching_message("111111\n")
        newer = _matching_message("222222\n")
        fake = FakeImap({8: older, 9: newer})

        self.assertEqual(self.open_and_wait(fake), "222222")
        self.assertEqual([uid for uid, _query in fake.fetches], [9, 9])

    def test_polling_waits_for_a_new_message(self) -> None:
        fake = FakeImap(
            {8: _fixture("valid_plain.eml")},
            search_results=[[], [8]],
        )
        clock = FakeClock()

        self.assertEqual(
            self.open_and_wait(
                fake,
                timeout=1.0,
                poll_interval=0.25,
                clock=clock,
            ),
            "482731",
        )
        self.assertEqual(clock.sleeps, [0.25])
        self.assertEqual(fake.search_count, 2)

    def test_ambiguous_and_malformed_matching_messages_are_rejected(self) -> None:
        cases = {
            "multiple codes": _matching_message("111111\n222222\n"),
            "embedded code": _matching_message("Your code is 111111.\n"),
            "five digits": _matching_message("12345\n"),
            "seven digits": _matching_message("1234567\n"),
        }
        for name, message in cases.items():
            with self.subTest(name=name), self.assertRaises(OtpParseError):
                self.open_and_wait(FakeImap({8: message}))

    def test_timeout_uses_a_bounded_monotonic_deadline(self) -> None:
        fake = FakeImap(search_results=[[]])
        clock = FakeClock()

        with self.assertRaises(OtpTimeoutError) as raised:
            self.open_and_wait(
                fake,
                timeout=0.6,
                poll_interval=0.25,
                clock=clock,
            )

        self.assertEqual(clock.sleeps[:2], [0.25, 0.25])
        self.assertAlmostEqual(clock.sleeps[2], 0.1)
        self.assertAlmostEqual(clock.now, 100.6)
        self.assertEqual(fake.search_count, 3)
        self.assertNotIn(self.synergy_password, str(raised.exception))
        self.assertNotIn(self.gmail_password, str(raised.exception))

    def test_rejected_app_password_is_a_safe_mailbox_error(self) -> None:
        fake = FakeImap()
        fake.login_error = imaplib.IMAP4.error(
            f"synthetic rejection containing {self.gmail_password}"
        )

        with (
            patch("wa_synergy.otp.imaplib.IMAP4_SSL", return_value=fake),
            self.assertRaises(OtpMailboxError) as raised,
            gmail_otp_mailbox(self.credentials),
        ):
            self.fail("mailbox context unexpectedly opened")

        self.assertEqual(
            str(raised.exception),
            "The dedicated Gmail inbox could not be accessed",
        )
        self.assertNotIn(self.gmail_password, str(raised.exception))
        self.assertNotIn(self.synergy_password, str(raised.exception))
        self.assertTrue(fake.logged_out)

    def test_tls_and_inbox_selection_failures_are_safe(self) -> None:
        fake = FakeImap()
        fake.select_status = "NO"
        cases: list[object] = [fake, ssl.SSLError("synthetic TLS transcript")]
        for failure in cases:
            with self.subTest(failure=type(failure).__name__):
                if isinstance(failure, FakeImap):
                    patcher = patch(
                        "wa_synergy.otp.imaplib.IMAP4_SSL", return_value=failure
                    )
                else:
                    patcher = patch(
                        "wa_synergy.otp.imaplib.IMAP4_SSL", side_effect=failure
                    )
                with (
                    patcher,
                    self.assertRaises(OtpMailboxError) as raised,
                    gmail_otp_mailbox(self.credentials),
                ):
                    self.fail("mailbox context unexpectedly opened")
                self.assertNotIn("synthetic", str(raised.exception))

    def test_uidvalidity_change_fails_instead_of_using_a_new_mailbox(self) -> None:
        fake = FakeImap({8: _fixture("valid_plain.eml")})
        with (
            patch("wa_synergy.otp.imaplib.IMAP4_SSL", return_value=fake),
            gmail_otp_mailbox(self.credentials) as mailbox,
        ):
            fake.uid_validity = 43
            with self.assertRaisesRegex(OtpMailboxError, "identity changed"):
                wait_for_otp(mailbox, timeout=1.0)


if __name__ == "__main__":
    unittest.main()
