"""
Tools = the agents' hands. An LLM can only *ask* to call these; our Python code decides.

Governance rules enforced here (not in the prompt — prompts can be ignored, code can't):
  1. The database is opened READ-ONLY.
  2. Only SELECT / WITH queries are accepted, one statement at a time.
  3. SQLite's authorizer blocks any read of raw_* tables, unless the read happens
     *inside* a certified cm_* view. So agents can only see certified metrics.
  4. Results are capped so a bad query can't flood the model's context.
"""
import json
import math
import re
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

DB_PATH = Path(__file__).resolve().parent.parent / "data" / "retail.db"
MAX_ROWS = 50


@dataclass
class Tool:
    """One tool: a name, a description the LLM reads, a JSON schema for its arguments, and the Python function."""
    name: str
    description: str
    parameters: dict
    fn: Callable[..., Any]


# ---------------------------------------------------------------- certified data access
def _authorizer(action, arg1, arg2, db_name, trigger_or_view):
    if action == sqlite3.SQLITE_READ:
        table, column = arg1 or "", arg2 or ""
        # column == "" is SQLite counting rows for COUNT(*) on a flattened view: no values are read.
        if table.startswith("raw_") and column and not (trigger_or_view or "").startswith("cm_"):
            return sqlite3.SQLITE_DENY          # raw table read directly -> blocked
    if action in (sqlite3.SQLITE_SELECT, sqlite3.SQLITE_READ, sqlite3.SQLITE_FUNCTION):
        return sqlite3.SQLITE_OK
    return sqlite3.SQLITE_DENY                  # INSERT, UPDATE, DROP, ATTACH, PRAGMA... all blocked


def _connect():
    con = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    con.set_authorizer(_authorizer)
    return con


def list_certified_metrics() -> dict:
    """Return every certified view, its columns and its business definition."""
    con = _connect()
    con.set_authorizer(None)  # reading the catalog itself is safe
    out = {}
    for view, desc in con.execute("SELECT view_name, description FROM cm_metric_dictionary"):
        cols = [r[1] for r in con.execute(f"PRAGMA table_info({view})")]
        out[view] = {"description": desc, "columns": cols}
    con.close()
    return out


def query_certified_metrics(sql: str) -> dict:
    """Run one read-only SQL query against the certified layer."""
    clean = sql.strip().rstrip(";")
    if ";" in clean:
        return {"error": "Only one SQL statement per call."}
    if not clean.lower().startswith(("select", "with")):
        return {"error": "Only SELECT/WITH queries are allowed."}
    # Belt and braces: the authorizer misses some edge cases (e.g. a JOIN ... USING on a raw table),
    # so we also refuse any query that even names a raw table.
    if re.search(r"\braw_\w+", clean, re.IGNORECASE):
        return {"error": "Raw tables are off-limits — you may only query certified cm_* views."}
    try:
        con = _connect()
        cur = con.execute(clean)
        cols = [d[0] for d in cur.description]
        rows = cur.fetchmany(MAX_ROWS + 1)
        con.close()
    except sqlite3.DatabaseError as e:
        msg = str(e)
        if "not authorized" in msg or "prohibited" in msg:
            msg += " — you may only query certified cm_* views."
        return {"error": msg}
    return {
        "columns": cols,
        "rows": [list(r) for r in rows[:MAX_ROWS]],
        "truncated": len(rows) > MAX_ROWS,
    }


REORDER_BUFFER_DAYS = 14


def reorder_quantity(store_id: str, product_id: str) -> dict:
    """Deterministic reorder rule, computed by code (LLMs make arithmetic mistakes).
    Order enough to last until the delivery arrives + 14 days, minus what's on the shelf."""
    con = _connect()
    row = con.execute("SELECT on_hand, avg_daily_units_28d, lead_time_days FROM cm_stock_cover "
                      "WHERE store_id = ? AND product_id = ?", (store_id, product_id)).fetchone()
    con.close()
    if row is None or row[1] is None:
        return {"error": f"No stock data for {product_id} @ {store_id}."}
    on_hand, pace, lead = row
    qty = max(0, math.ceil(pace * (lead + REORDER_BUFFER_DAYS) - on_hand))
    return {"quantity": qty, "formula": f"ceil({pace} * ({lead} + {REORDER_BUFFER_DAYS}) - {on_hand})"}


