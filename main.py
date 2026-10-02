"""
Entry point.   python main.py

Flow:  Analyst agent ──proposals──▶ Reviewer agent ──verdicts──▶ Human ──approved only──▶ outbox/
"""
import os
import sys
from pathlib import Path

from pilot.agent import GeminiLLM
from pilot.human_gate import ask_human, execute
from pilot.team import run_team
from pilot.tools import DB_PATH


def main():
    if not os.getenv("GEMINI_API_KEY"):
        sys.exit("Set GEMINI_API_KEY first (free key: https://aistudio.google.com/apikey)")
    if not DB_PATH.exists():
        import data.seed as seed
        seed.build()

    llm = GeminiLLM()
    print(f"Model: {llm.model} (fallbacks: {', '.join(llm.models[1:])})\n")
    try:
        board = run_team(llm)
    except RuntimeError as e:
        sys.exit(f"\n{e}")

    if not board.proposals:
        print("No action proposed today.")
        return
    print("\n=== HUMAN VALIDATION — nothing is executed without your approval ===")
    decisions = ask_human(board)
    execute(decisions)


if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).parent))
    main()
