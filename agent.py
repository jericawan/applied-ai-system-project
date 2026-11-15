"""PawPal+ AI agent: plan -> act -> check -> fix, on a draft copy of the household.

Follows the "AI Agent" box in diagrams/agent_workflow.mmd:
    start_run()   round = 0, copy Household -> Draft, note version
    plan()        1 · PLAN   LLM reads request + draft state, drafts steps
                  Route: does the request change the household?
    answer()      Answer only: build plan, explain, no edits
    execute()     2 · ACT    LLM calls tools, always ending with create_daily_plan
    check()       3 · CHECK  conflicts + skipped tasks, split fixable / unfixable
    fix()         4 · FIX    LLM moves / reassigns / reprioritizes, then re-plan
    explain()     explain_plan for the display
Then the UI decides: commit() saves the draft (if the version still matches),
rollback() throws it away and reloads the saved household.

The LLM is Google Gemini (google-genai SDK). Set GEMINI_API_KEY (or
GOOGLE_API_KEY) to use it. Tests pass in a fake client, so they never call the
real API.
"""

from __future__ import annotations

import copy
import json
import logging
import os
import sqlite3
from dataclasses import dataclass, field
from datetime import date, time
from typing import Any, Optional

import database
from pawpal_system import Household, Owner, Pet, ScheduledTask, Scheduler, Task

try:  # only needed for the real API; tests use a fake client
    from google import genai
    from google.genai import errors as genai_errors
except ImportError:  # pragma: no cover
    genai = genai_errors = None

logger = logging.getLogger("pawpal.agent")

MODEL = os.environ.get("GEMINI_MODEL", "gemini-3.8-flash")
MAX_TOKENS = 8192
MAX_ROUNDS = 3         # fix rounds before giving up
MAX_TOOL_RETRIES = 2    # failed tool calls allowed per step; the next one aborts the run
MAX_TOOL_TURNS = 12     # LLM turns allowed per ACT or FIX step

# Skipped-task reasons the agent can't fix by moving or reassigning tasks
UNFIXABLE_REASONS = ("nobody is home",)


class AgentError(Exception):
    """The run was aborted; its draft must be discarded."""


@dataclass
class RunResult:
    """Everything one agent run produced, for the plan display."""

    request: str
    draft: Household
    base_version: int
    day: date
    answer_only: bool = False
    reply: str = ""
    steps: list[str] = field(default_factory=list)
    plan: list[ScheduledTask] = field(default_factory=list)
    skipped: list[tuple[Task, str]] = field(default_factory=list)
    conflicts: list[str] = field(default_factory=list)
    fixable: list[str] = field(default_factory=list)
    unfixable: list[str] = field(default_factory=list)
    unresolved: list[str] = field(default_factory=list)
    explanation: str = ""
    rounds: int = 0
    changed: bool = False
    log: list[str] = field(default_factory=list)
    error: Optional[str] = None

    @property
    def needs_approval(self) -> bool:
        """True when the owner should approve or reject the draft."""
        return not self.answer_only and self.error is None and self.changed

    def record(self, step: str, detail: str) -> None:
        """Add a line to the agent trace and the Python log."""
        line = f"[{step}] {detail}"
        self.log.append(line)
        logger.info(line)


# --- Tool layer (tool schemas -> Python functions) ---------------------------

def _optional(kind: str, description: str) -> dict[str, Any]:
    """An argument the LLM may leave out (it isn't listed in "required")."""
    return {"type": kind, "description": description}


def _tool(
    name: str, description: str, properties: dict[str, Any], optional: tuple[str, ...] = ()
) -> dict[str, Any]:
    """A Gemini function declaration; every argument not in `optional` is required."""
    return {
        "name": name,
        "description": description,
        "parameters_json_schema": {
            "type": "object",
            "properties": properties,
            "required": [key for key in properties if key not in optional],
        },
    }


