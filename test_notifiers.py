from __future__ import annotations

import smtplib
import unittest
from types import SimpleNamespace
from unittest import mock

import notifiers


class FakeSMTP:
    instance = None

    def __init__(self, host, port, *, timeout, context):
        self.host = host
        self.port = port
        self.timeout = timeout
        self.context = context
        self.login_args = None
        self.message = None
        FakeSMTP.instance = self

    def __enter__(self):
        return self

    def __exit__(self, _type, _value, _traceback):
        return False

    def login(self, username, password):
        self.login_args = (username, password)

    def send_message(self, message, *, from_addr, to_addrs):
        self.message = message
        self.from_addr = from_addr
        self.to_addrs = to_addrs
        return {}


class EmailNotifierTests(unittest.TestCase):
    @staticmethod
    def config():
        return {
            "sender": "sender@example.invalid",
            "recipient": "roommate@example.invalid",
            "smtp_username": "sender@example.invalid",
            "smtp_host": "smtp.example.invalid",
            "smtp_port": 465,
            "smtp_security": "ssl",
            "keychain_service": "test.housing.smtp",
            "timeout_seconds": 30,
        }

    def test_smtp_ssl_uses_keychain_secret_and_plain_text(self) -> None:
        notifier = notifiers.EmailNotifier(self.config())
        with mock.patch.object(
            notifiers, "read_keychain_password", return_value="app-secret"
        ), mock.patch.object(notifiers.smtplib, "SMTP_SSL", FakeSMTP):
            result = notifier.send(
                subject="找房监控｜1条新房源",
                body="房源：https://example.invalid/1",
                message_id="<stable@example.invalid>",
            )
        instance = FakeSMTP.instance
        self.assertIsNotNone(instance)
        self.assertEqual(
            instance.login_args, ("sender@example.invalid", "app-secret")
        )
        self.assertEqual(instance.message["Message-ID"], "<stable@example.invalid>")
        self.assertEqual(instance.message["Auto-Submitted"], "auto-generated")
        self.assertNotIn("app-secret", instance.message.as_string())
        self.assertEqual(result.message_id, "<stable@example.invalid>")

    def test_header_injection_is_blocked(self) -> None:
        config = self.config()
        config["recipient"] = "victim@example.invalid\nBcc: attacker@example.invalid"
        with self.assertRaises(notifiers.EmailDeliveryError) as caught:
            notifiers.EmailNotifier(config)
        self.assertFalse(caught.exception.retryable)
        self.assertEqual(caught.exception.category, "invalid_recipient")

    def test_temporary_smtp_code_is_retryable(self) -> None:
        error = notifiers._classify_smtp_exception(
            smtplib.SMTPResponseException(451, b"temporary")
        )
        self.assertTrue(error.retryable)
        self.assertEqual(error.smtp_code, 451)

    def test_authentication_failure_is_blocked(self) -> None:
        error = notifiers._classify_smtp_exception(
            smtplib.SMTPAuthenticationError(535, b"bad credentials")
        )
        self.assertFalse(error.retryable)
        self.assertEqual(error.category, "smtp_authentication_failed")

    def test_apple_mail_uses_existing_oauth_account(self) -> None:
        config = {
            "transport": "apple_mail",
            "sender": "sender@example.invalid",
            "recipient": "roommate@example.invalid",
            "timeout_seconds": 30,
        }
        notifier = notifiers.build_email_notifier(config)
        completed = SimpleNamespace(returncode=0, stdout="sent\n", stderr="")
        with mock.patch.object(
            notifiers.subprocess, "run", return_value=completed
        ) as run:
            result = notifier.send(
                subject="测试",
                body="房源：https://example.invalid/1",
                message_id="<stable@example.invalid>",
            )
        argv = run.call_args.args[0]
        self.assertEqual(argv[-4], "sender@example.invalid")
        self.assertEqual(argv[-3], "roommate@example.invalid")
        self.assertEqual(result.message_id, "<stable@example.invalid>")

    def test_apple_mail_permission_failure_is_blocked(self) -> None:
        config = {
            "transport": "apple_mail",
            "sender": "sender@example.invalid",
            "recipient": "roommate@example.invalid",
        }
        notifier = notifiers.build_email_notifier(config)
        completed = SimpleNamespace(
            returncode=1,
            stdout="",
            stderr="Not authorized to send Apple events to Mail. (-1743)",
        )
        with mock.patch.object(notifiers.subprocess, "run", return_value=completed):
            with self.assertRaises(notifiers.EmailDeliveryError) as caught:
                notifier.send(
                    subject="测试",
                    body="body",
                    message_id="<stable@example.invalid>",
                )
        self.assertEqual(caught.exception.category, "apple_mail_permission_denied")
        self.assertFalse(caught.exception.retryable)


if __name__ == "__main__":
    unittest.main()
