"""Demo: sign up, log in, plan today, save to the database, and (optionally) ask the agent.

Run:
    python main.py            # sign-up, login, schedule, database, sorting demos
    python main.py --agent    # also sends one request to the AI agent (needs an API key)
"""

import sys
from datetime import time

import agent
import auth
import database
from auth import PetInput, SignupForm
from pawpal_system import Scheduler, Task

PASSWORD = "biscuit123"


def signup_demo(conn) -> str:
    """Create Jo's account and household, then Sam joins with the household code."""
    form = SignupForm(
        first_name="Jo", last_name="Wan", age=34, email="jo@example.com", password=PASSWORD,
        new_household_name="Jo's Home",
        pets=[
            PetInput("Biscuit", breed="Golden Retriever", age=4, special_needs=["joint medicine"]),
            PetInput("Mochi", breed="Tabby Cat", age=2),
        ],
    )
    errors = auth.validate_signup(conn, form)
    assert not any(errors.values()), errors
    _, code = auth.save_account(conn, form)

    # Sam joins the existing household, so no new pets are needed
    sam_form = SignupForm("Sam", "Wan", 31, "sam@example.com", PASSWORD, household_code=code)
    assert not any(auth.validate_signup(conn, sam_form).values())
    auth.save_account(conn, sam_form)

    # A second sign-up with Jo's email is rejected
    duplicate = SignupForm("Jo", "Again", 30, "jo@example.com", PASSWORD, household_code=code)
    print("Sign-up")
    print("=" * 40)
    print(f"Created household code {code}; Jo and Sam are members")
    print("Duplicate email:", auth.validate_signup(conn, duplicate)["owner"])
    return code


def login_demo(conn) -> auth.Session:
    """Show a failed login, then log Jo in and start a session."""
    print("\nLogin")
    print("=" * 40)
    print("Wrong password:", auth.login(conn, "jo@example.com", "wrong").message)
    result = auth.login(conn, "jo@example.com", PASSWORD)
    print("Right password:", result.message)
    return auth.start_session(conn, result.owner_id)


def schedule_demo(conn, session: auth.Session) -> None:
    """Set availability, add tasks, save them, and print today's schedule."""
    household = session.household
    jo = session.owner
    sam = next(m for m in household.members if m is not jo)
    biscuit, mochi = household.pets

    # Jo is home 7 AM - 9 PM; Sam is home 9 AM - 5 PM
    jo.update_home_hours(time(7, 0), time(21, 0))
    sam.update_home_hours(time(9, 0), time(17, 0))

    # Three tasks for Biscuit
    household.add_task(Task("Morning walk", 30, 1, pets=[biscuit]), due_time=time(7, 30), assigned_to=jo)
    household.add_task(Task("Joint medicine", 5, 1, pets=[biscuit]), due_time=time(12, 0), assigned_to=jo)
    household.add_task(Task("Evening fetch", 20, 2, pets=[biscuit]), due_time=time(18, 0), assigned_to=jo)

    # Three tasks for Mochi
    household.add_task(Task("Breakfast", 10, 1, pets=[mochi]), due_time=time(8, 15), assigned_to=jo)
    household.add_task(Task("Clean litter box", 10, 2, pets=[mochi]), due_time=time(15, 0), assigned_to=jo)
    household.add_task(Task("Brushing", 15, 3, pets=[mochi]), due_time=time(20, 0), assigned_to=jo)

    # Two tasks at the same time: Biscuit can't be with Jo and Sam at once
    household.add_task(Task("Grooming", 20, 2, pets=[biscuit]), due_time=time(10, 0), assigned_to=jo)
    household.add_task(Task("Vet call", 15, 1, pets=[biscuit]), due_time=time(10, 0), assigned_to=sam)

    auth.save_session(conn, session)

    # Load it back from the database to show the save worked
    saved, version = database.load_household(conn, session.household_code)
    print(f"\nSaved to database: version {version}, {len(saved.tasks)} tasks, "
          f"{len(saved.members)} members")

    scheduler = Scheduler(saved)
    plan = scheduler.create_daily_plan()

    print("\nToday's Schedule")
    print("=" * 40)
    for item in plan:
        pets = ", ".join(pet.name for pet in item.task.pets)
        print(
            f"{item.start_time:%I:%M %p} - {item.end_time():%I:%M %p}  "
            f"{item.task.name} ({pets}) [priority {item.task.priority}]"
        )

    if scheduler.skipped:
        print("\nSkipped:")
        for task, reason in scheduler.skipped:
            print(f"  {task.name}: {reason}")

    for warning in scheduler.warnings:
        print(f"WARNING: {warning}")


def version_conflict_demo(conn, code: str) -> None:
    """Two members load the same household; the second save is refused."""
    jo_copy, version = database.load_household(conn, code)
    sam_copy, _ = database.load_household(conn, code)
    database.save_household(conn, code, jo_copy, version)
    try:
        database.save_household(conn, code, sam_copy, version)
    except database.VersionConflict as exc:
        print("\nVersion check:", exc)


def agent_demo(conn, session: auth.Session) -> None:
    """Send one chat request through the AI agent, then approve its draft."""
    session = auth.reload_session(conn, session)
    request = "Biscuit's grooming and vet call are both at 10:00. Please sort that out."
    print("\nAI agent")
    print("=" * 40)
    print("Request:", request)
    result = agent.PawPalAgent().run(request, session.household, session.version)
    for line in result.log:
        print(" ", line)
    if result.error:
        print("Error:", result.error)
        return
    print(result.reply)
    print(result.explanation)
    if result.needs_approval:
        version = agent.commit(conn, session.household_code, result)
        print(f"Approved and saved as version {version}.")


def out_of_order_demo(conn, session: auth.Session) -> None:
    """Show the saved tasks in the order they were added, then sorted and filtered."""
    session = auth.reload_session(conn, session)
    household = session.household
    scheduler = Scheduler(household)

    print("\n\nTasks in the order they were added")
    print("=" * 40)
    for task in household.tasks:
        print(f"{task.due_time:%H:%M}  {task.name}")

    print("\nTasks sorted by time")
    print("=" * 40)
    for task in scheduler.sort_by_time():
        print(f"{task.due_time:%H:%M}  {task.name}")

    # Check off one task, then try the filters
    morning_walk = next(t for t in household.tasks if t.name == "Morning walk")
    household.check_off_task(morning_walk, by=session.owner)
    auth.save_session(conn, session)

    print("\nCompleted today:", [t.name for t in scheduler.filter_tasks(completed=True)])
    print("Still to do:", [t.name for t in scheduler.filter_tasks(completed=False)])
    print("Mochi's tasks:", [t.name for t in scheduler.filter_tasks(pet_name="mochi")])


def main() -> None:
    conn = database.connect(":memory:")  # throwaway database for the demo
    code = signup_demo(conn)
    session = login_demo(conn)
    schedule_demo(conn, session)
    version_conflict_demo(conn, code)
    if "--agent" in sys.argv:
        agent_demo(conn, session)
    else:
        print("\n(Run with --agent to send a request to the AI agent.)")
    out_of_order_demo(conn, session)


if __name__ == "__main__":
    main()