TOOL_ADD_TASK = _tool(
    "add_task",
    "Add a new care task for one or more household pets.",
    {
        "name": {"type": "string", "description": "Short task name, e.g. 'Evening walk'."},
        "duration_minutes": {"type": "integer", "description": "How long it takes, at least 1."},
        "priority": {"type": "integer", "description": "1 = most important, 3 = least."},
        "pet_names": {"type": "array", "items": {"type": "string"},
                      "description": "Names of the pets this task is for."},
        "due_time": _optional("string", "Fixed start time as HH:MM (24h). Leave out for any time."),
        "assigned_to": _optional("string", "Household member's name. Leave out to auto-assign."),
        "frequency": {"type": "string", "enum": ["once", "daily", "weekly"]},
    },
    optional=("due_time", "assigned_to"),
)
TOOL_EDIT_TASK = _tool(
    "edit_task",
    "Change a task's details. Leave out anything that should stay the same.",
    {
        "task_id": {"type": "string", "description": "Task id from the household state, e.g. 'T3'."},
        "name": _optional("string", "New name."),
        "duration_minutes": _optional("integer", "New duration in minutes."),
        "priority": _optional("integer", "New priority, 1 = most important."),
        "due_time": _optional("string", "New fixed start time as HH:MM (24h)."),
    },
    optional=("name", "duration_minutes", "priority", "due_time"),
)
TOOL_ASSIGN_TASK = _tool(
    "assign_task",
    "Make a household member responsible for a task.",
    {
        "task_id": {"type": "string", "description": "Task id, e.g. 'T3'."},
        "owner_name": {"type": "string", "description": "Household member's name."},
    },
)
TOOL_UPDATE_HOME_HOURS = _tool(
    "update_home_hours",
    "Change when a household member is home. Only use when the owner asks for it.",
    {
        "owner_name": {"type": "string"},
        "start": {"type": "string", "description": "HH:MM (24h)."},
        "end": {"type": "string", "description": "HH:MM (24h), after start."},
    },
)
TOOL_UPDATE_AVAILABILITY = _tool(
    "update_availability",
    "Change how many minutes of pet care a member can give today "
    "(leave out minutes = their whole time at home).",
    {
        "owner_name": {"type": "string"},
        "minutes": _optional("integer", "Total care minutes."),
    },
    optional=("minutes",),
)
TOOL_CREATE_DAILY_PLAN = _tool(
    "create_daily_plan",
    "Rebuild today's timed schedule from the current tasks. Call this last.",
    {},
)

ACT_TOOLS = [TOOL_ADD_TASK, TOOL_EDIT_TASK, TOOL_ASSIGN_TASK, TOOL_UPDATE_HOME_HOURS,
             TOOL_UPDATE_AVAILABILITY, TOOL_CREATE_DAILY_PLAN]
FIX_TOOLS = [TOOL_EDIT_TASK, TOOL_ASSIGN_TASK]


def _int(value: Any) -> Optional[int]:
    """Gemini can send whole numbers as floats (30.0); turn them back into ints."""
    if value is None:
        return None
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, int):
        return value
    raise ValueError(f"'{value}' is not a whole number.")


def _parse_hhmm(text: str) -> time:
    try:
        return time.fromisoformat(text)
    except (TypeError, ValueError):
        raise ValueError(f"'{text}' is not a time in HH:MM format.") from None


