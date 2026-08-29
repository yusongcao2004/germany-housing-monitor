"""Outbound-only notification transports for the housing monitor.

This module intentionally implements email sending only.  It never opens an
IMAP inbox, so a recipient cannot turn an email reply into an agent command.
SMTP credentials live in the macOS login keychain and are fetched only for the
duration of a send attempt.
"""

from __future__ import annotations

import smtplib
import ssl
import subprocess
from dataclasses import dataclass
from email.message import EmailMessage
from email.utils import formatdate
from typing import Any


class EmailDeliveryError(RuntimeError):
    """A sanitized delivery error safe to persist in the local outbox."""

    def __init__(
        self, category: str, *, retryable: bool, smtp_code: int | None = None
    ) -> None:
        super().__init__(category)
        self.category = category
        self.retryable = retryable
        self.smtp_code = smtp_code


@dataclass(frozen=True)
class EmailDeliveryResult:
    message_id: str


APPLE_MAIL_SEND_SCRIPT = r'''
on run argv
    if (count of argv) is not 4 then error "invalid arguments" number 17000
    set senderAddress to item 1 of argv
    set recipientAddress to item 2 of argv
    set messageSubject to item 3 of argv
    set messageBody to item 4 of argv

    tell application "Mail"
        set senderFound to false
        repeat with mailAccount in every account
            if senderAddress is in (email addresses of mailAccount) then
                set senderFound to true
                exit repeat
            end if
        end repeat
        if senderFound is false then error "configured sender is not in Mail" number 17001

        set outgoingMessage to make new outgoing message with properties ¬
            {subject:messageSubject, content:messageBody & return, visible:false, sender:senderAddress}
        tell outgoingMessage
            make new to recipient at end of to recipients with properties {address:recipientAddress}
            send
        end tell
    end tell
    return "sent"
end run
'''


def _safe_header(value: str, field: str) -> str:
    rendered = value.strip()
    if not rendered or "\n" in rendered or "\r" in rendered:
        raise EmailDeliveryError(f"invalid_{field}", retryable=False)
    return rendered


