"""SQLite data store for PawPal 2.0 accounts and households.

Matches the "Data store (SQLite)" box in diagrams/account_creation.mmd:
    accounts    email · hashed password · owner id · failed login count
    households  code · members · pets · tasks · version

A household is saved as one JSON document (members, pets and tasks), plus a
version number. Every save bumps the version, so two members saving at once
can't silently overwrite each other (see save_household).
"""

from __future__ import annotations

import json
import os
import sqlite3
from datetime import date, time
from typing import Any, Optional

from pawpal_system import Away, Household, Owner, Pet, Task

DEFAULT_DB_PATH = os.environ.get("PAWPAL_DB", "pawpal.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS households (
    code     TEXT PRIMARY KEY,
    name     TEXT NOT NULL,
    version  INTEGER NOT NULL DEFAULT 1,
    data     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS accounts (
    owner_id       TEXT PRIMARY KEY,
    email          TEXT NOT NULL UNIQUE,
    password_hash  TEXT NOT NULL,
    salt           TEXT NOT NULL,
    first_name     TEXT NOT NULL,
    last_name      TEXT NOT NULL,
    age            INTEGER NOT NULL,
    household_code TEXT NOT NULL REFERENCES households(code),
    failed_logins  INTEGER NOT NULL DEFAULT 0,
    locked_until   TEXT
);
"""


class VersionConflict(Exception):
    """Raised when a household changed in the store after it was loaded."""


def connect(path: str = DEFAULT_DB_PATH) -> sqlite3.Connection:
    """Open the database (":memory:" for a throwaway one) and create tables if needed."""
    conn = sqlite3.connect(path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(SCHEMA)
    return conn


# --- Converting a Household to and from JSON ------------------------------

def _time_str(t: Optional[time]) -> Optional[str]:
    return t.strftime("%H:%M") if t else None


def _parse_time(text: Optional[str]) -> Optional[time]:
    return time.fromisoformat(text) if text else None


def _date_str(d: Optional[date]) -> Optional[str]:
    return d.isoformat() if d else None


def _parse_date(text: Optional[str]) -> Optional[date]:
    return date.fromisoformat(text) if text else None


def household_to_dict(household: Household) -> dict[str, Any]:
    """Turn a Household into plain data. People and pets are referred to by list position."""
    member_index = {id(owner): i for i, owner in enumerate(household.members)}
    pet_index = {id(pet): i for i, pet in enumerate(household.pets)}

    def owner_ref(owner: Optional[Owner]) -> Optional[int]:
        return member_index.get(id(owner)) if owner is not None else None

    return {
        "name": household.name,
        "members": [
            {
                "owner_id": owner.owner_id,
                "name": owner.name,
                "home_start": _time_str(owner.home_start),
                "home_end": _time_str(owner.home_end),
                "available_minutes": owner.available_minutes,
                "away": [
                    {
                        "label": a.label,
                        "start_date": _date_str(a.start_date),
                        "end_date": _date_str(a.end_date),
                        "start_time": _time_str(a.start_time),
                        "end_time": _time_str(a.end_time),
                    }
                    for a in owner.away
                ],
            }
            for owner in household.members
        ],
        "pets": [
            {
                "name": pet.name,
                "breed": pet.breed,
                "gender": pet.gender,
                "age": pet.age,
                "birthday": _date_str(pet.birthday),
                "special_needs": list(pet.special_needs),
                "priority": pet.priority,
            }
            for pet in household.pets
        ],
        "tasks": [
            {
                "name": task.name,
                "duration_minutes": task.duration_minutes,
                "priority": task.priority,
                "pets": [pet_index[id(pet)] for pet in task.pets],
                "due_time": _time_str(task.due_time),
                "assigned_to": owner_ref(task.assigned_to),
                "last_completed": _date_str(task.last_completed),
                "completed_by": owner_ref(task.completed_by),
                "frequency": task.frequency,
                "due_date": _date_str(task.due_date),
            }
            for task in household.tasks
        ],
    }


def household_from_dict(data: dict[str, Any]) -> Household:
    """Rebuild a Household (with all its links) from household_to_dict output."""
    household = Household(data["name"])
    for m in data["members"]:
        household.add_member(
            Owner(
                m["name"],
                home_start=_parse_time(m["home_start"]),
                home_end=_parse_time(m["home_end"]),
                available_minutes=m["available_minutes"],
                owner_id=m["owner_id"],
                away=[  # .get: households saved before away dates existed have none
                    Away(a["label"], _parse_date(a["start_date"]), _parse_date(a["end_date"]),
                         _parse_time(a["start_time"]), _parse_time(a["end_time"]))
                    for a in m.get("away", [])
                ],
            )
        )
    for p in data["pets"]:
        household.add_pet(
            Pet(
                p["name"],
                breed=p["breed"],
                gender=p["gender"],
                age=p["age"],
                birthday=_parse_date(p["birthday"]),
                special_needs=list(p["special_needs"]),
                priority=p["priority"],
            )
        )
    for t in data["tasks"]:
        task = Task(
            t["name"],
            t["duration_minutes"],
            t["priority"],
            pets=[household.pets[i] for i in t["pets"]],
            due_time=_parse_time(t["due_time"]),
            frequency=t["frequency"],
            due_date=_parse_date(t["due_date"]),
        )
        task.last_completed = _parse_date(t["last_completed"])
        if t["completed_by"] is not None:
            task.completed_by = household.members[t["completed_by"]]
        assigned = household.members[t["assigned_to"]] if t["assigned_to"] is not None else None
        household.add_task(task, assigned_to=assigned)
    return household


# --- Households ------------------------------------------------------------

def household_code_exists(conn: sqlite3.Connection, code: str) -> bool:
    """Return True if a household already uses this code."""
    row = conn.execute("SELECT 1 FROM households WHERE code = ?", (code,)).fetchone()
    return row is not None


def insert_household(conn: sqlite3.Connection, code: str, household: Household) -> None:
    """Add a new household at version 1. The caller commits (see auth.save_account)."""
    conn.execute(
        "INSERT INTO households (code, name, version, data) VALUES (?, ?, 1, ?)",
        (code, household.name, json.dumps(household_to_dict(household))),
    )


def load_household(conn: sqlite3.Connection, code: str) -> Optional[tuple[Household, int]]:
    """Return (household, version) for a code, or None if no household has it."""
    row = conn.execute(
        "SELECT data, version FROM households WHERE code = ?", (code,)
    ).fetchone()
    if row is None:
        return None
    return household_from_dict(json.loads(row["data"])), row["version"]


def household_version(conn: sqlite3.Connection, code: str) -> Optional[int]:
    """Return the saved version number for a household (None if it doesn't exist)."""
    row = conn.execute("SELECT version FROM households WHERE code = ?", (code,)).fetchone()
    return row["version"] if row else None


def save_household(
    conn: sqlite3.Connection,
    code: str,
    household: Household,
    expected_version: int,
    commit: bool = True,
) -> int:
    """Save a household only if nobody else saved since `expected_version`.

    Returns:
        The new version number (expected_version + 1).

    Raises:
        VersionConflict: Another member saved first; reload and try again.
    """
    cursor = conn.execute(
        "UPDATE households SET name = ?, data = ?, version = version + 1 "
        "WHERE code = ? AND version = ?",
        (household.name, json.dumps(household_to_dict(household)), code, expected_version),
    )
    if cursor.rowcount != 1:
        conn.rollback()
        raise VersionConflict(
            f"Household {code} was changed by another member since version {expected_version}."
        )
    if commit:
        conn.commit()
    return expected_version + 1


# --- Accounts --------------------------------------------------------------

def get_account(conn: sqlite3.Connection, email: str) -> Optional[sqlite3.Row]:
    """Return the account row for an email (case-insensitive), or None."""
    return conn.execute(
        "SELECT * FROM accounts WHERE email = ?", (email.strip().lower(),)
    ).fetchone()


def get_account_by_id(conn: sqlite3.Connection, owner_id: str) -> Optional[sqlite3.Row]:
    """Return the account row for an owner id, or None."""
    return conn.execute("SELECT * FROM accounts WHERE owner_id = ?", (owner_id,)).fetchone()


def insert_account(
    conn: sqlite3.Connection,
    owner_id: str,
    email: str,
    password_hash: str,
    salt: str,
    first_name: str,
    last_name: str,
    age: int,
    household_code: str,
) -> None:
    """Add an account row. The caller commits (see auth.save_account)."""
    conn.execute(
        "INSERT INTO accounts (owner_id, email, password_hash, salt, first_name, "
        "last_name, age, household_code) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (owner_id, email.strip().lower(), password_hash, salt, first_name,
         last_name, age, household_code),
    )


def set_login_failures(
    conn: sqlite3.Connection, owner_id: str, failures: int, locked_until: Optional[str]
) -> None:
    """Record the failed-login count and lock time for an account."""
    conn.execute(
        "UPDATE accounts SET failed_logins = ?, locked_until = ? WHERE owner_id = ?",
        (failures, locked_until, owner_id),
    )
    conn.commit()