class ToolBox:
    """Runs the agent's tool calls against the draft household."""

    def __init__(self, household: Household, day: date):
        self.household = household
        self.day = day
        self.scheduler = Scheduler(household)
        self.planned = False  # has create_daily_plan run since the last edit?

    def _task(self, task_id: str) -> Task:
        try:
            index = int(task_id.strip().upper().lstrip("T")) - 1
        except ValueError:
            raise ValueError(f"'{task_id}' is not a task id like 'T3'.") from None
        if not 0 <= index < len(self.household.tasks):
            raise ValueError(f"There is no task {task_id}.")
        return self.household.tasks[index]

    def _owner(self, name: str) -> Owner:
        wanted = name.strip().lower()
        for owner in self.household.members:
            if owner.name.lower() == wanted or owner.name.lower().split()[0] == wanted:
                return owner
        raise ValueError(f"{name} is not a household member.")

    def _pet(self, name: str) -> Pet:
        wanted = name.strip().lower()
        for pet in self.household.pets:
            if pet.name.lower() == wanted:
                return pet
        raise ValueError(f"{name} is not a household pet.")

    def run(self, name: str, args: dict[str, Any]) -> str:
        """Run one tool call and return a short result for the LLM.

        Raises:
            ValueError: The arguments were invalid (sent back to the LLM as an error).
        """
        if name == "add_task":
            pets = [self._pet(n) for n in args["pet_names"]]
            frequency = None if args["frequency"] == "once" else args["frequency"]
            task = Task(args["name"], _int(args["duration_minutes"]), _int(args["priority"]),
                        pets=pets, frequency=frequency)
            due = _parse_hhmm(args["due_time"]) if args.get("due_time") else None
            owner = self._owner(args["assigned_to"]) if args.get("assigned_to") else None
            self.household.add_task(task, due_time=due, assigned_to=owner)
            result = f"Added T{len(self.household.tasks)} '{task.name}'."
        elif name == "edit_task":
            task = self._task(args["task_id"])
            due = _parse_hhmm(args["due_time"]) if args.get("due_time") else None
            self.household.edit_task(
                task, name=args.get("name"), duration_minutes=_int(args.get("duration_minutes")),
                priority=_int(args.get("priority")), due_time=due,
            )
            result = f"Updated {args['task_id']} '{task.name}'."
        elif name == "assign_task":
            task = self._task(args["task_id"])
            owner = self._owner(args["owner_name"])
            self.household.assign_task(task, owner)
            result = f"{args['task_id']} '{task.name}' is now {owner.name}'s."
        elif name == "update_home_hours":
            owner = self._owner(args["owner_name"])
            owner.update_home_hours(_parse_hhmm(args["start"]), _parse_hhmm(args["end"]))
            result = f"{owner.name} is home {args['start']}-{args['end']}."
        elif name == "update_availability":
            owner = self._owner(args["owner_name"])
            owner.update_availability(_int(args.get("minutes")))
            result = f"{owner.name} has {args.get('minutes') or 'all their home'} minutes."
        elif name == "create_daily_plan":
            return self.create_daily_plan()
        else:
            raise ValueError(f"Unknown tool '{name}'.")
        self.planned = False
        return result

    def create_daily_plan(self) -> str:
        """T4: rebuild the plan (this also runs detect_conflicts)."""
        plan = self.scheduler.create_daily_plan(self.day)
        self.planned = True
        return (f"Planned {len(plan)} tasks, skipped {len(self.scheduler.skipped)}, "
                f"{len(self.scheduler.warnings)} conflicts.")


def describe_household(household: Household, day: date) -> str:
    """Write the household's people, pets and tasks as text for the LLM."""
    lines = [f"Household: {household.name}. Planning day: {day:%A %Y-%m-%d}.", "Members:"]
    for owner in household.members:
        hours = (f"{owner.home_start:%H:%M}-{owner.home_end:%H:%M}"
                 if owner.home_start and owner.home_end else "home all day")
        minutes = owner.available_minutes if owner.available_minutes is not None else "no limit"
        lines.append(f"  - {owner.name}: {hours}, care minutes: {minutes}")
    lines.append("Pets:")
    for pet in household.pets:
        needs = ", ".join(pet.special_needs) or "none"
        lines.append(f"  - {pet.name} ({pet.breed or 'unknown breed'}, age {pet.age}), "
                     f"priority {pet.priority}, special needs: {needs}")
    lines.append("Tasks:")
    for i, task in enumerate(household.tasks, start=1):
        pets = ", ".join(p.name for p in task.pets) or "no pet"
        due = f"at {task.due_time:%H:%M}" if task.due_time else "any time"
        who = task.assigned_to.name if task.assigned_to else "unassigned"
        status = "still to do" if task.is_pending_on(day) else "not due / done"
        lines.append(f"  - T{i}: {task.name} ({pets}), {task.duration_minutes} min, "
                     f"priority {task.priority}, {due}, {who}, "
                     f"{task.frequency or 'one-time'}, {status}")
    if not household.tasks:
        lines.append("  (none)")
    return "\n".join(lines)


# --- Prompts -------------------------------------------------------------------