def read_keychain_password(service: str, account: str) -> str:
    """Read one app password without ever printing it or accepting it in argv."""

    try:
        completed = subprocess.run(
            [
                "/usr/bin/security",
                "find-generic-password",
                "-s",
                service,
                "-a",
                account,
                "-w",
            ],
            text=True,
            capture_output=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise EmailDeliveryError("keychain_unavailable", retryable=False) from exc
    password = completed.stdout.rstrip("\r\n")
    if completed.returncode != 0 or not password:
        raise EmailDeliveryError("smtp_app_password_missing", retryable=False)
    return password


def _classify_smtp_exception(exc: BaseException) -> EmailDeliveryError:
    if isinstance(exc, EmailDeliveryError):
        return exc
    if isinstance(exc, smtplib.SMTPAuthenticationError):
        return EmailDeliveryError(
            "smtp_authentication_failed",
            retryable=False,
            smtp_code=int(exc.smtp_code),
        )
    if isinstance(exc, smtplib.SMTPRecipientsRefused):
        return EmailDeliveryError("smtp_recipient_refused", retryable=False)
    if isinstance(exc, smtplib.SMTPSenderRefused):
        return EmailDeliveryError(
            "smtp_sender_refused",
            retryable=False,
            smtp_code=int(exc.smtp_code),
        )
    if isinstance(exc, smtplib.SMTPResponseException):
        code = int(exc.smtp_code)
        return EmailDeliveryError(
            "smtp_temporary_failure" if 400 <= code < 500 else "smtp_permanent_failure",
            retryable=400 <= code < 500,
            smtp_code=code,
        )
    if isinstance(exc, ssl.SSLCertVerificationError):
        return EmailDeliveryError("smtp_tls_verification_failed", retryable=False)
    if isinstance(
        exc,
        (
            TimeoutError,
            ConnectionError,
            OSError,
            smtplib.SMTPServerDisconnected,
            smtplib.SMTPConnectError,
        ),
    ):
        return EmailDeliveryError("smtp_connection_failed", retryable=True)
    return EmailDeliveryError("smtp_unknown_failure", retryable=True)


class EmailNotifier:
    """Send immutable plain-text batches through authenticated SMTP."""

    def __init__(self, config: dict[str, Any]) -> None:
        self.sender = _safe_header(str(config.get("sender", "")), "sender")
        self.recipient = _safe_header(
            str(config.get("recipient", "")), "recipient"
        )
        self.username = _safe_header(
            str(config.get("smtp_username") or self.sender), "smtp_username"
        )
        self.host = _safe_header(str(config.get("smtp_host", "")), "smtp_host")
        self.port = int(config.get("smtp_port", 465))
        self.security = str(config.get("smtp_security", "ssl")).strip().lower()
        if self.security not in {"ssl", "starttls"}:
            raise EmailDeliveryError("invalid_smtp_security", retryable=False)
        self.keychain_service = _safe_header(
            str(config.get("keychain_service", "")), "keychain_service"
        )
        self.timeout = int(config.get("timeout_seconds", 30))

    def send(self, *, subject: str, body: str, message_id: str) -> EmailDeliveryResult:
        subject = _safe_header(subject, "subject")
        message_id = _safe_header(message_id, "message_id")
        password = read_keychain_password(self.keychain_service, self.username)

        message = EmailMessage()
        message["From"] = self.sender
        message["To"] = self.recipient
        message["Subject"] = subject
        message["Date"] = formatdate(localtime=False)
        message["Message-ID"] = message_id
        message["Auto-Submitted"] = "auto-generated"
        message["X-Auto-Response-Suppress"] = "All"
        message.set_content(body, subtype="plain", charset="utf-8")

        context = ssl.create_default_context()
        try:
            if self.security == "ssl":
                with smtplib.SMTP_SSL(
                    self.host,
                    self.port,
                    timeout=self.timeout,
                    context=context,
                ) as client:
                    client.login(self.username, password)
                    refused = client.send_message(
                        message,
                        from_addr=self.sender,
                        to_addrs=[self.recipient],
                    )
            else:
                with smtplib.SMTP(
                    self.host, self.port, timeout=self.timeout
                ) as client:
                    client.ehlo()
                    client.starttls(context=context)
                    client.ehlo()
                    client.login(self.username, password)
                    refused = client.send_message(
                        message,
                        from_addr=self.sender,
                        to_addrs=[self.recipient],
                    )
            if refused:
                raise EmailDeliveryError("smtp_recipient_refused", retryable=False)
        except Exception as exc:
            raise _classify_smtp_exception(exc) from exc
        finally:
            password = ""
        return EmailDeliveryResult(message_id=message_id)


class AppleMailNotifier:
    """Use an existing Sign in with Google account in macOS Mail."""

    def __init__(self, config: dict[str, Any]) -> None:
        self.sender = _safe_header(str(config.get("sender", "")), "sender")
        self.recipient = _safe_header(
            str(config.get("recipient", "")), "recipient"
        )
        self.timeout = int(config.get("timeout_seconds", 30))

    def send(self, *, subject: str, body: str, message_id: str) -> EmailDeliveryResult:
        subject = _safe_header(subject, "subject")
        message_id = _safe_header(message_id, "message_id")
        try:
            completed = subprocess.run(
                [
                    "/usr/bin/osascript",
                    "-e",
                    APPLE_MAIL_SEND_SCRIPT,
                    "--",
                    self.sender,
                    self.recipient,
                    subject,
                    body,
                ],
                text=True,
                capture_output=True,
                timeout=self.timeout,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise EmailDeliveryError("apple_mail_timeout", retryable=True) from exc
        except OSError as exc:
            raise EmailDeliveryError("apple_mail_unavailable", retryable=True) from exc
        if completed.returncode != 0:
            error = completed.stderr.lower()
            if "-1743" in error or "not authorized" in error or "not authorised" in error:
                raise EmailDeliveryError(
                    "apple_mail_permission_denied", retryable=False
                )
            if "17001" in error:
                raise EmailDeliveryError("apple_mail_sender_missing", retryable=False)
            raise EmailDeliveryError("apple_mail_send_failed", retryable=True)
        if completed.stdout.strip() != "sent":
            raise EmailDeliveryError("apple_mail_unconfirmed", retryable=True)
        return EmailDeliveryResult(message_id=message_id)


def build_email_notifier(config: dict[str, Any]) -> EmailNotifier | AppleMailNotifier:
    transport = str(config.get("transport", "smtp")).strip().lower()
    if transport == "apple_mail":
        return AppleMailNotifier(config)
    if transport == "smtp":
        return EmailNotifier(config)
    raise EmailDeliveryError("invalid_email_transport", retryable=False)
