"""Outgoing mail for account links (D78).

There is no mail service in the free local stack, so the only real sender here is ``LogMailer``,
which writes the message to the application log. That is a DEVELOPMENT stand-in and the one place
a link token is allowed in a log, because the log is the inbox. A deployment with real users needs
a real sender (an implementation of ``Mailer`` for SMTP or a mail API) and must not run
``LogMailer``. ``OutboxMailer`` keeps messages in memory for tests.

A link carries its token in the URL fragment (after ``#``). A fragment is never sent to the server
or in a Referer header, and a mail scanner that fetches the link does not consume the token: the
page reads the fragment and posts it.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Protocol

log = logging.getLogger("vitalmesh.mail")


@dataclass(frozen=True)
class Message:
    to: str
    subject: str
    body: str


class Mailer(Protocol):
    def send(self, message: Message) -> None: ...


class LogMailer:
    """Development only: the log is the inbox."""

    def send(self, message: Message) -> None:
        log.warning(
            "DEV MAILER (no mail service) to=%s subject=%s\n%s",
            message.to,
            message.subject,
            message.body,
        )


class OutboxMailer:
    def __init__(self) -> None:
        self.sent: list[Message] = []

    def send(self, message: Message) -> None:
        self.sent.append(message)


def verification_message(to: str, base_url: str, token: str, valid_hours: int) -> Message:
    return Message(
        to,
        "Confirm your VitalMesh email",
        "Confirm that this is your email address:\n"
        f"{base_url}/verify#token={token}\n\n"
        f"The link works once and expires in {valid_hours} hours. "
        "If you did not create an account, ignore this message.",
    )


def reset_message(to: str, base_url: str, token: str, valid_minutes: int) -> Message:
    return Message(
        to,
        "Reset your VitalMesh password",
        "Choose a new password:\n"
        f"{base_url}/reset#token={token}\n\n"
        f"The link works once and expires in {valid_minutes} minutes. Using it signs you out "
        "everywhere. If you did not ask for this, ignore this message: your password is unchanged.",
    )
