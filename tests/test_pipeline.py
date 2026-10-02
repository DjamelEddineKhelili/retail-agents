"""
Tests that run WITHOUT an API key: a scripted fake LLM plays both agents,
so we can check the plumbing (loop, tools, governance, human gate, audit) deterministically.

    python -m pytest -q
"""
import json

import pytest

import pilot.human_gate as gate
from pilot.agent import Agent, Reply, ToolCall, GeminiLLM
from pilot.team import run_team
from pilot.tools import Board, query_certified_metrics


class ScriptedLLM:
    """Returns pre-written replies in order, and records what it was shown."""
    def __init__(self, replies):
        self.replies = list(replies)
        self.seen = []

    def chat(self, system, history, tools):
        self.seen.append({"system": system, "history": list(history), "tools": [t.name for t in tools]})
        return self.replies.pop(0)


def call(name, **args):
    return Reply(text="", calls=[ToolCall(name, args, id=None)])


# ---------------------------------------------------------------- governance
def test_raw_tables_are_blocked():
    assert "error" in query_certified_metrics("SELECT * FROM raw_sales")
    assert "error" in query_certified_metrics("SELECT s.* FROM cm_daily_sales s JOIN raw_sales r USING(store_id)")
    assert "error" in query_certified_metrics("DELETE FROM cm_daily_sales")
    assert "error" in query_certified_metrics("SELECT 1; SELECT 2")


def test_certified_layer_filters_test_transactions():
    r = query_certified_metrics("SELECT MAX(units) FROM cm_daily_sales WHERE store_id='PAR01' AND product_id='SKU-301'")
    assert r["rows"][0][0] < 50  # the 500-unit POS test transaction is gone


def test_planted_stockout_is_visible():
    r = query_certified_metrics("SELECT store_id, product_id FROM cm_stock_cover WHERE days_of_cover < lead_time_days")
    assert ["PAR01", "SKU-101"] in r["rows"]


# ---------------------------------------------------------------- the loop
def test_agent_loop_feeds_tool_results_back():
    llm = ScriptedLLM([
        call("query_certified_metrics", sql="SELECT COUNT(*) FROM cm_stores"),
        Reply(text="There are 3 stores."),
    ])
    agent = Agent("T", "sys", Board().analyst_tools(), llm, log=lambda *_: None)
    assert agent.run("How many stores?") == "There are 3 stores."
    tool_msg = llm.seen[1]["history"][-1]
    assert tool_msg["role"] == "tool_results" and tool_msg["results"][0]["result"]["rows"] == [[3]]


def test_unknown_tool_and_bad_args_are_reported_not_crashing():
    llm = ScriptedLLM([call("drop_database"), call("query_certified_metrics", query="oops"), Reply(text="ok")])
    agent = Agent("T", "sys", Board().analyst_tools(), llm, log=lambda *_: None)
    agent.run("x")
    assert "Unknown tool" in agent.trace[0]["result"]["error"]
    assert "Bad arguments" in agent.trace[1]["result"]["error"]


def test_step_limit_stops_runaway_agent():
    llm = ScriptedLLM([call("list_certified_metrics")] * 3)
    agent = Agent("T", "sys", Board().analyst_tools(), llm, max_steps=3, log=lambda *_: None)
    assert "step limit" in agent.run("x")


# ---------------------------------------------------------------- full pipeline
def test_two_agents_then_human_then_execution(tmp_path, monkeypatch):
    monkeypatch.setattr(gate, "OUTBOX", tmp_path)
    llm = ScriptedLLM([
        # Analyst
        call("submit_proposal", action_type="reorder", store_id="PAR01", product_id="SKU-101", value=470,
             rationale="1.1 days of cover vs 4-day lead time", evidence="SELECT ..."),
        call("submit_proposal", action_type="markdown", store_id="LIL01", product_id="SKU-403", value=30,
             rationale="229 days of cover on a summer item", evidence="SELECT ..."),
        Reply(text="2 proposals."),
        # Reviewer
        call("record_verdict", proposal_id="P1", verdict="adjust", reason="Arithmetic gives 482", corrected_value=482),
        call("record_verdict", proposal_id="P2", verdict="agree", reason="Confirmed"),
        Reply(text="Reviewed 2."),
    ])
    board = run_team(llm, log=lambda *_: None)
    assert len(board.proposals) == 2 and board.verdicts["P1"]["corrected_value"] == 482

    assert board.proposals[0]["computed_value"] == 455    # code recomputes; AI said 470, reviewer 482
    answers = iter(["a", "r"])  # human approves the reorder (at the code's value), rejects the markdown
    decisions = gate.ask_human(board, ask=lambda _: next(answers), say=lambda *_: None)
    gate.execute(decisions, say=lambda *_: None)

    po = (tmp_path / "purchase_orders.csv").read_text().splitlines()
    assert len(po) == 2 and ",455," in po[1]
    assert not (tmp_path / "markdowns.csv").exists()          # rejected -> never executed
    audit = [json.loads(line) for line in (tmp_path / "audit_log.jsonl").read_text().splitlines()]
    assert [a["human_decision"] for a in audit] == ["approved", "rejected"]


def test_unreviewed_proposal_is_flagged():
    llm = ScriptedLLM([
        call("submit_proposal", action_type="reorder", store_id="LYO01", product_id="SKU-302", value=161,
             rationale="r", evidence="e"),
        Reply(text="1"),
        Reply(text="I reviewed nothing."),
    ])
    board = run_team(llm, log=lambda *_: None)
    assert board.verdicts["P1"]["verdict"] == "no_review"


