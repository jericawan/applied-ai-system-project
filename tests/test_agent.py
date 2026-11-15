"""Tests for the AI agent (agent.py) using a scripted fake LLM client."""

import json
from datetime import date, time
from types import SimpleNamespace

import pytest

import agent
import database
from pawpal_system import Household, Owner, Pet, Task

DAY = date(2026, 11, 16)


# --- Fake Gemini client --------------------------------------------------------

def gemini_response(text=None, calls=None, finish_reason="STOP"):
    """Shaped like google.genai's GenerateContentResponse."""
    parts = [{"text": text}] if text else [{"function_call": vars(c)} for c in calls or []]
    candidate = SimpleNamespace(content={"role": "model", "parts": parts},
                                finish_reason=finish_reason)
    return SimpleNamespace(candidates=[candidate], text=text, function_calls=calls)


def text_response(text):
    return gemini_response(text=text)


def plan_response(changes, steps=(), reply="OK"):
    return text_response(json.dumps(
        {"changes_household": changes, "steps": list(steps), "reply": reply}))


def tool_response(*calls):
    return gemini_response(calls=[SimpleNamespace(id=f"call{i}", name=name, args=args)
                                  for i, (name, args) in enumerate(calls)])


class FakeClient:
    """Returns scripted responses in order and records every request."""

    def __init__(self, *responses):
        self.responses = list(responses)
        self.requests = []
        self.models = SimpleNamespace(generate_content=self._create)

    def _create(self, **kwargs):
        self.requests.append(kwargs)
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


@pytest.fixture
def home():
    jo = Owner("Jo", home_start=time(7), home_end=time(21), owner_id="jo")
    sam = Owner("Sam", home_start=time(9), home_end=time(17), owner_id="sam")
    biscuit, mochi = Pet("Biscuit", priority=1), Pet("Mochi")
    household = Household("Home")
    for member in (jo, sam):
        household.add_member(member)
    for pet in (biscuit, mochi):
        household.add_pet(pet)
    household.add_task(Task("Grooming", 20, 2, pets=[biscuit]), due_time=time(10), assigned_to=jo)
    household.add_task(Task("Vet call", 15, 1, pets=[biscuit]), due_time=time(10), assigned_to=sam)
    return household


def run(client, household, request="Please help", **kwargs):
    return agent.PawPalAgent(client=client, **kwargs).run(request, household, 1, DAY)


# --- Plan + route ---------------------------------------------------------------

def test_question_is_answered_without_changes(home):
    client = FakeClient(plan_response(False, reply="Grooming is at 10:00."))
    before = database.household_to_dict(home)

    result = run(client, home, "When is grooming?")

    assert result.answer_only and not result.needs_approval
    assert result.reply == "Grooming is at 10:00."
    assert "Daily plan" in result.explanation
    assert len(client.requests) == 1  # no tool loop for questions
    assert database.household_to_dict(home) == before


def test_planner_asks_for_json_output(home):
    client = FakeClient(plan_response(False))
    run(client, home)
    request = client.requests[0]
    assert request["model"] == agent.MODEL
    assert request["config"]["response_mime_type"] == "application/json"
    assert request["config"]["response_json_schema"] == agent.PLAN_SCHEMA


def test_tool_steps_turn_off_automatic_function_calling(home):
    client = FakeClient(plan_response(True, ["x"]), text_response("Done."))
    run(client, home, max_rounds=0)
    config = client.requests[1]["config"]
    assert config["automatic_function_calling"] == {"disable": True}
    assert config["tools"][0]["function_declarations"] == agent.ACT_TOOLS


def test_float_numbers_from_gemini_are_accepted(home):
    client = FakeClient(
        plan_response(True, ["Shorten vet call"]),
        tool_response(("edit_task", {"task_id": "T2", "duration_minutes": 10.0,
                                     "due_time": "12:00"})),
        text_response("Done."),
    )
    result = run(client, home)
    assert result.draft.tasks[1].duration_minutes == 10
    assert isinstance(result.draft.tasks[1].duration_minutes, int)


def test_unreadable_plan_aborts_the_run(home):
    result = run(FakeClient(text_response("not json")), home)
    assert result.error and not result.needs_approval


# --- Act --------------------------------------------------------------------------

def test_change_request_edits_draft_not_saved_household(home):
    client = FakeClient(
        plan_response(True, ["Add a walk"]),
        tool_response(("add_task", {"name": "Evening walk", "duration_minutes": 30, "priority": 2,
                                    "pet_names": ["Mochi"], "due_time": "18:00",
                                    "assigned_to": "Jo", "frequency": "daily"}),
                      ("create_daily_plan", {})),
        text_response("Added an evening walk."),
        tool_response(("edit_task", {"task_id": "T1", "name": None, "duration_minutes": None,
                                     "priority": None, "due_time": "11:00"})),
        text_response("Moved grooming."),
    )
    result = run(client, home, "Add an evening walk for Mochi")

    assert result.error is None and result.needs_approval
    assert [t.name for t in result.draft.tasks][-1] == "Evening walk"
    assert len(home.tasks) == 2  # the saved household is untouched
    assert result.conflicts == [] and result.rounds == 1


