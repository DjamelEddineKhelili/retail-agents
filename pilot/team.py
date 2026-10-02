"""
The two agents and how they cooperate.

  Analyst   -> scans the certified metrics, finds stock problems, SUBMITS proposals.
  Reviewer  -> independently re-queries the data for each proposal and AGREES / DISAGREES / ADJUSTS.
  Human     -> sees both opinions and makes the final call (see human_gate.py).

Neither agent can execute an action. They only write to the shared Board.
"""
from .agent import Agent
from .tools import Board, to_json

ANALYST_PROMPT = """You are the Stock Analyst agent for a French retail chain (3 stores).
Goal: find products that need action THIS WEEK and submit proposals.

Rules:
- Start by calling list_certified_metrics. Only use certified cm_* views; never guess numbers.
- Stockout risk: days_of_cover < lead_time_days. Propose a 'reorder'.
  Get the quantity from the reorder_quantity tool. Never compute it yourself.
- Overstock: days_of_cover > 90. Propose a 'markdown' (discount %, 10 to 40) and explain why.
- Do not treat a single-day spike as a trend; cm_stock_cover already removes spikes.
- Submit one proposal per (store, product). Put the exact SQL you used in 'evidence'.
- Be economical: every turn costs an API call. Get all the rows you need in ONE query, and call
  submit_proposal for ALL proposals in the SAME turn (you can call several tools at once).
- When finished, reply with a short summary: how many proposals and why. No tool call in that last message."""

REVIEWER_PROMPT = """You are the Reviewer agent. You are a skeptical second pair of eyes.
You receive proposals written by another agent. For EACH proposal:
- Re-check it yourself with your own SQL on the certified cm_* views (do not trust the evidence text blindly).
- For reorders, call reorder_quantity yourself and compare. Never do arithmetic in your head.
- Check business sense: is the problem real? Is the quantity or discount reasonable? Any data-quality doubt?
- Call record_verdict: 'agree', 'disagree', or 'adjust' with corrected_value.
Every proposal must get exactly one verdict. When done, reply with a short summary and no tool call.
Be economical: every turn costs an API call. Fetch the data for ALL proposals in ONE query
(e.g. WHERE (store_id, product_id) IN (...)), then call record_verdict for ALL proposals in the SAME turn."""


def run_team(llm, log=print) -> Board:
    board = Board()

    analyst = Agent("Analyst", ANALYST_PROMPT, board.analyst_tools(), llm, log=log)
    analyst_summary = analyst.run("Run today's stock check across all stores and submit your proposals.")
    log(f"\nAnalyst summary: {analyst_summary}\n")

    if not board.proposals:
        return board

    reviewer = Agent("Reviewer", REVIEWER_PROMPT, board.reviewer_tools(), llm, max_steps=25, log=log)
    reviewer_summary = reviewer.run("Review these proposals:\n" + to_json(board.proposals))
    log(f"\nReviewer summary: {reviewer_summary}\n")

    # A missing verdict is itself a signal for the human.
    for p in board.proposals:
        board.verdicts.setdefault(p["id"], {"verdict": "no_review", "reason": "Reviewer did not check this one.",
                                            "corrected_value": None})
    return board
