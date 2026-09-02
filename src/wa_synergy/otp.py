"""Fresh one-time-passcode retrieval from a dedicated personal Gmail inbox."""

from __future__ import annotations

import imaplib
import re
import ssl
import time
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from dataclasses import dataclass, field
from datetime import UTC, datetime
from email import policy
from email.message import EmailMessage
from email.parser import BytesParser
from email.utils import parseaddr
from html.parser import HTMLParser

from .config import SynergyCredentials
from .errors import OtpMailboxError, OtpParseError, OtpTimeoutError

_GMAIL_HOST = "imap.gmail.com"
_GMAIL_PORT = 993
_GMAIL_FOLDER = "INBOX"
_EXPECTED_SENDER_NAME = "Synergy"
_EXPECTED_SENDER_ADDRESS = "info@synergy.net.au"
_EXPECTED_SUBJECT = "Your Synergy online one-time passcode"
_OTP_LINE = re.compile(r"[0-9]{6}\Z")
_HEADER_QUERY = "(BODY.PEEK[HEADER.FIELDS (FROM SUBJECT DATE MESSAGE-ID)])"
_UIDVALIDITY_STATUS = re.compile(rb'^(?:"INBOX"|INBOX) \(UIDVALIDITY ([0-9]+)\)$')
_FULL_MESSAGE_QUERY = "(BODY.PEEK[])"


@dataclass(frozen=True, slots=True)
class GmailOtpMailbox:
    """One read-only Gmail session and its pre-authentication freshness boundary."""

    uid_validity: int
    uid_next: int
    authentication_started_at: datetime
    _connection: imaplib.IMAP4_SSL = field(repr=False)


class _HtmlTextExtractor(HTMLParser):
    _BLOCK_TAGS = frozenset(
        {
            "address",
            "article",
            "aside",
            "blockquote",
            "br",
            "div",
            "footer",
            "h1",
            "h2",
            "h3",
            "h4",
            "h5",
            "h6",
            "header",
            "li",
            "main",
            "nav",
            "p",
            "section",
            "table",
            "td",
            "th",
            "tr",
        }
    )

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._chunks: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        del attrs
        if tag in self._BLOCK_TAGS:
            self._chunks.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in self._BLOCK_TAGS:
            self._chunks.append("\n")

    def handle_data(self, data: str) -> None:
        self._chunks.append(data)

    def text(self) -> str:
        return "".join(self._chunks)


def _require_ok(status: str, data: object) -> object:
    if status != "OK":
        raise OtpMailboxError(
            f"Gmail IMAP command returned status {status!r}: {data!r}"
        )
    return data


def _response_integer(connection: imaplib.IMAP4_SSL, name: str) -> int:
    status, values = connection.response(name)
    if status != name or not values or len(values) != 1:
        raise OtpMailboxError(
            f"Gmail IMAP {name} response was malformed: "
            f"status={status!r}, values={values!r}"
        )
    value = values[0]
    if not isinstance(value, bytes) or not value.isdigit():
        raise OtpMailboxError(
            f"Gmail IMAP {name} was not a positive integer: {value!r}"
        )
    parsed = int(value)
    if parsed < 1:
        raise OtpMailboxError(f"Gmail IMAP {name} must be positive, got {parsed}")
    return parsed


@contextmanager
def gmail_otp_mailbox(
    credentials: SynergyCredentials,
) -> Iterator[GmailOtpMailbox]:
    """Open fixed, certificate-verified Gmail IMAP access and always log out.

    The caller should enter this context immediately before submitting the Synergy
    password. The yielded UID boundary then excludes every message already present.
    """

    if not isinstance(credentials, SynergyCredentials):
        raise OtpMailboxError("Gmail OTP access requires Synergy credentials")

    authentication_started_at = datetime.now(UTC)
    connection: imaplib.IMAP4_SSL | None = None
    try:
        tls_context = ssl.create_default_context()
        connection = imaplib.IMAP4_SSL(
            _GMAIL_HOST,
            _GMAIL_PORT,
            ssl_context=tls_context,
        )
        _require_ok(
            *connection.login(credentials.email, credentials.gmail_app_password)
        )
        _require_ok(*connection.select(_GMAIL_FOLDER, readonly=True))
        uid_validity = _response_integer(connection, "UIDVALIDITY")
        uid_next = _response_integer(connection, "UIDNEXT")
        mailbox = GmailOtpMailbox(
            uid_validity=uid_validity,
            uid_next=uid_next,
            authentication_started_at=authentication_started_at,
            _connection=connection,
        )
        yield mailbox
    finally:
        if connection is not None:
            with suppress(imaplib.IMAP4.error, OSError, ssl.SSLError):
                connection.logout()


def _uid_call(
    connection: imaplib.IMAP4_SSL,
    command: str,
    *arguments: str,
) -> object:
    status, data = connection.uid(command, *arguments)
    return _require_ok(status, data)


def _current_uid_validity(connection: imaplib.IMAP4_SSL) -> int:
    status, data = connection.status(_GMAIL_FOLDER, "(UIDVALIDITY)")
    if status != "OK" or not isinstance(data, list) or len(data) != 1:
        raise OtpMailboxError(
            f"Gmail IMAP UIDVALIDITY status response was malformed: "
            f"status={status!r}, data={data!r}"
        )
    value = data[0]
    if not isinstance(value, bytes):
        raise OtpMailboxError(
            f"Gmail IMAP UIDVALIDITY response was not bytes: {value!r}"
        )
    match = _UIDVALIDITY_STATUS.fullmatch(value)
    if match is None:
        raise OtpMailboxError(
            f"Gmail IMAP UIDVALIDITY response had an unexpected format: {value!r}"
        )
    parsed = int(match.group(1))
    if parsed < 1:
        raise OtpMailboxError(f"Gmail IMAP UIDVALIDITY must be positive, got {parsed}")
    return parsed