PLANNER_SYSTEM = """You are the planning step of PawPal+, a pet care scheduling assistant \
for a household that shares pets. Read the owner's request and the household state.

Decide whether the request needs changes to the household (adding, editing or reassigning \
tasks, or changing someone's availability). Questions about the current plan need no changes.

Return JSON with:
- changes_household: true if anything must change, false for questions only.
- steps: a short ordered list of the changes you will make (empty for questions).
- reply: for questions, a short, friendly answer using the household state; \
for change requests, one sentence saying what you will do."""

ACT_SYSTEM = """You are the action step of PawPal+. Carry out the planned steps by calling \
the tools, one change at a time. Refer to tasks by id (T1, T2, ...) and to people and pets \
by name. Only change home hours or availability when the owner asked for it. \
When every change is made, call create_daily_plan, then reply with one sentence \
saying what you changed."""

FIX_SYSTEM = """You are the fix step of PawPal+. The schedule has the problems listed below. \
Resolve as many as you can using edit_task (move a task's time, shorten it, or change its \
priority) and assign_task (give it to another member who is home then). Prefer moving \
flexible changes over reassigning; don't change the owner's request. \
When you are done, reply with one sentence saying what you changed."""

PLAN_SCHEMA = {
    "type": "object",
    "properties": {
        "changes_household": {"type": "boolean"},
        "steps": {"type": "array", "items": {"type": "string"}},
        "reply": {"type": "string"},
    },
    "required": ["changes_household", "steps", "reply"],
}

# Gemini finish reasons that mean the answer was blocked
BLOCKED_REASONS = {"SAFETY", "PROHIBITED_CONTENT", "BLOCKLIST", "SPII", "RECITATION"}


def _api_error_message(exc: Exception) -> Optional[str]:
    """Turn an API failure into a message for the owner (None = not an API failure)."""
    if genai_errors is not None and isinstance(exc, genai_errors.APIError):
        if exc.code == 429:
            return "The AI service is busy or the free-tier limit was reached. Try again in a minute."
        if exc.code in (401, 403) or "API key" in str(exc):
            return "The Gemini API key is missing or invalid."
        if isinstance(exc, genai_errors.ServerError):
            return f"The AI service is having problems ({exc.code}). Try again shortly."
        return f"The AI service returned an error ({exc.code})."
    if isinstance(exc, (ConnectionError, TimeoutError)) or type(exc).__module__.startswith(
        ("httpx", "httpcore")
    ):
        return "Couldn't reach the AI service."
    return None


def _finish_reason(response: Any) -> str:
    """The first candidate's finish reason as text, e.g. "STOP" ("" if none)."""
    if not response.candidates:
        return ""
    reason = response.candidates[0].finish_reason
    return str(getattr(reason, "name", reason) or "")


# --- The agent -----------------------------------------------------------------

