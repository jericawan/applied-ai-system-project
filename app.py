"""PawPal+ Streamlit app: login / sign-up, AI agent chat, and manual task tools.

Flow (diagrams/account_creation.mmd, then diagrams/agent_workflow.mmd):
    login / sign-up -> session -> chat request -> agent draft -> approve -> commit
"""

from datetime import date, time

import streamlit as st

import agent
import auth
import database
from pawpal_system import Pet, Scheduler, Task

st.set_page_config(page_title="PawPal+", page_icon="🐾", layout="centered")

PRIORITY_TO_INT = {"high": 1, "medium": 2, "low": 3}
REPEAT_OPTIONS = {"does not repeat": None, "daily": "daily", "weekly": "weekly"}


@st.cache_resource
def get_connection():
    """One SQLite connection shared across reruns (file: pawpal.db or $PAWPAL_DB)."""
    return database.connect()


conn = get_connection()


INT_TO_PRIORITY = {number: label for label, number in PRIORITY_TO_INT.items()}


def priority_label(priority):
    """1 -> "High", 2 -> "Medium", 3 -> "Low" (other numbers shown as-is)."""
    return INT_TO_PRIORITY.get(priority, str(priority)).capitalize()


def plan_table(plan):
    """Rows for st.table from a list of ScheduledTask."""
    return [
        {
            "Time": f"{item.start_time:%I:%M %p} - {item.end_time():%I:%M %p}",
            "Task": item.task.name,
            "Pet": ", ".join(p.name for p in item.task.pets),
            "Who": item.assigned_to.name if item.assigned_to else "",
            "Why": item.reason,
        }
        for item in plan
    ]


def save_session_or_reload(session):
    """Save manual edits; if another member saved first, reload their version instead."""
    try:
        auth.save_session(conn, session)
        return True
    except database.VersionConflict:
        st.session_state.session = auth.reload_session(conn, session)
        st.session_state.flash = ("warning", "Another member saved changes first, so yours weren't "
                                             "saved. The latest household is loaded; please try again.")
        return False


# --- Login & account creation ----------------------------------------------------

def show_login():
    with st.form("login"):
        email = st.text_input("Email")
        password = st.text_input("Password", type="password")
        submitted = st.form_submit_button("Log in")
    if submitted:
        result = auth.login(conn, email, password)
        if result.ok:
            st.session_state.session = auth.start_session(conn, result.owner_id)
            st.rerun()
        st.error(result.message)
    st.caption("No account yet? Open the **Create account** tab.")


def _parse_optional_time(text):
    text = (text or "").strip()
    return time.fromisoformat(text) if text else None