def _search_fresh_uids(mailbox: GmailOtpMailbox) -> list[int]:
    current_uid_validity = _current_uid_validity(mailbox._connection)
    if current_uid_validity != mailbox.uid_validity:
        raise OtpMailboxError("The Gmail inbox identity changed during OTP retrieval")

    data = _uid_call(
        mailbox._connection,
        "search",
        None,  # type: ignore[arg-type]
        f"UID {mailbox.uid_next}:*",
    )
    if not isinstance(data, list) or len(data) != 1 or not isinstance(data[0], bytes):
        raise OtpMailboxError(f"Gmail IMAP UID search response was malformed: {data!r}")

    uids: list[int] = []
    for raw_uid in data[0].split():
        if not raw_uid.isdigit():
            raise OtpMailboxError(
                f"Gmail IMAP UID search returned a non-numeric UID: {raw_uid!r}"
            )
        uid = int(raw_uid)
        if uid >= mailbox.uid_next:
            uids.append(uid)
    return sorted(set(uids), reverse=True)


def _fetched_bytes(data: object) -> bytes:
    if not isinstance(data, list):
        raise OtpMailboxError(
            f"Gmail IMAP fetch response was not a list: {type(data).__name__}"
        )
    payloads = [
        item[1]
        for item in data
        if isinstance(item, tuple) and len(item) == 2 and isinstance(item[1], bytes)
    ]
    if len(payloads) != 1:
        raise OtpMailboxError(
            f"Gmail IMAP fetch returned {len(payloads)} message payloads; expected one"
        )
    return payloads[0]


def _fetch_message_bytes(connection: imaplib.IMAP4_SSL, uid: int, query: str) -> bytes:
    return _fetched_bytes(_uid_call(connection, "fetch", str(uid), query))


def _parse_email(document: bytes, *, candidate: bool) -> EmailMessage:
    try:
        message = BytesParser(policy=policy.default).parsebytes(document)
    except (UnicodeError, ValueError, TypeError) as exc:
        source = "Synergy OTP candidate" if candidate else "Gmail message"
        raise OtpParseError(f"{source} could not be parsed: {exc}") from exc
    if candidate and message.defects:
        details = "; ".join(str(defect) for defect in message.defects)
        raise OtpParseError(f"Synergy OTP message defects: {details}")
    return message


def _is_expected_message(message: EmailMessage) -> bool:
    sender_name, sender_address = parseaddr(str(message.get("From", "")))
    subject = str(message.get("Subject", ""))
    return (
        sender_name == _EXPECTED_SENDER_NAME
        and sender_address.lower() == _EXPECTED_SENDER_ADDRESS
        and subject == _EXPECTED_SUBJECT
    )


def _html_text(document: str) -> str:
    parser = _HtmlTextExtractor()
    try:
        parser.feed(document)
        parser.close()
    except (ValueError, AssertionError) as exc:
        raise OtpParseError(f"Synergy OTP HTML could not be parsed: {exc}") from exc
    return parser.text()


def _codes_in_text(document: str) -> set[str]:
    return {
        line.strip()
        for line in document.splitlines()
        if _OTP_LINE.fullmatch(line.strip()) is not None
    }


def _extract_otp(message: EmailMessage) -> str:
    codes: set[str] = set()
    parts = message.walk() if message.is_multipart() else (message,)
    try:
        for part in parts:
            if part.is_multipart() or part.get_content_disposition() == "attachment":
                continue
            content_type = part.get_content_type()
            if content_type not in {"text/plain", "text/html"}:
                continue
            content = part.get_content()
            if not isinstance(content, str):
                raise OtpParseError(
                    f"Synergy OTP {content_type} part returned "
                    f"{type(content).__name__}, expected text"
                )
            if content_type == "text/html":
                content = _html_text(content)
            codes.update(_codes_in_text(content))
    except (LookupError, UnicodeError, ValueError, TypeError) as exc:
        raise OtpParseError(f"Synergy OTP message decoding failed: {exc}") from exc

    if len(codes) != 1:
        raise OtpParseError(
            "A Synergy OTP message did not contain one unambiguous six-digit code"
        )
    return codes.pop()


def wait_for_otp(
    mailbox: GmailOtpMailbox,
    *,
    timeout: float = 120.0,
    poll_interval: float = 2.0,
) -> str:
    """Poll for and return the newest fresh, exactly allowlisted Synergy OTP."""

    if not isinstance(mailbox, GmailOtpMailbox):
        raise OtpMailboxError("Gmail OTP polling requires an open mailbox")
    if timeout <= 0 or poll_interval <= 0:
        raise ValueError("OTP timeout and poll interval must be positive")

    deadline = time.monotonic() + timeout
    inspected_uids: set[int] = set()
    first_poll = True
    while first_poll or time.monotonic() < deadline:
        first_poll = False
        for uid in _search_fresh_uids(mailbox):
            if uid in inspected_uids:
                continue
            inspected_uids.add(uid)
            header = _parse_email(
                _fetch_message_bytes(mailbox._connection, uid, _HEADER_QUERY),
                candidate=False,
            )
            if not _is_expected_message(header):
                continue
            full_message = _parse_email(
                _fetch_message_bytes(mailbox._connection, uid, _FULL_MESSAGE_QUERY),
                candidate=True,
            )
            if not _is_expected_message(full_message):
                raise OtpParseError("A Synergy OTP message changed while being fetched")
            code = _extract_otp(full_message)
            if time.monotonic() <= deadline:
                return code
            break

        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        time.sleep(min(poll_interval, remaining))

    raise OtpTimeoutError(
        "No unambiguous fresh Synergy OTP arrived before the deadline"
    )
