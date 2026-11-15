"""Login and account creation for PawPal 2.0.

Follows the "Login & account creation" box in diagrams/account_creation.mmd:
    login()                   Log in -> Credentials valid? (lock after 5 failures)
    find_household()          Join household -> Household code found? -> already has pets?
    generate_household_code() Create new household -> unique code
    validate_signup()         All fields valid?
    save_account()            Save account (one transaction)
    start_session()           Start session: owner + household
"""

from __future__ import annotations

import hashlib
import hmac
import re
import secrets
import sqlite3
import string
import uuid
from dataclasses import dataclass, field
from datetime import datetime, time, timedelta
from typing import Optional

import database
from pawpal_system import Household, Owner, Pet, Task

MAX_FAILED_LOGINS = 5
LOCKOUT_MINUTES = 15
MIN_PASSWORD_LENGTH = 8
CODE_LENGTH = 6
_PBKDF2_ROUNDS = 200_000
_EMAIL_PATTERN = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


# --- Passwords ---------------------------------------------------------------

def hash_password(password: str, salt: Optional[str] = None) -> tuple[str, str]:
    """Return (hash, salt) as hex strings. A new random salt is made if none is given."""
    salt = salt or secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256", password.encode(), bytes.fromhex(salt), _PBKDF2_ROUNDS
    )
    return digest.hex(), salt


def _password_matches(password: str, stored_hash: str, salt: str) -> bool:
    candidate, _ = hash_password(password, salt)
    return hmac.compare_digest(candidate, stored_hash)


# --- Login ---------------------------------------------------------------------

@dataclass
class LoginResult:
    """What happened when someone tried to log in."""

    ok: bool
    message: str
    owner_id: Optional[str] = None


def login(
    conn: sqlite3.Connection, email: str, password: str, now: Optional[datetime] = None
) -> LoginResult:
    """Check an email and password ("Credentials valid?").

    How it works:
        1. Look up the account and refuse if it is locked.
        2. Compare the password hash.
        3. Wrong password: add a failure; on the 5th, lock for 15 minutes.
        4. Right password: reset the failure count.
    """
    now = now or datetime.now()
    account = database.get_account(conn, email)
    if account is None:
        return LoginResult(False, "Email or password is incorrect.")

    if account["locked_until"] and now < datetime.fromisoformat(account["locked_until"]):
        return LoginResult(False, "Too many failed attempts. Try again in a few minutes.")

    if not _password_matches(password, account["password_hash"], account["salt"]):
        failures = account["failed_logins"] + 1
        locked_until = None
        if failures >= MAX_FAILED_LOGINS:
            locked_until = (now + timedelta(minutes=LOCKOUT_MINUTES)).isoformat()
            failures = 0  # start counting again once the lock ends
        database.set_login_failures(conn, account["owner_id"], failures, locked_until)
        if locked_until:
            return LoginResult(False, f"Too many failed attempts. Locked for {LOCKOUT_MINUTES} minutes.")
        return LoginResult(False, "Email or password is incorrect.")

    database.set_login_failures(conn, account["owner_id"], 0, None)
    return LoginResult(True, "Welcome back!", owner_id=account["owner_id"])


# --- Sign-up -----------------------------------------------------------------

@dataclass
class CareTaskInput:
    """One care task entered on the sign-up form for a pet."""

    name: str
    duration_minutes: int
    priority: int = 2
    due_time: Optional[time] = None
    frequency: Optional[str] = "daily"


@dataclass
class PetInput:
    """One pet entered on the sign-up form."""

    name: str
    breed: str = ""
    age: int | str = 0
    special_needs: list[str] = field(default_factory=list)
    care_tasks: list[CareTaskInput] = field(default_factory=list)


@dataclass
class SignupForm:
    """Everything on the "Create account" form.

    Set household_code to join a household, or new_household_name to create one.
    """

    first_name: str
    last_name: str
    age: int | str
    email: str
    password: str
    household_code: Optional[str] = None
    new_household_name: Optional[str] = None
    pets: list[PetInput] = field(default_factory=list)


def email_taken(conn: sqlite3.Connection, email: str) -> bool:
    """Return True if an account already uses this email."""
    return database.get_account(conn, email) is not None


def find_household(conn: sqlite3.Connection, code: str) -> Optional[tuple[Household, int]]:
    """Look up a household by its code ("Household code found?")."""
    return database.load_household(conn, code.strip().upper())


def generate_household_code(conn: sqlite3.Connection) -> str:
    """Make a random 6-character code, regenerating on a collision."""
    alphabet = string.ascii_uppercase + string.digits
    while True:
        code = "".join(secrets.choice(alphabet) for _ in range(CODE_LENGTH))
        if not database.household_code_exists(conn, code):
            return code


