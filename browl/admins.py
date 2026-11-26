"""The admins added in the UI, and when each account last signed out, in the persistent database (DESIGN.md §2.11).

`browl.jobs.JobStore` creates and migrates the database at startup; this reads and writes its `admin` and `sign_out`
tables. Each call opens its own connection, so calls can run on any thread.

Email addresses are shown as given, lowercased, but compared in their canonical form (`canonical_email`), so an address
typed in any case, or a Gmail address with or without dots, matches the account Google signs in. The store's methods
take addresses in either form.
"""

import re
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from pydantic.dataclasses import dataclass as pydantic_dataclass

_GMAIL_DOMAIN = "gmail.com"
"""The domain whose addresses ignore dots in the local part: `j.doe@gmail.com` is `jdoe@gmail.com`."""

_MAX_EMAIL_CHARS = 254
"""The longest email address accepted, the most an SMTP path allows (RFC 5321 with erratum 1690)."""

_EMAIL = re.compile(r"[^@\s]+@[^@\s]+\.[^@\s.]+")
"""An email address, loosely: no spaces, one `@`, and a dot in the domain. Google checks the rest at sign-in."""


def canonical_email(address: str) -> str:
    """Return the form of an email address that browl compares, and keys the tables by.

    It's lowercased, and a Gmail address loses the dots in its local part, which Gmail ignores. Other Gmail
    equivalences, such as `+` tags and `googlemail.com`, aren't applied.

    Examples:
        >>> canonical_email("J.Doe@Gmail.com"), canonical_email("J.Doe@Example.org")
        ('jdoe@gmail.com', 'j.doe@example.org')
    """
    local, at, domain = address.lower().rpartition("@")
    if at and domain == _GMAIL_DOMAIN:
        local = local.replace(".", "")
    return f"{local}{at}{domain}"


def parse_email(text: str) -> str:
    """Return an email address typed in a form or the settings, trimmed and lowercased, to show.

    Its canonical form must be an address too, so e.g. `...@gmail.com` isn't. Control characters, which would reach
    the logs, are refused.

    Raises:
        ValueError: The text isn't an email address.
    """
    address = text.strip().lower()
    if len(address) > _MAX_EMAIL_CHARS or not address.isprintable() or not _EMAIL.fullmatch(canonical_email(address)):
        raise ValueError(text)
    return address


@dataclass(frozen=True)
class Account:
    """What the persistent database says about a signed-in account.

    Attributes:
        listed: Whether its email is in the `admin` table.
        signed_out_at: When it last signed out; None if it never has.
    """

    listed: bool
    signed_out_at: datetime | None


@pydantic_dataclass(frozen=True)
class Admin:
    """An admin added in the UI: a row of the `admin` table.

    Attributes:
        email: Their email address as it was added, lowercased.
        added_at: When they were added.
        added_by: The address of the admin who added them, as they signed in; None if no one was signed in, with
            `AUTH_DISABLED`.
    """

    email: str
    added_at: datetime
    added_by: str | None


class AdminStore:
    """The persistent database's `admin` and `sign_out` tables.

    Args:
        path: The database file. It must exist: a missing one is an error, not created.
    """

    def __init__(self, path: Path) -> None:
        """Use the database at a path; nothing is opened until a call."""
        self.path = path

    def _connect(self, mode: str) -> closing[sqlite3.Connection]:
        # Opened by URI with a mode, so a missing file is an error rather than a new, empty database.
        uri = f"{self.path.absolute().as_uri()}?mode={mode}"
        return closing(sqlite3.connect(uri, uri=True, isolation_level=None))

    def account(self, email: str) -> Account:
        """Return whether an email address is in the `admin` table, and when it last signed out, in one read.

        Raises:
            sqlite3.Error: The database couldn't be read.
        """
        with self._connect("ro") as connection:
            listed, signed_out_at = connection.execute(
                "SELECT EXISTS (SELECT 1 FROM admin WHERE email = ?1),"
                " (SELECT signed_out_at FROM sign_out WHERE email = ?1)",
                (canonical_email(email),),
            ).fetchone()
        return Account(bool(listed), None if signed_out_at is None else datetime.fromisoformat(signed_out_at))

    def sign_out(self, email: str, at: datetime) -> None:
        """Record that an email address signed out, ending its sessions that started at or before then.

        Raises:
            sqlite3.Error: The database couldn't be written.
        """
        with self._connect("rw") as connection:
            connection.execute(
                "INSERT INTO sign_out (email, signed_out_at) VALUES (?, ?)"
                " ON CONFLICT (email) DO UPDATE SET signed_out_at = excluded.signed_out_at",
                (canonical_email(email), at.isoformat()),
            )

    def admins(self) -> list[Admin]:
        """Return the admins in the `admin` table, by email address.

        Raises:
            sqlite3.Error: The database couldn't be read.
        """
        with self._connect("ro") as connection:
            rows = connection.execute(
                "SELECT display_email, added_at, added_by FROM admin ORDER BY display_email"
            ).fetchall()
        return [Admin(*row) for row in rows]

    def add(self, email: str, by: str | None, at: datetime) -> bool:
        """Add an email address to the `admin` table, returning whether it was added: False if it was there already.

        Args:
            email: The address, lowercased; it's shown as given.
            by: The address of the admin adding it; None if no one is signed in.
            at: When it's added.

        Raises:
            sqlite3.Error: The database couldn't be written.
        """
        with self._connect("rw") as connection:
            cursor = connection.execute(
                "INSERT INTO admin (email, display_email, added_at, added_by) VALUES (?, ?, ?, ?)"
                " ON CONFLICT (email) DO NOTHING",
                (canonical_email(email), email, at.isoformat(), by),
            )
        return cursor.rowcount == 1

    def remove(self, email: str) -> bool:
        """Remove an email address from the `admin` table, returning whether it was there.

        Raises:
            sqlite3.Error: The database couldn't be written.
        """
        with self._connect("rw") as connection:
            cursor = connection.execute("DELETE FROM admin WHERE email = ?", (canonical_email(email),))
        return cursor.rowcount == 1
