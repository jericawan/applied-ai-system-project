# Model Card: PawPal 2.0

## Overview

PawPal 2.0 is a pet-care scheduler for households where several people share the pets. You can type a request in plain English, such as "Alex is away this weekend, who walks Buddy?", and an AI agent built on Google Gemini (`gemini-3.8-flash`, using function calling) plans the changes. It makes them on a draft copy of the household using a small set of tools, then checks the result with the scheduler and fixes any conflicts it finds. Nothing is saved until the owner approves the plan.

- **Model:** Google Gemini, through the `google-genai` SDK. The model can be changed with the `GEMINI_MODEL` setting.


## How I collaborated with AI during development

I used an AI coding assistant (Claude Code) throughout the project:

- **Design:** I had it review my architecture diagram for logic and data-flow mistakes. Later I asked it to split the diagram into two (account creation, and the agent workflow) because one diagram had become too hard to read.
- **Building:** it helped turn the diagram into code. That included the SQLite database, accounts and login, the agent's plan → act → check → fix loop, and the Streamlit pages.
- **Debugging:** I pasted terminal errors and asked what they meant. Examples: the `quote>` prompt caused by curly quotes, "install google-genai" caused by running the wrong Python environment, and Gemini 503 errors.
- **Testing:** it wrote pytest tests with a fake Gemini client, so the agent can be tested without an API key or spending free-tier requests.

I tested every change myself in the app and decided which suggestions to keep.

## One AI suggestion I accepted

**Working on a draft and requiring approval.** The assistant suggested that the agent edit a copy of the household, not the saved data, and that changes only be saved after I approve them, with a version check so two family members can't overwrite each other. I kept this because it means a bad AI plan can never damage the real schedule. It became the core safety feature of the project.

## One AI suggestion I rejected or revised

**What "Reject" does.** In the first version, rejecting a plan just threw it away and you had to start over. I changed it so the feedback box sends the original request, the rejected plan and my feedback back to the agent, which then makes a revised plan.

Other things I didn't accept as suggested:

- The assistant first built the agent on the Anthropic (Claude) API. I switched it to Google Gemini because I could get a free API key.
- When it listed several possible fixes for problems that only happen when many people use the app at once, I asked which ones were actually needed and only added three of them.

**A flawed result I had to catch:** the first version of the agent had no tool for adding a pet. When I asked it to add a new cat, it created a feeding task linked to no pet at all, instead of failing. I fixed this by adding an `add_pet` tool and a rule that every task must belong to at least one pet.

## Testing and reliability
I used **automated testing** to ensure that all of my functions work properly. 
**terminal:**
(.venv) (base) jericawan@MacBook-Air applied-ai-system-poject % cd /Users/jericawan/Desktop/codepath26/AI110/applied-ai-system-project
.venv/bin/python -m pytest
====================================================== test session starts ======================================================
platform darwin -- Python 3.13.9, pytest-9.2.0, pluggy-1.7.0
rootdir: /Users/jericawan/Desktop/codepath26/AI110/applied-ai-system-project
plugins: anyio-4.16.0
collected 142 items                                                                                                             

tests/test_agent.py ..........................                                                                            [ 18%]
tests/test_away.py ..............                                                                                         [ 28%]
tests/test_database_auth.py ...................                                                                           [ 41%]
tests/test_pawpal.py ...................................................................................                  [100%]

====================================================== 142 passed in 1.25s ======================================================
(.venv) (base) jericawan@MacBook-Air applied-ai-system-project % 

I also found it was important to include logging/error handling. An example of something I implemented was ensuring that all manual inputs (account creation, manual task adding) follow a guideline of filling out the basics (ex. password must be at least 8 characteres), and if it does not meet the guidelines it will not save the content and will provide a error message. 
## Limitations and biases
- **Greedy scheduling:** tasks are placed in priority order, then in the order they were added. The scheduler doesn't search for the best overall schedule, so tasks entered first can get the better times.
- **No home hours means "home all day":** a member with no home hours set can get tasks at midnight. Home hours can now be set in the Availability section, but sign-up doesn't ask for them.
- **The AI can misunderstand requests.** Vague requests, or names that don't match a pet or member, can lead to the wrong change. The check step and the approval step catch most of these, but the user still has to read the plan.
- **Bias toward one kind of household:** it assumes one time zone, English-language requests and a fairly regular daily routine. Members are treated as either available or away, with no in-between.

## Potential misuse of AI
Currently I do not think that there is a way to misuse the AI. One potential error that I personally have not tested is removing family members from a household which could be slightly problematic. 

## AI Reliability 
What surprised me about AI is that it can't always be trusted as it may interpret your wording differently than expected. For me I felt that I had to repeat requests just because they were taken as something else and I had to be very specific about the changes I want to make. 

## Suggestions and Rejections 
- **Accepted:** Something that I accepted was including the requirement that "every task needs a pet". This ensures that the logic does not break when creating a task, and each pet has a task and vice versa. This was something I personally didn't think of since I made assumptions that it was something that was already implemented. 
- **Rejected:** Orginally my agent had the ability to reject or accept changes, with opportunity to give feedback. I found that it was important to implement a back and forward between the user and agent where the user can make changes then have the agent rework their idea. 
## Future Improvements 
- Ask for home hours during sign-up, and warn when a member has none set.
- Replace greedy scheduling with a smarter approach that balances tasks fairly between members.
- make the web design to display weekly tasks versus personal tasks. 