def _parse_age(value: int | str) -> Optional[int]:
    try:
        age = int(str(value).strip())
    except ValueError:
        return None
    return age if age >= 0 else None


def validate_signup(conn: sqlite3.Connection, form: SignupForm) -> dict[str, list[str]]:
    """Check the whole form ("All fields valid?").

    Returns:
        Error messages grouped by the step to send the user back to:
        "owner" (S1), "household" (S3a/S3b) and "pets" (S4). All lists empty = valid.
    """
    errors: dict[str, list[str]] = {"owner": [], "household": [], "pets": []}

    if not form.first_name.strip() or not form.last_name.strip():
        errors["owner"].append("First and last name are required.")
    owner_age = _parse_age(form.age)
    if owner_age is None:
        errors["owner"].append("Age must be a whole number.")
    if not _EMAIL_PATTERN.match(form.email.strip()):
        errors["owner"].append("Enter a valid email address.")
    elif email_taken(conn, form.email):
        errors["owner"].append("An account with this email already exists.")
    if len(form.password) < MIN_PASSWORD_LENGTH:
        errors["owner"].append(f"Password must be at least {MIN_PASSWORD_LENGTH} characters.")

    existing_pets = 0
    if form.household_code:
        found = find_household(conn, form.household_code)
        if found is None:
            errors["household"].append("No household has that code.")
        else:
            existing_pets = len(found[0].pets)
    elif not (form.new_household_name or "").strip():
        errors["household"].append("Enter a household code to join, or a name for a new household.")

    for i, pet in enumerate(form.pets, start=1):
        label = pet.name.strip() or f"Pet {i}"
        if not pet.name.strip():
            errors["pets"].append(f"Pet {i} needs a name.")
        if _parse_age(pet.age) is None:
            errors["pets"].append(f"{label}'s age must be a whole number.")
        for care in pet.care_tasks:
            if not care.name.strip():
                errors["pets"].append(f"{label} has a care task with no name.")
            if care.duration_minutes <= 0:
                errors["pets"].append(f"{label}'s '{care.name}' must take at least 1 minute.")
    if existing_pets + len(form.pets) == 0:
        errors["pets"].append("Add at least one pet (or join a household that has pets).")

    return errors


def save_account(conn: sqlite3.Connection, form: SignupForm) -> tuple[str, str]:
    """Create the Owner and Pet objects and save everything in one transaction.

    Call validate_signup first. If any write fails, nothing is saved.

    Returns:
        (owner_id, household_code)
    """
    owner_id = uuid.uuid4().hex
    owner = Owner(f"{form.first_name.strip()} {form.last_name.strip()}", owner_id=owner_id)
    password_hash, salt = hash_password(form.password)

    try:
        if form.household_code:
            code = form.household_code.strip().upper()
            household, version = database.load_household(conn, code)
        else:
            code = generate_household_code(conn)
            household, version = Household(form.new_household_name.strip()), None

        household.add_member(owner)
        for pet_input in form.pets:
            pet = Pet(
                pet_input.name.strip(),
                breed=pet_input.breed.strip(),
                age=_parse_age(pet_input.age) or 0,
                special_needs=[need.strip() for need in pet_input.special_needs if need.strip()],
            )
            household.add_pet(pet)
            for care in pet_input.care_tasks:
                household.add_task(
                    Task(care.name.strip(), care.duration_minutes, care.priority,
                         pets=[pet], frequency=care.frequency),
                    due_time=care.due_time,
                )

        if version is None:
            database.insert_household(conn, code, household)
        else:
            database.save_household(conn, code, household, version, commit=False)
        database.insert_account(
            conn, owner_id, form.email, password_hash, salt,
            form.first_name.strip(), form.last_name.strip(), int(form.age), code,
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return owner_id, code


# --- Session -------------------------------------------------------------------

@dataclass
class Session:
    """Who is logged in, and the household they share ("Start session")."""

    owner: Owner
    household: Household
    household_code: str
    version: int


def start_session(conn: sqlite3.Connection, owner_id: str) -> Session:
    """Load the logged-in person's household and find their Owner object in it."""
    account = database.get_account_by_id(conn, owner_id)
    if account is None:
        raise ValueError("No account with that id.")
    household, version = database.load_household(conn, account["household_code"])
    owner = next(m for m in household.members if m.owner_id == owner_id)
    return Session(owner, household, account["household_code"], version)


def reload_session(conn: sqlite3.Connection, session: Session) -> Session:
    """Throw away unsaved changes and load the latest saved household."""
    return start_session(conn, session.owner.owner_id)


def save_session(conn: sqlite3.Connection, session: Session) -> None:
    """Save manual edits to the session's household (raises database.VersionConflict)."""
    session.version = database.save_household(
        conn, session.household_code, session.household, session.version
    )