SQL_TOOLS = [
    Tool(
        name="list_certified_metrics",
        description="List the certified metric views you may query, with their columns and business definitions. Call this first.",
        parameters={"type": "object", "properties": {}},
        fn=list_certified_metrics,
    ),
    Tool(
        name="query_certified_metrics",
        description="Run ONE read-only SQLite SELECT query on the certified cm_* views. Returns columns and up to 50 rows.",
        parameters={
            "type": "object",
            "properties": {"sql": {"type": "string", "description": "A single SELECT query."}},
            "required": ["sql"],
        },
        fn=query_certified_metrics,
    ),
    Tool(
        name="reorder_quantity",
        description="Compute the official reorder quantity for one store/product. ALWAYS use this; never do the arithmetic yourself.",
        parameters={
            "type": "object",
            "properties": {"store_id": {"type": "string"}, "product_id": {"type": "string"}},
            "required": ["store_id", "product_id"],
        },
        fn=reorder_quantity,
    ),
]


# ---------------------------------------------------------------- the shared board
@dataclass
class Board:
    """Where agents leave work for each other (and for the human). No agent can *execute* anything."""
    proposals: list = field(default_factory=list)
    verdicts: dict = field(default_factory=dict)

    # -- Analyst's tool
    def submit_proposal(self, action_type: str, store_id: str, product_id: str,
                        value: float, rationale: str, evidence: str) -> dict:
        if action_type not in ("reorder", "markdown"):
            return {"error": "action_type must be 'reorder' or 'markdown'."}
        if action_type == "markdown" and not (5 <= value <= 50):
            return {"error": "markdown value is a discount percent between 5 and 50."}
        if action_type == "reorder" and not (1 <= value <= 5000):
            return {"error": "reorder value is a quantity between 1 and 5000."}
        pid = f"P{len(self.proposals) + 1}"
        computed = None
        if action_type == "reorder":
            check = reorder_quantity(store_id, product_id)
            if "error" in check:
                return check
            computed = check["quantity"]
        self.proposals.append(dict(id=pid, action_type=action_type, store_id=store_id,
                                   product_id=product_id, value=value, computed_value=computed,
                                   rationale=rationale, evidence=evidence))
        return {"ok": True, "proposal_id": pid}

    # -- Reviewer's tool
    def record_verdict(self, proposal_id: str, verdict: str, reason: str,
                       corrected_value: float | None = None) -> dict:
        if proposal_id not in {p["id"] for p in self.proposals}:
            return {"error": f"Unknown proposal {proposal_id}."}
        if verdict not in ("agree", "disagree", "adjust"):
            return {"error": "verdict must be 'agree', 'disagree' or 'adjust'."}
        if verdict == "adjust" and corrected_value is None:
            return {"error": "'adjust' needs a corrected_value."}
        self.verdicts[proposal_id] = dict(verdict=verdict, reason=reason, corrected_value=corrected_value)
        return {"ok": True}

    def analyst_tools(self):
        return SQL_TOOLS + [Tool(
            name="submit_proposal",
            description=("Submit ONE recommended action for human review. It will NOT be executed by you. "
                         "action_type 'reorder' -> value = units to order. "
                         "action_type 'markdown' -> value = discount percent (5-50)."),
            parameters={
                "type": "object",
                "properties": {
                    "action_type": {"type": "string", "enum": ["reorder", "markdown"]},
                    "store_id": {"type": "string"},
                    "product_id": {"type": "string"},
                    "value": {"type": "number"},
                    "rationale": {"type": "string", "description": "Why, in one or two sentences, with the key numbers."},
                    "evidence": {"type": "string", "description": "The SQL query that proves it."},
                },
                "required": ["action_type", "store_id", "product_id", "value", "rationale", "evidence"],
            },
            fn=self.submit_proposal,
        )]

    def reviewer_tools(self):
        return SQL_TOOLS + [Tool(
            name="record_verdict",
            description=("Record your verdict on one proposal after re-checking it against the data yourself. "
                         "'agree', 'disagree', or 'adjust' (then give corrected_value)."),
            parameters={
                "type": "object",
                "properties": {
                    "proposal_id": {"type": "string"},
                    "verdict": {"type": "string", "enum": ["agree", "disagree", "adjust"]},
                    "reason": {"type": "string"},
                    "corrected_value": {"type": "number"},
                },
                "required": ["proposal_id", "verdict", "reason"],
            },
            fn=self.record_verdict,
        )]


def to_json(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, default=str)
