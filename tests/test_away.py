"""Tests for events / vacations (Away) and the warnings they cause."""

from datetime import date, time

import pytest

import agent
import database
from pawpal_system import Away, Household, Owner, Pet, Scheduler, Task
from tests.test_agent import FakeClient, plan_response, text_response, tool_response

DAY = date(2026, 11, 20)
TRIP = Away("Beach trip", date(2026, 11, 19), date(2026, 11, 22))
DENTIST = Away("Dentist", DAY, DAY, time(14, 0), time(15, 0))


@pytest.fixture
def home():
    jo = Owner("Jo", home_start=time(7), home_end=time(21))
    sam = Owner("Sam", home_start=time(7), home_end=time(21))
    biscuit = Pet("Biscuit")
    household = Household("Home")
    household.add_member(jo)
    household.add_member(sam)
    household.add_pet(biscuit)
    return household, jo, sam, biscuit


# --- Away ---------------------------------------------------------------------

def test_away_rejects_end_before_start():
    with pytest.raises(ValueError):
        Away("Trip", date(2026, 11, 22), date(2026, 11, 19))


def test_away_needs_both_times_or_neither():
    with pytest.raises(ValueError):
        Away("Meeting", DAY, DAY, start_time=time(9, 0))


def test_whole_day_absence_covers_every_day_in_range():
    assert TRIP.covers(date(2026, 11, 19)) and TRIP.covers(date(2026, 11, 22), time(8, 0), 30)
    assert not TRIP.covers(date(2026, 11, 23))


def test_partial_day_absence_only_blocks_overlapping_times():
    assert DENTIST.covers(DAY, time(14, 30), 15)
    assert DENTIST.covers(DAY, time(13, 45), 30)       # runs into the appointment
    assert not DENTIST.covers(DAY, time(15, 0), 30)    # starts right after
    assert not DENTIST.covers(DAY)                     # "any time" task can move


def test_owner_away_during_returns_the_absence(home):
    _, jo, _, _ = home
    jo.add_away(TRIP)
    assert jo.away_during(DAY) is TRIP
    jo.remove_away(TRIP)
    assert jo.away_during(DAY) is None


# --- Warnings -------------------------------------------------------------------

def test_warns_when_assigned_person_is_away(home):
    household, jo, _, biscuit = home
    jo.add_away(TRIP)
    household.add_task(Task("Walk", 30, 1, pets=[biscuit]), due_time=time(8), assigned_to=jo)

    warnings = Scheduler(household).away_warnings(DAY)

    assert len(warnings) == 1
    assert "Jo is away (Beach trip" in warnings[0] and "'Walk'" in warnings[0]


def test_no_warning_outside_the_absence(home):
    household, jo, _, biscuit = home
    jo.add_away(TRIP)
    household.add_task(Task("Walk", 30, 1, pets=[biscuit]), due_time=time(8), assigned_to=jo)
    assert Scheduler(household).away_warnings(date(2026, 11, 25)) == []


def test_anyone_task_warns_only_when_everyone_is_away(home):
    household, jo, sam, biscuit = home
    household.add_task(Task("Feed", 10, 1, pets=[biscuit]), due_time=time(8))
    jo.add_away(TRIP)
    assert Scheduler(household).away_warnings(DAY) == []

    sam.add_away(Away("Work trip", DAY, DAY))
    assert "Everyone is away" in Scheduler(household).away_warnings(DAY)[0]


def test_anyone_task_is_not_given_to_someone_away(home):
    household, jo, sam, biscuit = home
    jo.add_away(TRIP)
    task = Task("Feed", 10, 1, pets=[biscuit])
    household.add_task(task, due_time=time(8))

    Scheduler(household).create_daily_plan(DAY)

    assert task.assigned_to is sam


# --- Saving ---------------------------------------------------------------------

def test_away_dates_are_saved_and_loaded(home):
    household, jo, _, _ = home
    jo.add_away(TRIP)
    jo.add_away(DENTIST)

    copy = database.household_from_dict(database.household_to_dict(household))

    away = copy.members[0].away
    assert [a.describe() for a in away] == [TRIP.describe(), DENTIST.describe()]


def test_households_saved_before_away_dates_still_load(home):
    household, _, _, _ = home
    data = database.household_to_dict(household)
    for member in data["members"]:
        del member["away"]
    assert database.household_from_dict(data).members[0].away == []


# --- Agent ----------------------------------------------------------------------

def test_agent_reassigns_task_from_someone_away(home):
    household, jo, _, biscuit = home
    jo.add_away(TRIP)
    household.add_task(Task("Walk", 30, 1, pets=[biscuit]), due_time=time(8), assigned_to=jo)
    client = FakeClient(
        plan_response(True, ["Check the schedule"]),
        text_response("Nothing to add."),
        tool_response(("assign_task", {"task_id": "T1", "owner_name": "Sam"})),
        text_response("Gave the walk to Sam."),
    )

    result = agent.PawPalAgent(client=client).run("Check my week", household, 1, DAY)

    assert result.rounds == 1 and result.unresolved == []
    assert result.draft.tasks[0].assigned_to.name == "Sam"
    assert "away: Beach trip" in client.requests[0]["contents"][0]["parts"][0]["text"]