def show_signup():
    st.markdown("**1. About you**")
    col1, col2 = st.columns(2)
    first_name = col1.text_input("First name")
    last_name = col2.text_input("Last name")
    age = st.text_input("Age")
    email = st.text_input("Email", key="signup_email")
    password = st.text_input("Password (8+ characters)", type="password", key="signup_password")

    st.markdown("**2. Household**")
    choice = st.radio("Are you part of an existing household?",
                      ["No, create a new household", "Yes, join with a code"])
    joining = choice.startswith("Yes")
    household_code = new_household_name = None
    existing_pets = []
    if joining:
        household_code = st.text_input("Household code").strip().upper() or None
        if household_code:
            found = auth.find_household(conn, household_code)
            if found is None:
                st.error("No household has that code.")
            else:
                existing_pets = [p.name for p in found[0].pets]
                pets_text = ", ".join(existing_pets) or "no pets yet"
                st.success(f"Found **{found[0].name}** ({pets_text}).")
    else:
        new_household_name = st.text_input("Household name", placeholder="The Williams Home")

    st.markdown("**3. Pets**")
    if existing_pets:
        st.caption("This household already has pets, so adding more is optional.")
    pet_rows = st.data_editor(
        [{"name": "", "breed": "", "age": 0, "special_needs": ""}],
        num_rows="dynamic", key="signup_pets", width="stretch",
        column_config={"special_needs": st.column_config.TextColumn(
            "special needs", help="Separate with commas")},
    )
    st.caption("Care tasks (optional). Time is HH:MM, or leave blank for any time.")
    care_rows = st.data_editor(
        [{"pet": "", "task": "", "minutes": 15, "priority": "medium", "time": "", "repeats": "daily"}],
        num_rows="dynamic", key="signup_tasks", width="stretch",
        column_config={
            "priority": st.column_config.SelectboxColumn(options=list(PRIORITY_TO_INT)),
            "repeats": st.column_config.SelectboxColumn(options=list(REPEAT_OPTIONS)),
        },
    )

    if not st.button("Create account", type="primary"):
        return

    # Build the form from the inputs (blank rows are ignored)
    form_errors = []
    pets = [
        auth.PetInput(row["name"], breed=row["breed"] or "", age=row["age"] or 0,
                      special_needs=(row["special_needs"] or "").split(","))
        for row in pet_rows if (row.get("name") or "").strip()
    ]
    by_name = {p.name.strip().lower(): p for p in pets}
    for row in care_rows:
        if not (row.get("task") or "").strip():
            continue
        pet = by_name.get((row.get("pet") or "").strip().lower())
        if pet is None:
            form_errors.append(f"Care task '{row['task']}' is for a pet that isn't in the pet list.")
            continue
        try:
            due = _parse_optional_time(row.get("time"))
        except ValueError:
            form_errors.append(f"'{row['task']}' time must be HH:MM.")
            continue
        pet.care_tasks.append(auth.CareTaskInput(
            row["task"], int(row.get("minutes") or 0),
            PRIORITY_TO_INT.get(row.get("priority"), 2), due,
            REPEAT_OPTIONS.get(row.get("repeats"), "daily"),
        ))

    form = auth.SignupForm(first_name, last_name, age, email, password,
                           household_code=household_code if joining else None,
                           new_household_name=new_household_name, pets=pets)
    errors = auth.validate_signup(conn, form)
    errors["pets"] += form_errors
    if any(errors.values()):
        for section, title in [("owner", "About you"), ("household", "Household"), ("pets", "Pets")]:
            for message in errors[section]:
                st.error(f"{title}: {message}")
        return

    owner_id, code = auth.save_account(conn, form)
    st.session_state.session = auth.start_session(conn, owner_id)
    st.session_state.flash = ("success", f"Account created! Your household code is {code}.")
    st.rerun()


# --- AI agent chat ---------------------------------------------------------------

def show_agent(session, plan_day):
    st.subheader("Ask PawPal+")
    st.caption("Ask a question about your plan, or ask for changes "
               "(e.g. \"Move Mochi's brushing to the evening\"). You approve changes before they're saved.")

    with st.form("chat", clear_on_submit=True):
        request = st.text_area("Your request", height=80)
        sent = st.form_submit_button("Send")
    if sent and request.strip():
        feedback = st.session_state.pop("feedback", "")
        full_request = request.strip() + (f"\n(Feedback on the last plan: {feedback})" if feedback else "")
        try:
            pawpal_agent = agent.PawPalAgent()
        except Exception as exc:  # missing package or credentials
            st.error(f"The AI agent isn't available: {exc}")
            return
        with st.spinner("Planning..."):
            st.session_state.agent_result = pawpal_agent.run(
                full_request, session.household, session.version, plan_day)

    result = st.session_state.get("agent_result")
    if result is None:
        return

    with st.chat_message("user"):
        st.write(result.request)
    with st.chat_message("assistant"):
        if result.error:
            st.error(f"{result.error} Nothing was changed.")
        else:
            st.write(result.reply)
            if result.plan:
                st.table(plan_table(result.plan))
            for warning in result.unresolved:
                st.warning(f"⚠️ Couldn't fix: {warning}")
            for note in result.unfixable:
                st.info(f"Can't be fixed by rescheduling: {note}")
            if result.rounds:
                st.caption(f"Fixed conflicts in {result.rounds} round(s).")
            with st.expander("Why this plan"):
                st.text(result.explanation)
        with st.expander("Agent trace"):
            st.text("\n".join(result.log))

    if result.error or result.answer_only:
        if result.answer_only:
            st.caption("This was a question, so nothing needs saving.")
        return
    if not result.changed:
        st.caption("No changes were needed.")
        return

    feedback = st.text_input("Feedback if you reject (optional)", key="feedback_box")
    col1, col2 = st.columns(2)
    if col1.button("Approve and save", type="primary"):
        try:
            agent.commit(conn, session.household_code, result)
            st.session_state.flash = ("success", "Saved!")
        except database.VersionConflict:
            st.session_state.flash = ("warning", "Another member saved changes first, so this plan "
                                                 "wasn't saved. Please send your request again.")
        st.session_state.session = auth.reload_session(conn, session)
        del st.session_state.agent_result
        st.rerun()
    if col2.button("Reject"):
        if feedback.strip():
            st.session_state.feedback = feedback.strip()
        st.session_state.session = auth.reload_session(conn, session)
        del st.session_state.agent_result
        st.session_state.flash = ("info", "Discarded. Any feedback will be added to your next request.")
        st.rerun()


