# How this project works — explained simply

Read this top to bottom once, then open each file it points to. ~20 minutes.

---

## 1. The big picture: a small shop with three employees

Imagine a shop.

- **The Analyst** is a junior employee. Every morning he looks at the stock reports and writes sticky notes: *"Order 470 oat milks for Paris!"*
- **The Reviewer** is a picky senior. She takes each sticky note, goes back to the reports **herself**, and writes on it: *"Agree"*, *"No"*, or *"Wrong number, it's 455."*
- **The Manager (you, the human)** reads each note with both opinions and signs or tears it up.
- **The Clerk** (plain Python) only sends orders the Manager signed. He can't read, can't think, can't be convinced. He just obeys signatures.

The two AI employees **never touch the phone to the supplier**. That's the whole safety idea.

---

## 2. What is an "agent", really? → `pilot/agent.py`

An LLM alone is a brain in a jar: it can only talk.
A **tool** is a button we give it: "run this SQL", "submit a proposal".
An **agent** is the LLM + buttons + a loop:

```
while True:
    ask the model: "here's the task, here's what happened so far, here are your buttons"
    if the model just talks  -> it's finished, stop
    if the model asks to press buttons -> WE press them (in Python), and tell it what happened
```

Key point for interviews: **the model never presses the button itself.** It writes "please press `query_certified_metrics` with this SQL", and our code decides whether to do it. That's where all the control lives.

Look at `Agent.run()`: that loop is ~20 lines. `max_steps` is the emergency brake so an agent can't loop forever (and burn money).

`GeminiLLM` is just a translator between our simple message format and Google's format. If tomorrow you use Claude or GPT, you write another translator; the agents don't change.

---

## 3. Tools and governance → `pilot/tools.py`

A `Tool` has 4 things:
1. a **name** (`query_certified_metrics`)
2. a **description** — the model reads this to decide when to use it (it's like the label under a button)
3. a **schema** — what arguments it takes (like a form with required fields)
4. the **Python function** that actually runs

**Governance** = "the agent may only see data the data team has certified."
The database has two floors:
- `raw_*` tables: the basement. Messy. Contains fake test sales (500 earbuds sold in a test!) and a promo spike.
- `cm_*` views: the shop floor. Cleaned and validated. ("cm" = certified metrics.)

Agents can only go to the shop floor. Three locks on the basement door:
1. The database is opened **read-only** → nobody can delete or modify anything.
2. SQLite's **authorizer**: a little function SQLite calls before every read, asking "may I?". We say no for `raw_` tables, unless the read happens inside a certified view.
3. A **text check**: if the query even mentions `raw_`, refuse.

Why lock #3? Because I found a hole in lock #2 (a `JOIN ... USING` trick). That's in the README as "what failed" — the job offer literally asks you to *document what fails*. Mention it.

**The Board** is the shared whiteboard between agents. The Analyst can only *write proposals*. The Reviewer can only *write verdicts*. Neither has an "execute" button.

---

## 4. The two agents → `pilot/team.py`

They are the **same loop**, just with:
- different instructions (the system prompt),
- different buttons (`submit_proposal` vs `record_verdict`).

That's all "multi-agent" means here: several loops, each with its own role, passing work through shared state.

The reorder rule, without math scaring you:
> "Order enough to last until the truck arrives, plus two extra weeks, minus what's already on the shelf."
> = daily sales × (delivery days + 14) − stock on hand

Example: oat milk in Paris sells ~27/day, truck takes 4 days, 30 on the shelf.
27 × 18 = 486, minus 30 → about 456 (with the exact 26.89 per day it gives 455).

If the Reviewer forgets a proposal, the code marks it **NOT REVIEWED** so silence never looks like approval.

---

## 5. Human in the loop → `pilot/human_gate.py`

`ask_human()` shows each proposal + the Reviewer's opinion and waits for `a` / `e` / `r`.
`execute()` writes **only approved** lines to CSV files (pretend it's sending orders to the ERP), and writes **every** decision (approved or rejected, who said what, when) to `audit_log.jsonl`. That log is what a retail Data Direction wants for trust and compliance.

---

## 6. Tests → `tests/test_pipeline.py`

Testing with a real LLM is slow, costs money, and gives different answers each time.
So `ScriptedLLM` is an **actor reading a script**: it returns pre-written answers. That lets us check the *plumbing* is right every time, for free: locks hold, loop works, errors don't crash, rejected things never get executed.

---

## 7. Interview cheat-sheet

**"Why two agents and not one?"**
Separation of duties. LLMs make confident mistakes; a second agent that re-derives the numbers with its own queries catches them. Same reason banks have a maker and a checker.

**"How do you make sure the agent doesn't do something dangerous?"**
It has no dangerous buttons. Data access is read-only and restricted to certified views in code. The only path to action goes through a human, then a dumb executor.

**"Why didn't you use a framework?"**
To understand every step. The concepts (tools, loop, shared state, HITL, step limits) are exactly what Agent Studio / ADK / LangGraph package. Rebuilding it in Agent Studio is my next step.

**"How would you go to production on Google Cloud?"**
Replace SQLite with BigQuery and the `cm_*` views with authorized views; add an evaluation set with planted problems to measure how many the agents find and how often the Reviewer catches errors.

**"Can you trust the LLM's numbers?"**
No. In a live run the Reviewer wrote "455" in its explanation but submitted 472. So the AI only decides *whether* to reorder; a plain Python tool computes *how much*, and the human sees the code's number with a warning whenever an AI number differs.

**"What failed?"**
The authorizer bypass with `JOIN ... USING`, and `COUNT(*)` being wrongly blocked. Both fixed, both covered by tests.
