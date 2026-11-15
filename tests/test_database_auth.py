"""Tests for the SQLite data store (database.py) and login / sign-up (auth.py)."""

from datetime import date, datetime, time, timedelta

import pytest

import auth
import database
from auth import CareTaskInput, PetInput, SignupForm
from pawpal_system import Household, Owner, Pet, Task

PASSWORD = "biscuit123"


@pytest.fixture
def conn():
    connection = database.connect(":memory:")
    yield connection
    connection.close()


def jo_form(**changes):
    form = SignupForm(
        "Jo", "Wan", "34", "jo@example.com", PASSWORD, new_household_name="Jo's Home",
        pets=[PetInput("Biscuit", "Golden Retriever", 4, ["joint medicine"],
                       [CareTaskInput("Walk", 30, 1, time(7, 30))])],
    )
    for key, value in changes.items():
        setattr(form, key, value)
    return form


def sign_up_jo(conn):
    return auth.save_account(conn, jo_form())


# --- database.py --------------------------------------------------------------

def test_household_round_trips_through_json():
    jo, sam = Owner("Jo", time(7), time(21), 90, owner_id="a"), Owner("Sam", owner_id="b")
    biscuit = Pet("Biscuit", breed="Golden", age=4, special_needs=["meds"], priority=1)
    household = Household("Home")
    household.add_member(jo)
    household.add_member(sam)
    household.add_pet(biscuit)
    walk = Task("Walk", 30, 1, pets=[biscuit], frequency="daily", due_date=date(2026, 11, 1))
    household.add_task(walk, due_time=time(8), assigned_to=sam)
    walk.mark_complete(jo, date(2026, 11, 1))

    copy = database.household_from_dict(database.household_to_dict(household))

    assert database.household_to_dict(copy) == database.household_to_dict(household)
    task = copy.tasks[0]
    assert task.assigned_to is copy.members[1] and task in copy.members[1].tasks
    assert task.completed_by is copy.members[0]
    assert task.pets == [copy.pets[0]] and task in copy.pets[0].tasks


def test_save_household_bumps_version(conn):
    database.insert_household(conn, "ABC123", Household("Home"))
    conn.commit()
    household, version = database.load_household(conn, "ABC123")
    household.name = "New name"

    assert database.save_household(conn, "ABC123", household, version) == 2
    assert database.load_household(conn, "ABC123")[0].name == "New name"


def test_second_save_from_same_version_is_refused(conn):
    database.insert_household(conn, "ABC123", Household("Home"))
    conn.commit()
    first, version = database.load_household(conn, "ABC123")
    second, _ = database.load_household(conn, "ABC123")
    database.save_household(conn, "ABC123", first, version)

    with pytest.raises(database.VersionConflict):
        database.save_household(conn, "ABC123", second, version)


def test_load_missing_household_returns_none(conn):
    assert database.load_household(conn, "NOPE00") is None


# --- Sign-up ------------------------------------------------------------------

def test_valid_signup_has_no_errors(conn):
    assert not any(auth.validate_signup(conn, jo_form()).values())


def test_signup_rejects_bad_owner_info(conn):
    errors = auth.validate_signup(conn, jo_form(age="thirty", email="not-an-email", password="short"))
    assert len(errors["owner"]) == 3


def test_signup_rejects_email_already_registered(conn):
    sign_up_jo(conn)
    errors = auth.validate_signup(conn, jo_form(email="JO@example.com"))
    assert any("already exists" in e for e in errors["owner"])


def test_signup_needs_a_pet_when_creating_a_household(conn):
    errors = auth.validate_signup(conn, jo_form(pets=[]))
    assert errors["pets"]


def test_signup_rejects_unknown_household_code(conn):
    errors = auth.validate_signup(conn, jo_form(household_code="ZZZZZZ", new_household_name=None))
    assert errors["household"] == ["No household has that code."]


def test_joining_a_household_with_pets_needs_no_new_pets(conn):
    _, code = sign_up_jo(conn)
    sam = SignupForm("Sam", "Wan", 31, "sam@example.com", PASSWORD, household_code=code.lower())
    assert not any(auth.validate_signup(conn, sam).values())

    auth.save_account(conn, sam)
    household, version = database.load_household(conn, code)
    assert [m.name for m in household.members] == ["Jo Wan", "Sam Wan"]
    assert version == 2


def test_save_account_creates_pets_and_care_tasks(conn):
    owner_id, code = sign_up_jo(conn)
    session = auth.start_session(conn, owner_id)

    assert session.owner.owner_id == owner_id
    assert session.household_code == code
    assert [p.name for p in session.household.pets] == ["Biscuit"]
    task = session.household.tasks[0]
    assert (task.name, task.due_time, task.frequency) == ("Walk", time(7, 30), "daily")


def test_save_account_is_all_or_nothing(conn):
    sign_up_jo(conn)
    # Same email gets past a skipped validation but fails on the UNIQUE constraint
    with pytest.raises(Exception):
        auth.save_account(conn, jo_form(new_household_name="Second Home"))
    assert conn.execute("SELECT COUNT(*) FROM households").fetchone()[0] == 1


def test_household_codes_are_unique(conn, monkeypatch):
    sign_up_jo(conn)
    taken = conn.execute("SELECT code FROM households").fetchone()[0]
    picks = iter(list(taken) + list("NEW123"))
    monkeypatch.setattr(auth.secrets, "choice", lambda _alphabet: next(picks))
    assert auth.generate_household_code(conn) == "NEW123"


# --- Login --------------------------------------------------------------------

def test_login_with_right_password(conn):
    owner_id, _ = sign_up_jo(conn)
    result = auth.login(conn, "jo@example.com", PASSWORD)
    assert result.ok and result.owner_id == owner_id


def test_login_with_wrong_password_or_email(conn):
    sign_up_jo(conn)
    assert not auth.login(conn, "jo@example.com", "wrong-password").ok
    assert not auth.login(conn, "nobody@example.com", PASSWORD).ok


def test_password_is_not_stored_in_plain_text(conn):
    sign_up_jo(conn)
    account = database.get_account(conn, "jo@example.com")
    assert PASSWORD not in account["password_hash"]


def test_account_locks_after_five_failures_then_unlocks(conn):
    sign_up_jo(conn)
    now = datetime(2026, 11, 15, 9, 0)
    for _ in range(auth.MAX_FAILED_LOGINS):
        auth.login(conn, "jo@example.com", "wrong-password", now=now)

    locked = auth.login(conn, "jo@example.com", PASSWORD, now=now)
    assert not locked.ok and "Too many" in locked.message

    later = now + timedelta(minutes=auth.LOCKOUT_MINUTES + 1)
    assert auth.login(conn, "jo@example.com", PASSWORD, now=later).ok


def test_successful_login_resets_failure_count(conn):
    sign_up_jo(conn)
    auth.login(conn, "jo@example.com", "wrong-password")
    auth.login(conn, "jo@example.com", PASSWORD)
    assert database.get_account(conn, "jo@example.com")["failed_logins"] == 0


# --- Session ------------------------------------------------------------------

def test_save_session_and_reload(conn):
    owner_id, _ = sign_up_jo(conn)
    session = auth.start_session(conn, owner_id)
    session.owner.update_home_hours(time(7), time(21))
    auth.save_session(conn, session)

    reloaded = auth.reload_session(conn, session)
    assert reloaded.owner.home_start == time(7)
    assert reloaded.version == session.version
