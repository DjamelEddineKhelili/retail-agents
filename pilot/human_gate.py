"""
Human-in-the-loop: nothing happens until a person says yes.

The executor is plain Python, not an LLM. Approved actions are written to outbox/
(a stand-in for "send the purchase order to the ERP"). Every decision, approved or not,
goes to an append-only audit log.
"""
import csv
import json
from datetime import datetime
from pathlib import Path

OUTBOX = Path(__file__).resolve().parent.parent / "outbox"


def show(p: dict, v: dict) -> str:
    unit = "units" if p["action_type"] == "reorder" else "% off"
    badge = {"agree": "✅ agrees", "disagree": "❌ disagrees", "adjust": "✏️  adjusts",
             "no_review": "⚠️  NOT REVIEWED"}[v["verdict"]]
    lines = [
        "─" * 70,
        f"{p['id']}  {p['action_type'].upper()}  {p['product_id']} @ {p['store_id']}  →  {p['value']:g} {unit}",
        f"  Analyst : {p['rationale']}",
        f"  Reviewer: {badge} — {v['reason']}",
    ]
    if v["verdict"] == "adjust":
        lines.append(f"            suggests {v['corrected_value']:g} {unit}")
    if p.get("computed_value") is not None:
        lines.append(f"  🧮 Code : {p['computed_value']} units (official formula, computed by code, not by AI)")
        ai_numbers = {p["value"]} | ({v["corrected_value"]} if v.get("corrected_value") is not None else set())
        if any(n != p["computed_value"] for n in ai_numbers):
            lines.append("  ⚠️  An AI number differs from the formula. Default below uses the code's value.")
    return "\n".join(lines)


def ask_human(board, ask=input, say=print) -> list[dict]:
    """Walk the human through every proposal. Returns the list of decisions."""
    decisions = []
    for p in board.proposals:
        v = board.verdicts[p["id"]]
        say(show(p, v))
        if p.get("computed_value") is not None:
            default_value = p["computed_value"]          # trust the code over any AI arithmetic
        elif v["verdict"] == "adjust":
            default_value = v["corrected_value"]
        else:
            default_value = p["value"]
        while True:
            ans = ask(f"  [a]pprove {default_value:g} / [e]dit value / [r]eject ? ").strip().lower()
            if ans in ("a", "r"):
                break
            if ans == "e":
                try:
                    default_value = float(ask("  New value: "))
                    continue
                except ValueError:
                    say("  Not a number.")
                    continue
        decisions.append({
            "proposal": p,
            "reviewer": v,
            "human_decision": "approved" if ans == "a" else "rejected",
            "final_value": (_clean(default_value, p["action_type"]) if ans == "a" else None),
            "decided_at": datetime.now().isoformat(timespec="seconds"),
        })
    return decisions


def execute(decisions: list[dict], say=print) -> None:
    """Only approved decisions become actions. Everything is audited."""
    OUTBOX.mkdir(exist_ok=True)
    with open(OUTBOX / "audit_log.jsonl", "a", encoding="utf-8") as log:
        for d in decisions:
            log.write(json.dumps(d, ensure_ascii=False) + "\n")

    approved = [d for d in decisions if d["human_decision"] == "approved"]
    for kind, filename, header in (("reorder", "purchase_orders.csv", "quantity"),
                                   ("markdown", "markdowns.csv", "discount_pct")):
        rows = [d for d in approved if d["proposal"]["action_type"] == kind]
        if not rows:
            continue
        path = OUTBOX / filename
        new = not path.exists()
        with open(path, "a", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            if new:
                w.writerow(["created_at", "store_id", "product_id", header, "proposal_id"])
            for d in rows:
                p = d["proposal"]
                w.writerow([d["decided_at"], p["store_id"], p["product_id"], d["final_value"], p["id"]])
        say(f"→ wrote {len(rows)} line(s) to outbox/{filename}")
    say(f"→ {len(approved)}/{len(decisions)} approved. Full trail in outbox/audit_log.jsonl")


def _clean(value, action_type):
    """Order quantities are whole units: 455, not 455.0."""
    return int(round(value)) if action_type == "reorder" else value