class PawPalAgent:
    """Runs one chat request through plan -> act -> check -> fix."""

    def __init__(self, client: Any = None, model: str = MODEL, max_rounds: int = MAX_ROUNDS):
        if client is None:
            if genai is None:
                raise RuntimeError("Install the 'google-genai' package to use the AI agent.")
            if not (os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")):
                raise RuntimeError("Set the GEMINI_API_KEY environment variable to use the AI agent.")
            client = genai.Client()
        self.client = client
        self.model = model
        self.max_rounds = max_rounds

    # Entry point ------------------------------------------------------------
    def run(
        self, request: str, household: Household, version: int, day: Optional[date] = None
    ) -> RunResult:
        """Handle one request. Never changes `household`; the result holds a draft."""
        result = self.start_run(request, household, version, day or date.today())
        toolbox = ToolBox(result.draft, result.day)
        try:
            self.plan(result)
            if result.answer_only:
                self.answer(result, toolbox)
            else:
                self.execute(result, toolbox)
                self.check_and_fix(result, toolbox)
                result.changed = household_differs(result.draft, household)
        except AgentError as exc:
            result.error = str(exc)
            result.record("ERROR", f"{exc} Draft discarded.")
        return result

    def start_run(self, request: str, household: Household, version: int, day: date) -> RunResult:
        """round = 0, copy Household -> Draft, note version."""
        result = RunResult(request, copy.deepcopy(household), version, day)
        result.record("START", f"Request: {request!r} (household version {version})")
        return result

    # LLM call ---------------------------------------------------------------
    def _call_llm(
        self,
        system: str,
        contents: list[Any],
        tools: Optional[list[dict[str, Any]]] = None,
        schema: Optional[dict[str, Any]] = None,
    ) -> Any:
        """One Gemini request. Tools are run by our code, not the SDK."""
        config: dict[str, Any] = {"system_instruction": system, "max_output_tokens": MAX_TOKENS}
        if tools:
            config["tools"] = [{"function_declarations": tools}]
            config["automatic_function_calling"] = {"disable": True}
        if schema:
            config["response_mime_type"] = "application/json"
            config["response_json_schema"] = schema
        try:
            response = self.client.models.generate_content(
                model=self.model, contents=contents, config=config
            )
        except Exception as exc:
            message = _api_error_message(exc)
            if message is None:
                raise
            raise AgentError(message) from exc

        reason = _finish_reason(response)
        if not response.candidates or reason in BLOCKED_REASONS:
            raise AgentError("The AI declined this request.")
        if reason == "MAX_TOKENS":
            raise AgentError("The AI's answer was cut off.")
        if reason == "MALFORMED_FUNCTION_CALL":
            raise AgentError("The AI sent a tool call that couldn't be read.")
        return response

    # 1 · PLAN + Route -------------------------------------------------------
    def plan(self, result: RunResult) -> None:
        """Ask the LLM what the request needs, and route it."""
        state = describe_household(result.draft, result.day)
        prompt = f"{state}\n\nOwner's request: {result.request}"
        response = self._call_llm(
            PLANNER_SYSTEM, [{"role": "user", "parts": [{"text": prompt}]}], schema=PLAN_SCHEMA
        )
        try:
            data = json.loads(response.text or "")
            data["changes_household"], data["steps"], data["reply"]
        except (json.JSONDecodeError, KeyError, TypeError) as exc:
            raise AgentError("The AI's plan couldn't be read.") from exc
        result.answer_only = not data["changes_household"]
        result.steps = list(data["steps"])
        result.reply = data["reply"]
        route = "question only" if result.answer_only else f"{len(result.steps)} steps"
        result.record("PLAN", f"{route}: {result.steps or result.reply}")

    # Answer only ------------------------------------------------------------
    def answer(self, result: RunResult, toolbox: ToolBox) -> None:
        """1 build plan, 2 explain. Nothing is saved for questions."""
        toolbox.create_daily_plan()
        self.explain(result, toolbox)

    # 2 · ACT ----------------------------------------------------------------
    def execute(self, result: RunResult, toolbox: ToolBox) -> None:
        """Let the LLM make the planned changes, then make sure the plan is rebuilt."""
        steps = "\n".join(f"{i}. {s}" for i, s in enumerate(result.steps, start=1))
        prompt = (f"{describe_household(result.draft, result.day)}\n\n"
                  f"Owner's request: {result.request}\nPlanned steps:\n{steps}")
        self._tool_loop(result, "ACT", ACT_SYSTEM, prompt, ACT_TOOLS, toolbox)
        if not toolbox.planned:
            toolbox.create_daily_plan()
            result.record("ACT", "create_daily_plan run after the last change.")

    def _tool_loop(
        self, result: RunResult, step: str, system: str, prompt: str,
        tools: list[dict[str, Any]], toolbox: ToolBox,
    ) -> None:
        """Tool-use loop: call the LLM, run its function calls, send the results back."""
        contents: list[Any] = [{"role": "user", "parts": [{"text": prompt}]}]
        failures = 0
        for _ in range(MAX_TOOL_TURNS):
            response = self._call_llm(system, contents, tools=tools)
            # Send the model's turn back unchanged (it may carry thought signatures)
            contents.append(response.candidates[0].content)
            calls = response.function_calls or []
            if not calls:
                return
            parts = []
            for call in calls:
                args = dict(call.args or {})
                try:
                    output = toolbox.run(call.name, args)
                except (ValueError, KeyError, TypeError) as exc:
                    failures += 1
                    result.record(step, f"{call.name} failed ({failures}/{MAX_TOOL_RETRIES} "
                                        f"retries): {exc}")
                    if failures > MAX_TOOL_RETRIES:
                        raise AgentError(f"Tool calls kept failing: {exc}") from exc
                    reply = {"error": str(exc)}
                else:
                    result.record(step, f"{call.name}({json.dumps(args)}) -> {output}")
                    reply = {"result": output}
                function_response = {"name": call.name, "response": reply}
                if getattr(call, "id", None):
                    function_response["id"] = call.id
                parts.append({"function_response": function_response})
            contents.append({"role": "user", "parts": parts})
        raise AgentError(f"The {step} step took more than {MAX_TOOL_TURNS} turns.")

    # 3 · CHECK -> Guard -> Rounds -> 4 · FIX loop ------------------------------
    def check_and_fix(self, result: RunResult, toolbox: ToolBox) -> None:
        """Repeat check -> fix until clean, out of rounds, or no longer improving."""
        last_count: Optional[int] = None
        while True:
            fixable = self.check(result, toolbox)
            if not fixable:  # Guard: nothing fixable left
                break
            no_progress = last_count is not None and len(fixable) >= last_count
            if result.rounds >= self.max_rounds or no_progress:
                self.flag_unresolved(result, "no progress" if no_progress else "max rounds")
                break
            last_count = len(fixable)
            self.fix(result, toolbox)
            result.rounds += 1
        self.explain(result, toolbox)

    def check(self, result: RunResult, toolbox: ToolBox) -> list[str]:
        """Read conflicts (T5) and skipped tasks; split skipped into fixable / unfixable."""
        result.conflicts = toolbox.scheduler.detect_conflicts()
        fixable_skips, unfixable = [], []
        for task, why in toolbox.scheduler.skipped:
            target = unfixable if any(r in why for r in UNFIXABLE_REASONS) else fixable_skips
            target.append(f"{task.name} skipped: {why}")
        result.fixable = result.conflicts + fixable_skips
        result.unfixable = unfixable
        result.record("CHECK", f"round {result.rounds}: {len(result.fixable)} fixable, "
                               f"{len(unfixable)} unfixable")
        return result.fixable

    def fix(self, result: RunResult, toolbox: ToolBox) -> None:
        """1 edit (LLM), 2 re-plan (create_daily_plan)."""
        problems = "\n".join(f"- {p}" for p in result.fixable)
        prompt = (f"{describe_household(result.draft, result.day)}\n\n"
                  f"Owner's request: {result.request}\nProblems to fix:\n{problems}")
        self._tool_loop(result, "FIX", FIX_SYSTEM, prompt, FIX_TOOLS, toolbox)
        toolbox.create_daily_plan()

    def flag_unresolved(self, result: RunResult, why: str) -> None:
        """Keep the remaining problems as warnings for the owner."""
        result.unresolved = list(result.fixable)
        result.record("FIX", f"stopped ({why}); {len(result.unresolved)} left unresolved")

    def explain(self, result: RunResult, toolbox: ToolBox) -> None:
        """T6: explain_plan, plus the plan itself for the display."""
        result.explanation = toolbox.scheduler.explain_plan()
        result.plan = list(toolbox.scheduler.daily_plan)
        result.skipped = list(toolbox.scheduler.skipped)


# --- Review + commit (used by the UI) --------------------------------------------

def household_differs(draft: Household, saved: Household) -> bool:
    """Draft differs from saved household?"""
    return database.household_to_dict(draft) != database.household_to_dict(saved)


def commit(conn: sqlite3.Connection, household_code: str, result: RunResult) -> int:
    """Save the approved draft. Raises database.VersionConflict if another member saved first."""
    new_version = database.save_household(conn, household_code, result.draft, result.base_version)
    result.record("COMMIT", f"saved as version {new_version}")
    return new_version


def rollback(conn: sqlite3.Connection, household_code: str) -> tuple[Household, int]:
    """Discard the draft and reload the latest saved household."""
    return database.load_household(conn, household_code)
