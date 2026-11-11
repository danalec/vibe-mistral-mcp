"""Does the bridge adapt a thinking level the selected model cannot take?

Mistral Medium 3.5 accepts only 'off' and 'high'; asking for 'low' fails the
prompt outright. The bridge must substitute the model's own level and say so.

    python tests/probe_clamp.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from vibe_mcp import DEFAULT_EXE, VibeACP  # noqa: E402


def main():
    agent = VibeACP(DEFAULT_EXE)
    try:
        agent.ensure_session(os.getcwd())
        model = agent.option_current("model")
        levels = agent.option_values("thinking")
        print(f"active model: {model}")
        print(f"its levels : {levels}")
        unsupported = next((lvl for lvl in ("low", "medium", "xhigh", "max") if lvl not in levels), None)
        if not unsupported:
            print("every level is supported; nothing to clamp")
            return
        print(f"\nasking for {unsupported!r} (not offered by this model)...")
        result = agent.prompt(
            "Reply with exactly: CLAMP-OK",
            cwd=os.getcwd(),
            model=None,
            thinking=unsupported,
            mode=None,
            timeout=180,
        )
        print("answer :", (result.get("_rendered") or "").strip()[:60])
        print("model  :", result.get("_model"))
        print("thinking:", result.get("_thinking"))
        print("notes  :", result.get("_notes"))
        if not result.get("_notes"):
            print("FAIL: the substitution was not reported")
            return 1
        if "CLAMP-OK" not in (result.get("_rendered") or ""):
            print("FAIL: the prompt did not succeed")
            return 1
        print("\nok: the level was clamped and reported")
    finally:
        agent.close()


if __name__ == "__main__":
    sys.exit(main())
