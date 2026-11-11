"""Probe which Vibe models this account can actually run.

For every model the ACP session advertises, run one short prompt and look for
Vibe's fallback notice in its stderr. A model that is not available is served
by another one, and the answer says so.

    python tests/probe_models.py
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from vibe_mcp import DEFAULT_EXE, DEFAULT_MODEL, VibeACP  # noqa: E402


def probe(agent, model):
    agent._stderr_tail.clear()
    before = len(agent._stderr_tail)
    started = time.time()
    try:
        result = agent.prompt(
            "Reply with exactly: PROBE-OK",
            cwd=os.getcwd(),
            model=model,
            thinking="high",
            mode=None,
            timeout=180,
        )
    except Exception as exc:  # noqa: BLE001
        return {"model": model, "ok": False, "error": str(exc)[:220], "elapsed": round(time.time() - started, 1)}
    tail = "\n".join(agent._stderr_tail[before:])
    fallback = ""
    for line in tail.splitlines():
        if "not available" in line:
            fallback = line.split("running", 1)[-1].strip().strip(".'")
    return {
        "model": model,
        "ok": True,
        "answer": (result.get("_rendered") or "")[:40],
        "fallback_to": fallback,
        "elapsed": round(time.time() - started, 1),
    }


def main():
    agent = VibeACP(DEFAULT_EXE)
    try:
        agent.ensure_session(os.getcwd())
        models = agent.option_values("model")
        thinking = agent.option_values("thinking")
        print(f"advertised models : {models}")
        print(f"advertised thinking: {thinking}")
        print(f"bridge default    : {DEFAULT_MODEL}")
        print()
        for model in models:
            row = probe(agent, model)
            if row.get("ok"):
                state = "FALLBACK -> " + row["fallback_to"] if row.get("fallback_to") else "available"
                print(f"{row['model']:26} ok   {row['elapsed']:>6}s  {state}   {row['answer']}")
            else:
                print(f"{row['model']:26} FAIL {row['elapsed']:>6}s  {row['error']}")
    finally:
        agent.close()


if __name__ == "__main__":
    main()
