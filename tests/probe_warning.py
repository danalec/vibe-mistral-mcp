"""Where does Vibe emit the 'model not available' notice?

Prints the stderr tail right after the process starts and after each model
selection, so a warning that belongs to the startup default is not confused
with one that belongs to the requested model.
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from vibe_mcp import DEFAULT_EXE, VibeACP  # noqa: E402


def tail_of(agent, since):
    return "\n".join(agent._stderr_tail[since:])


def main():
    agent = VibeACP(DEFAULT_EXE)
    try:
        print("--- starting process ---")
        agent._ensure_process()
        time.sleep(3)
        mark = len(agent._stderr_tail)
        print("stderr at startup:", tail_of(agent, 0)[:400] or "(empty)")

        print("\n--- session/new ---")
        agent.ensure_session(os.getcwd())
        time.sleep(1)
        mark = len(agent._stderr_tail)
        print("stderr after session/new:", tail_of(agent, mark)[:400] or "(empty)")

        for model in agent.option_values("model"):
            before = len(agent._stderr_tail)
            try:
                agent.set_option("model", model)
                time.sleep(1.5)
            except Exception as exc:  # noqa: BLE001
                print(f"  {model}: set_option failed {exc}")
                continue
            delta = tail_of(agent, before)
            print(f"  set model {model:24} -> {delta.strip()[:200] or '(no notice)'}")
    finally:
        agent.close()


if __name__ == "__main__":
    main()
