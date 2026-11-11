"""Which inference-API id does Vibe need for Mistral Large 4?

Vibe's built-in catalogue offers the id `mistral-large-4-0-3`, but the
inference API answers 400 `Invalid model` for it, so the agent silently falls
back to another model. The API publishes `mistral-large-4` and
`mistral-large-4-0`; this probe writes a config per candidate and reports which
one actually answers.

    python tests/probe_large4_ids.py
"""
import os
import shutil
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from vibe_mcp import DEFAULT_EXE, VibeACP  # noqa: E402

VIBE_HOME = os.environ.get("VIBE_HOME") or os.path.join(os.path.expanduser("~"), ".vibe")
CONFIG = os.path.join(VIBE_HOME, "config.toml")

CANDIDATES = ["mistral-large-4", "mistral-large-4-0", "mistral-large-latest", "mistral-large-2512"]


def config_for(candidate):
    return f'''theme = "auto"

allowed_models = ["mistral-large-*"]
active_model = "{candidate}"

[[models]]
name = "{candidate}"
provider = "mistral"
alias = "{candidate}"
thinking = "high"
thinking_levels = ["low", "medium", "high"]
max_context_length = 1048576
supports_images = true
'''


def try_candidate(candidate):
    with open(CONFIG, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(config_for(candidate))
    agent = VibeACP(DEFAULT_EXE)
    try:
        agent.ensure_session(os.getcwd())
        models = agent.option_values("model")
        if candidate not in models:
            return "not offered", models
        result = agent.prompt(
            "Reply with exactly: LARGE4-OK",
            cwd=os.getcwd(),
            model=candidate,
            thinking="high",
            mode=None,
            timeout=240,
        )
        answer = (result.get("_rendered") or "").strip()
        return f"ok ({answer[:20]})", models
    except Exception as exc:  # noqa: BLE001
        message = str(exc)
        if "Invalid model" in message:
            return "API says Invalid model", []
        return f"failed: {message[:140]}", []
    finally:
        agent.close()
        time.sleep(0.5)


def main():
    if not os.path.exists(CONFIG):
        print(f"no config at {CONFIG}")
        return 1
    backup = CONFIG + ".ids-backup"
    shutil.copy2(CONFIG, backup)
    print(f"config backed up to {backup}\n")
    working = []
    try:
        for candidate in CANDIDATES:
            state, models = try_candidate(candidate)
            mark = "ok  " if state.startswith("ok") else "FAIL"
            print(f"{mark} {candidate:24} {state}")
            if state.startswith("ok"):
                working.append(candidate)
    finally:
        shutil.copy2(backup, CONFIG)
        os.remove(backup)
        print(f"\nconfig restored from {backup}")
    print("\nids the API accepts:", working or "none")
    return 0 if working else 1


if __name__ == "__main__":
    sys.exit(main())