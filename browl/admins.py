"""The admins added in the UI, and when each account last signed out, in the persistent database (DESIGN.md §2.11).

`browl.jobs.JobStore` creates and migrates the database at startup; this reads and writes its `admin` and `sign_out`
tables. Each call opens its own connection, so calls can run on any thread.
"""

import sqlite3
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path


@dataclass(frozen=True)
class Account:
    """What the persistent database says about a signed-in account.

    Attributes:
        listed: Whether its email is in the `admin` table.
        signed_out_at: When it last signed out; None if it never has.
    """

    listed: bool
    signed_out_at: datetime | None


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
                (email,),
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
                (email, at.isoformat()),
            )
