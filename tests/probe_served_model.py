"""Prove that the configured model is the one that actually answered.

Vibe reports a model it cannot serve only in its own log, and the prompt still
succeeds with a substitute. This probe therefore compares what the agent says it
selected against what it wrote to its log, for the configured model.

    python tests/probe_served_model.py
"""
import os
import re
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from vibe_mcp import DEFAULT_EXE, VibeACP  # noqa: E402

VIBE_HOME = os.environ.get("VIBE_HOME") or os.path.join(os.path.expanduser("~"), ".vibe")
LOG = os.path.join(VIBE_HOME, "logs", "vibe.log")
NOISE = re.compile(r"Skill name|Failed to parse skill|validation error|String should match|type=string_pattern", re.I)


def log_size():
    try:
        return os.path.getsize(LOG)
    except OSError:
        return 0


def new_lines(offset):
    if not os.path.exists(LOG):
        return []
    with open(LOG, "r", encoding="utf-8", errors="replace") as handle:
        handle.seek(offset)
        return [line.rstrip() for line in handle if line.strip() and not NOISE.search(line)]


def main():
    agent = VibeACP(DEFAULT_EXE)
    try:
        offset = log_size()
        agent.ensure_session(os.getcwd())
        selected = agent.option_current("model")
        levels = agent.option_values("thinking")
        print(f"models offered  : {agent.option_values('model')}")
        print(f"active model    : {selected}")
        print(f"its levels      : {levels}")

        result = agent.prompt(
            "Reply with exactly: SERVED-OK",
            cwd=os.getcwd(),
            model=None,
            thinking="high",
            mode=None,
            timeout=240,
        )
        print("answer          :", (result.get("_rendered") or "").strip()[:60])
        print("reported model  :", result.get("_model"))
        time.sleep(1.5)
        lines = new_lines(offset)
        swaps = [line for line in lines if "not available" in line]
        print(f"new log lines   : {len(lines)}")
        for line in lines[-6:]:
            print("   ", line[:200])
        print()
        if swaps:
            print("VERDICT: FAIL — Vibe served another model:")
            for line in swaps[-2:]:
                print("   ", line[:220])
            return 1
        if "SERVED-OK" not in (result.get("_rendered") or ""):
            print("VERDICT: FAIL — no answer")
            return 1
        if result.get("_model") != selected:
            print(f"VERDICT: FAIL — selection changed to {result.get('_model')}")
            return 1
        print(f"VERDICT: ok — {selected} answered, no substitution")
        return 0
    finally:
        agent.close()


if __name__ == "__main__":
    sys.exit(main())