# ---------------------------------------------------------------- Gemini adapter (offline)
def test_gemini_adapter_builds_valid_request_and_parses_calls(monkeypatch):
    from google.genai import types
    monkeypatch.setenv("GEMINI_API_KEY", "fake")
    llm = GeminiLLM(model="test-model", sleep=lambda s: None)
    captured = {}

    def fake_generate(model, contents, config):
        captured.update(model=model, contents=contents, config=config)
        part = types.Part(function_call=types.FunctionCall(name="list_certified_metrics", args={}))
        return types.GenerateContentResponse(candidates=[types.Candidate(content=types.Content(role="model", parts=[part]))])

    monkeypatch.setattr(llm.client.models, "generate_content", fake_generate)
    reply = llm.chat("sys", [{"role": "user", "text": "hi"}], Board().analyst_tools())
    assert reply.calls[0].name == "list_certified_metrics"
    names = [f.name for f in captured["config"].tools[0].function_declarations]
    assert names == ["list_certified_metrics", "query_certified_metrics", "reorder_quantity", "submit_proposal"]

    # second turn: the model's own message + tool results must be sent back
    history = [{"role": "user", "text": "hi"},
               {"role": "assistant", "raw": reply.raw},
               {"role": "tool_results", "results": [{"id": None, "name": "list_certified_metrics", "result": {"a": 1}}]}]
    llm.chat("sys", history, Board().analyst_tools())
    assert captured["contents"][2].parts[0].function_response.name == "list_certified_metrics"


# ---------------------------------------------------------------- resilience (real 503s seen in the first live run)
def _busy():
    from google.genai import errors
    return errors.ServerError(503, {"error": {"code": 503, "message": "high demand", "status": "UNAVAILABLE"}})


def test_gemini_retries_then_falls_back_to_next_model(monkeypatch):
    from google.genai import types
    monkeypatch.setenv("GEMINI_API_KEY", "fake")
    monkeypatch.delenv("GEMINI_MODEL", raising=False)
    llm = GeminiLLM(model="busy-model", retries=2, sleep=lambda s: None, log=lambda *_: None)
    tried = []

    def fake_generate(model, contents, config):
        tried.append(model)
        if model == "busy-model":
            raise _busy()
        return types.GenerateContentResponse(candidates=[types.Candidate(
            content=types.Content(role="model", parts=[types.Part(text="hello")]))])

    monkeypatch.setattr(llm.client.models, "generate_content", fake_generate)
    reply = llm.chat("sys", [{"role": "user", "text": "hi"}], [])
    assert reply.text == "hello"
    assert tried == ["busy-model", "busy-model", "gemini-3.5-flash-lite"]
    assert llm.model == "gemini-3.5-flash-lite"   # sticks with the model that works


def test_gemini_gives_clear_error_when_everything_is_down(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "fake")
    llm = GeminiLLM(retries=1, sleep=lambda s: None, log=lambda *_: None)
    monkeypatch.setattr(llm.client.models, "generate_content", lambda **_: (_ for _ in ()).throw(_busy()))
    with pytest.raises(RuntimeError, match="No Gemini model available"):
        llm.chat("sys", [{"role": "user", "text": "hi"}], [])


def test_unknown_model_and_daily_quota_skip_straight_to_next_model(monkeypatch):
    from google.genai import errors, types
    monkeypatch.setenv("GEMINI_API_KEY", "fake")
    llm = GeminiLLM(model="old-model", sleep=lambda s: None, log=lambda *_: None)
    llm.models = ["old-model", "daily-capped", "good-lite"]
    tried = []

    def fake_generate(model, contents, config):
        tried.append(model)
        if model == "old-model":
            raise errors.ClientError(404, {"error": {"code": 404, "message": "no longer available", "status": "NOT_FOUND"}})
        if model == "daily-capped":
            raise errors.ClientError(429, {"error": {"code": 429, "status": "RESOURCE_EXHAUSTED", "message": "quota",
                                     "details": [{"violations": [{"quotaId": "GenerateRequestsPerDayPerProjectPerModel-FreeTier"}]}]}})
        return types.GenerateContentResponse(candidates=[types.Candidate(
            content=types.Content(role="model", parts=[types.Part(text="ok")]))])

    monkeypatch.setattr(llm.client.models, "generate_content", fake_generate)
    assert llm.chat("sys", [{"role": "user", "text": "hi"}], []).text == "ok"
    assert tried == ["old-model", "daily-capped", "good-lite"]   # no pointless retries on either


def test_pacing_spaces_out_calls(monkeypatch):
    from google.genai import types
    monkeypatch.setenv("GEMINI_API_KEY", "fake")
    monkeypatch.delenv("GEMINI_RPM", raising=False)
    slept = []
    llm = GeminiLLM(model="gemini-3.5-flash-lite", sleep=slept.append, clock=lambda: 100.0, log=lambda *_: None)
    ok = types.GenerateContentResponse(candidates=[types.Candidate(
        content=types.Content(role="model", parts=[types.Part(text="ok")]))])
    monkeypatch.setattr(llm.client.models, "generate_content", lambda **_: ok)
    llm.chat("s", [{"role": "user", "text": "a"}], [])
    llm.chat("s", [{"role": "user", "text": "b"}], [])
    assert slept and 4 < slept[0] < 5   # 15 req/min -> ~4.4s between calls


def test_reorder_quantity_is_computed_by_code():
    from pilot.tools import reorder_quantity
    assert reorder_quantity("PAR01", "SKU-101")["quantity"] == 455   # ceil(26.89 * 18 - 30)
