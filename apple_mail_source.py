#!/usr/bin/env python3
"""Read only recent official housing notifications from macOS Mail."""

from __future__ import annotations

import base64
import subprocess
from dataclasses import dataclass

from mail_sources import MailMessage


class AppleMailReadError(RuntimeError):
    pass


APPLE_MAIL_READ_SCRIPT = r'''
on b64(theText)
    set safeText to theText as text
    set commandText to "/usr/bin/printf %s " & quoted form of safeText & " | /usr/bin/base64 | /usr/bin/tr -d '\\n'"
    return do shell script commandText
end b64

on isHousingSender(senderText, subjectText)
    ignoring case
        if senderText contains "immobilienscout24.de" then return true
        if senderText contains "wg-gesucht.de" then return true
        if senderText contains "immowelt.de" then return true
        if senderText contains "kleinanzeigen.de" then return true
    end ignoring
    return false
end isHousingSender

on run argv
    if (count of argv) is not 2 then error "invalid arguments" number 17100
    set lookbackDays to (item 1 of argv) as integer
    set maxMessages to (item 2 of argv) as integer
    if lookbackDays < 1 or lookbackDays > 90 then error "invalid lookback" number 17101
    if maxMessages < 1 or maxMessages > 2000 then error "invalid maximum" number 17102
    set cutoffDate to (current date) - (lookbackDays * days)
    set outputLines to {}
    tell application "Mail"
        set recentMessages to (messages of inbox whose date received is greater than my cutoffDate)
        repeat with mailMessage in recentMessages
            if (count of my outputLines) is greater than or equal to my maxMessages then exit repeat
            set senderText to sender of mailMessage as text
            set subjectText to subject of mailMessage as text
            if my isHousingSender(senderText, subjectText) then
                set messageIdText to message id of mailMessage as text
                set receivedText to date received of mailMessage as text
                set bodyText to content of mailMessage as text
                set headersText to all headers of mailMessage as text
                if (length of bodyText) > 50000 then set bodyText to text 1 thru 50000 of bodyText
                if (length of headersText) > 30000 then set headersText to text 1 thru 30000 of headersText
                set end of my outputLines to (my b64(messageIdText)) & tab & (my b64(senderText)) & tab & (my b64(subjectText)) & tab & (my b64(receivedText)) & tab & (my b64(bodyText)) & tab & (my b64(headersText))
            end if
        end repeat
    end tell
    set AppleScript's text item delimiters to linefeed
    set outputText to outputLines as text
    set AppleScript's text item delimiters to ""
    return outputText
end run
'''


@dataclass(frozen=True)
class AppleMailReadResult:
    messages: tuple[MailMessage, ...]
    raw_rows: int


def _decode(field: str) -> str:
    try:
        return base64.b64decode(field, validate=True).decode("utf-8", "replace")
    except (ValueError, UnicodeDecodeError) as exc:
        raise AppleMailReadError("Apple Mail returned malformed data") from exc


def parse_mail_rows(output: str) -> AppleMailReadResult:
    messages: list[MailMessage] = []
    raw_rows = 0
    for line in output.splitlines():
        if not line.strip():
            continue
        raw_rows += 1
        fields = line.split("\t")
        if len(fields) not in {5, 6}:
            raise AppleMailReadError("Apple Mail returned an invalid row")
        message_id, sender, subject, received_at, body = map(_decode, fields[:5])
        authentication_results = _decode(fields[5]) if len(fields) == 6 else ""
        messages.append(
            MailMessage(
                message_id=message_id,
                sender=sender,
                subject=subject,
                received_at=received_at,
                body=body,
                authentication_results=authentication_results,
            )
        )
    return AppleMailReadResult(tuple(messages), raw_rows)


def read_recent_housing_mail(
    *,
    lookback_days: int = 7,
    max_messages: int = 200,
    timeout_seconds: int = 60,
) -> AppleMailReadResult:
    if not 1 <= int(lookback_days) <= 90:
        raise AppleMailReadError("Apple Mail lookback must be between 1 and 90 days")
    if not 1 <= int(max_messages) <= 2000:
        raise AppleMailReadError("Apple Mail maximum must be between 1 and 2000 messages")
    try:
        completed = subprocess.run(
            [
                "/usr/bin/osascript",
                "-e",
                APPLE_MAIL_READ_SCRIPT,
                "--",
                str(lookback_days),
                str(max_messages),
            ],
            text=True,
            capture_output=True,
            timeout=timeout_seconds,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise AppleMailReadError("Apple Mail read timed out") from exc
    except OSError as exc:
        raise AppleMailReadError("Apple Mail is unavailable") from exc
    if completed.returncode != 0:
        error = completed.stderr.casefold()
        if "-1743" in error or "not authorized" in error:
            raise AppleMailReadError("Apple Mail automation permission denied")
        raise AppleMailReadError("Apple Mail read failed")
    return parse_mail_rows(completed.stdout)
