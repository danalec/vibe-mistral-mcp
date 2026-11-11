"""Which thinking levels actually work, per model?

Vibe advertises the levels it believes a model supports, but the inference API
has the last word. For every advertised model and level, this runs one prompt
and reports whether the API accepted it.

    python tests/probe_thinking.py
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from vibe_mcp import DEFAULT_EXE, VibeACP  # noqa: E402


def probe(agent, model, thinking):
    started = time.time()
    try:
        result = agent.prompt(
            "Reply with exactly: THINK-OK",
            cwd=os.getcwd(),
            model=model,
            thinking=thinking,
            mode=None,
            timeout=150,
        )
        answer = (result.get("_rendered") or "").strip()
        return True, answer[:30], round(time.time() - started, 1)
    except Exception as exc:  # noqa: BLE001
        return False, str(exc)[:200], round(time.time() - started, 1)


def main():
    agent = VibeACP(DEFAULT_EXE)
    try:
        agent.ensure_session(os.getcwd())
        models = agent.option_values("model")
        levels = agent.option_values("thinking")
        print(f"models: {models}")
        print(f"levels: {levels}\n")
        working = {}
        for model in models:
            working[model] = []
            for level in levels:
                ok, detail, elapsed = probe(agent, model, level)
                mark = "ok  " if ok else "FAIL"
                print(f"{mark} {model:24} thinking={str(level):8} {elapsed:>6}s  {detail}")
                if ok:
                    working[model].append(level)
        print("\nworking levels per model:")
        for model, ok_levels in working.items():
            print(f"  {model:24} {ok_levels}")
    finally:
        agent.close()


if __name__ == "__main__":
    main()