def test_plan_is_rebuilt_even_if_llm_forgets(home):
    client = FakeClient(
        plan_response(True, ["Shorten vet call"]),
        tool_response(("edit_task", {"task_id": "T2", "name": None, "duration_minutes": 10,
                                     "priority": None, "due_time": "12:00"})),
        text_response("Done."),
    )
    result = run(client, home)
    assert any("create_daily_plan run after the last change" in line for line in result.log)
    assert result.plan and result.fixable == []


def test_tool_error_is_sent_back_then_retried(home):
    client = FakeClient(
        plan_response(True, ["Assign vet call"]),
        tool_response(("assign_task", {"task_id": "T2", "owner_name": "Alex"})),
        tool_response(("edit_task", {"task_id": "T2", "name": None, "duration_minutes": None,
                                     "priority": None, "due_time": "12:00"})),
        text_response("Moved the vet call."),
    )
    result = run(client, home)

    # contents: [0] prompt, [1] model's function call, [2] function results sent back
    sent_back = client.requests[2]["contents"][2]
    assert sent_back["role"] == "user"
    function_response = sent_back["parts"][0]["function_response"]
    assert function_response["id"] == "call0"
    assert "not a household member" in function_response["response"]["error"]
    assert result.error is None


def test_too_many_tool_errors_abort_the_run(home):
    bad = ("assign_task", {"task_id": "T99", "owner_name": "Jo"})
    client = FakeClient(plan_response(True, ["x"]), tool_response(bad), tool_response(bad),
                        tool_response(bad))
    result = run(client, home)
    assert "kept failing" in result.error and not result.needs_approval


def test_api_error_aborts_the_run(home):
    result = run(FakeClient(ConnectionError("down")), home)
    assert result.error == "Couldn't reach the AI service."


def test_blocked_answer_aborts_the_run(home):
    blocked = gemini_response(text="", finish_reason="SAFETY")
    assert "declined" in run(FakeClient(blocked), home).error


def test_blocked_prompt_with_no_candidates_aborts_the_run(home):
    blocked = SimpleNamespace(candidates=[], text=None, function_calls=None)
    assert "declined" in run(FakeClient(blocked), home).error


def test_gemini_rate_limit_error_is_explained(home):
    from google.genai import errors
    rate_limited = errors.ClientError(429, {"error": {"message": "quota", "status": "RESOURCE_EXHAUSTED"}})
    assert "busy" in run(FakeClient(rate_limited), home).error


def test_missing_api_key_gives_clear_message(monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    with pytest.raises(RuntimeError, match="GEMINI_API_KEY"):
        agent.PawPalAgent()


# --- Check + fix loop -----------------------------------------------------------

def test_fixer_resolves_conflict(home):
    client = FakeClient(
        plan_response(True, ["Fix the clash"]),
        text_response("Nothing to add."),   # ACT makes no changes
        tool_response(("edit_task", {"task_id": "T1", "name": None, "duration_minutes": None,
                                     "priority": None, "due_time": "11:00"})),
        text_response("Moved grooming to 11:00."),
    )
    result = run(client, home)

    assert result.rounds == 1 and result.unresolved == []
    assert result.draft.tasks[0].due_time == time(11)
    assert result.needs_approval


def test_fix_loop_stops_when_no_progress(home):
    client = FakeClient(
        plan_response(True, ["Fix the clash"]),
        text_response("Nothing to add."),
        text_response("I couldn't change anything."),  # FIX round 1 makes no edits
    )
    result = run(client, home)

    assert result.rounds == 1
    assert result.unresolved and "double-booked" in result.unresolved[0]
    assert any("no progress" in line for line in result.log)


def test_fix_loop_stops_at_max_rounds(home):
    client = FakeClient(plan_response(True, ["Fix"]), text_response("Nothing."))
    result = run(client, home, max_rounds=0)
    assert result.rounds == 0 and result.unresolved


def test_nobody_home_is_unfixable(home):
    home.tasks.clear()
    for member in home.members:
        member.tasks.clear()
    for pet in home.pets:
        pet.tasks.clear()
    home.add_task(Task("Midnight meds", 5, 1, pets=[home.pets[0]]), due_time=time(23, 0))
    client = FakeClient(plan_response(True, ["Plan"]), text_response("Done."))

    result = run(client, home)

    assert result.fixable == [] and result.rounds == 0
    assert "nobody is home" in result.unfixable[0]


# --- Commit / rollback ----------------------------------------------------------

def test_commit_saves_draft_and_detects_conflicts(home):
    conn = database.connect(":memory:")
    database.insert_household(conn, "HOME01", home)
    conn.commit()
    saved, version = database.load_household(conn, "HOME01")
    client = FakeClient(
        plan_response(True, ["Move grooming"]),
        tool_response(("edit_task", {"task_id": "T1", "name": None, "duration_minutes": None,
                                     "priority": None, "due_time": "11:00"}),
                      ("create_daily_plan", {})),
        text_response("Moved."),
    )
    result = agent.PawPalAgent(client=client).run("Move grooming", saved, version, DAY)

    assert agent.commit(conn, "HOME01", result) == 2
    reloaded, _ = agent.rollback(conn, "HOME01")
    assert reloaded.tasks[0].due_time == time(11)

    with pytest.raises(database.VersionConflict):  # same draft, now out of date
        agent.commit(conn, "HOME01", result)