# --- To-do checklist ---------------------------------------------------------------

def show_todo_checklist(session, plan_day):
    """A checkbox for each task due on plan_day; checking one marks it done and saves."""
    household, owner = session.household, session.owner
    st.subheader(f"To-Do List · {plan_day:%A, %b %d}")

    # Tasks still to do that day, plus ones already done that day, earliest first
    todays = [t for t in household.tasks if t.is_pending_on(plan_day) or t.is_done_on(plan_day)]
    if not todays:
        st.info("Nothing to do on this day.")
        return

    done_count = sum(t.is_done_on(plan_day) for t in todays)
    st.progress(done_count / len(todays), text=f"{done_count} of {len(todays)} done")

    for task in Scheduler(household).sort_by_time(todays):
        when = task.due_time.strftime("%I:%M %p") if task.due_time else "Any time"
        pets = ", ".join(p.name for p in task.pets) or "no pet"
        who = f" · {task.assigned_to.name}" if task.assigned_to else ""
        label = f"**{task.name}** ({pets}) · {when} · {task.duration_minutes} min{who}"
        done = task.is_done_on(plan_day)
        if done and task.completed_by is not None:
            label += f" · done by {task.completed_by.name}"

        # Key by position in household.tasks (Household only accepts its own Task objects).
        # Done tasks are locked: checking off can't be undone once the next copy exists.
        # The key includes the status so a finished task gets a fresh, ticked checkbox.
        key = f"todo_{household.tasks.index(task)}_{plan_day}_{'done' if done else 'open'}"
        checked = st.checkbox(label, value=done, disabled=done, key=key)
        if checked and not done:
            next_task = household.check_off_task(task, by=owner, on=plan_day)
            if save_session_or_reload(session):
                message = f"Done! '{task.name}' is checked off."
                if next_task is not None:
                    message += f" Next one added for {next_task.due_date:%A, %b %d}."
                st.session_state.flash = ("success", message)
            else:
                st.session_state.pop(key, None)  # not saved: untick so it isn't retried
            st.rerun()


# --- Manual task tools (saved to the database) -----------------------------------

def show_manual_tools(session, plan_day):
    household, owner = session.household, session.owner

    st.subheader("Add a Task")
    pet_names = [p.name for p in household.pets]
    pet_choice = st.selectbox("Pet", pet_names + ["➕ New pet"])
    if pet_choice == "➕ New pet":
        col1, col2 = st.columns(2)
        new_pet_name = col1.text_input("Pet name", value="Mochi")
        species = col2.selectbox("Species", ["dog", "cat", "other"])

    col1, col2, col3 = st.columns(3)
    task_title = col1.text_input("Task title", value="Morning walk")
    duration = col2.number_input("Duration (minutes)", min_value=1, max_value=240, value=20)
    priority = col3.selectbox("Priority", ["low", "medium", "high"], index=2)

    members = {m.name: m for m in household.members}
    assignee = st.selectbox("Who does it", [owner.name] + [n for n in members if n != owner.name]
                            + ["Auto-assign"])
    has_due_time = st.checkbox("Must be done at a set time")
    due_time = st.time_input("Due time", value=time(17, 0)) if has_due_time else None
    frequency = REPEAT_OPTIONS[st.selectbox("Repeats", list(REPEAT_OPTIONS))]
    first_due = st.date_input("First due date", value=date.today()) if frequency else None

    if st.button("Add task"):
        if pet_choice == "➕ New pet":
            pet = next((p for p in household.pets if p.name == new_pet_name), None)
            if pet is None:
                pet = Pet(name=new_pet_name, breed=species)
                household.add_pet(pet)
        else:
            pet = next(p for p in household.pets if p.name == pet_choice)
        task = Task(name=task_title, duration_minutes=int(duration),
                    priority=PRIORITY_TO_INT[priority], pets=[pet],
                    frequency=frequency, due_date=first_due)
        household.add_task(task, due_time=due_time, assigned_to=members.get(assignee))

        # Warn right away if the new task overlaps another timed task
        clashes = Scheduler(household).find_clashes(task)
        if save_session_or_reload(session):
            st.success(f"Added '{task_title}' for {pet.name}.")
            for other in clashes:
                same_task = other.name.strip().lower() == task.name.strip().lower()
                kind = "Possible duplicate" if same_task else "Time clash"
                message = (f"{kind}: '{task.name}' at {task.due_time:%I:%M %p} overlaps "
                           f"'{other.name}' ({', '.join(p.name for p in other.pets)}) "
                           f"at {other.due_time:%I:%M %p}.")
                st.warning(f"⚠️ {message}")
                st.toast(message, icon="⚠️")
        else:
            st.rerun()

    if household.tasks:
        st.write("Current tasks:")
        st.table([
            {
                "Task": t.name,
                "Pet": ", ".join(p.name for p in t.pets),
                "Duration (minutes)": t.duration_minutes,
                "Priority": priority_label(t.priority),
                "Due time": t.due_time.strftime("%I:%M %p") if t.due_time else "Any time",
                "Assigned to": t.assigned_to.name if t.assigned_to else "Not assigned",
                "Repeats": (t.frequency or "no").capitalize(),
                "Due date": t.due_date.strftime("%b %d") if t.due_date else "Every day",
                "Done": "Yes" if t.last_completed else "",
            }
            for t in Scheduler(household).sort_by_time()
        ])
    else:
        st.info("No tasks yet. Add one above, or ask PawPal+.")

    st.divider()
    show_todo_checklist(session, plan_day)

    st.divider()
    st.subheader("Build Schedule")
    st.caption("Plans the tasks still to do on the chosen day (not saved).")
    if st.button("Generate schedule"):
        scheduler = Scheduler(household)
        plan = scheduler.create_daily_plan(plan_day)
        if plan:
            st.table(plan_table(plan))
        else:
            st.info("No tasks to schedule on this day.")
        for task, reason in scheduler.skipped:
            st.warning(f"Skipped {task.name}: {reason}")
        for warning in scheduler.warnings:
            st.warning(f"⚠️ {warning}")
        # Planning assigns unassigned tasks; reload so that isn't kept unsaved
        st.session_state.session = auth.reload_session(conn, session)


# --- Page ------------------------------------------------------------------------

st.title("🐾 PawPal+")

if "session" not in st.session_state:
    st.caption("Plan your household's pet care together.")
    login_tab, signup_tab = st.tabs(["Log in", "Create account"])
    with login_tab:
        show_login()
    with signup_tab:
        show_signup()
    st.stop()

session = st.session_state.session

with st.sidebar:
    st.markdown(f"**{session.owner.name}**")
    st.markdown(f"Household: **{session.household.name}**")
    st.markdown(f"Household code: `{session.household_code}`")
    st.caption("Share the code so others can join.")
    plan_day = st.date_input("Day", value=date.today())
    if st.button("Log out"):
        st.session_state.clear()
        st.rerun()

if "flash" in st.session_state:
    kind, message = st.session_state.pop("flash")
    getattr(st, kind)(message)

show_agent(session, plan_day)
st.divider()
show_manual_tools(session, plan_day)
