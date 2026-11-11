"""Is mistral-large-4-0-3 really served, or does Vibe swap it silently?

Vibe reports a model it cannot serve only in its own log, not on the ACP wire,
and the prompt still succeeds with another model. This probe therefore:

1. writes a config that explicitly allows Large 4 (with the thinking levels the
   inference API documents for it),
2. starts a fresh agent, selects Large 4 and runs one prompt,
3. reports the ACP-selected model *and* every new line Vibe wrote to its log.

    python tests/probe_large4.py
"""
import os
import re
import shutil
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from vibe_mcp import DEFAULT_EXE, VibeACP  # noqa: E402

VIBE_HOME = os.environ.get("VIBE_HOME") or os.path.join(os.path.expanduser("~"), ".vibe")
CONFIG = os.path.join(VIBE_HOME, "config.toml")
LOG = os.path.join(VIBE_HOME, "logs", "vibe.log")

CONFIG_TEXT = '''theme = "auto"

allowed_models = ["mistral-large-*", "mistral-medium-*", "*glm-5-3*"]
active_model = "mistral-large-4-0-3"

[[models]]
name = "mistral-large-4-0-3"
provider = "mistral"
alias = "mistral-large-4-0-3"
thinking = "high"
thinking_levels = ["low", "medium", "high"]
max_context_length = 1048576
supports_images = true

[[models]]
name = "mistral-medium-3.5"
provider = "mistral"
alias = "mistral-medium-3.5"
thinking_levels = ["off", "high"]
max_context_length = 262144
supports_images = true
'''

NOISE = re.compile(
    r"Skill name|Failed to parse skill|validation error|String should match|type=string_pattern|"
    r"skill at |skills/",
    re.I,
)


def log_size():
    try:
        return os.path.getsize(LOG)
    except OSError:
        return 0


def new_log_lines(offset):
    if not os.path.exists(LOG):
        return []
    with open(LOG, "r", encoding="utf-8", errors="replace") as handle:
        handle.seek(offset)
        return [line.rstrip() for line in handle if line.strip() and not NOISE.search(line)]


def main():
    if not os.path.exists(CONFIG):
        print(f"no config at {CONFIG}")
        return 1
    backup = CONFIG + ".probe-backup"
    shutil.copy2(CONFIG, backup)
    print(f"config backed up to {backup}")
    with open(CONFIG, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(CONFIG_TEXT)

    agent = VibeACP(DEFAULT_EXE)
    try:
        offset = log_size()
        agent.ensure_session(os.getcwd())
        models = agent.option_values("model")
        print(f"models offered: {models}")
        if "mistral-large-4-0-3" not in models:
            print("FAIL: Large 4 is not offered at all with an explicit allowance")
            return 1

        result = agent.prompt(
            "Reply with exactly: LARGE4-OK",
            cwd=os.getcwd(),
            model="mistral-large-4-0-3",
            thinking="high",
            mode=None,
            timeout=240,
        )
        print("answer          :", (result.get("_rendered") or "").strip()[:60])
        print("selected model  :", result.get("_model"))
        print("selected thinking:", result.get("_thinking"))
        print("notes           :", result.get("_notes"))
        time.sleep(1.5)
        lines = new_log_lines(offset)
        print(f"\nvibe log ({len(lines)} new lines):")
        for line in lines[-12:]:
            print("  ", line[:220])
        warnings = [line for line in lines if "not available" in line]
        print()
        if warnings:
            print("VERDICT: Vibe swapped the model silently — Large 4 was not served")
            return 1
        print("VERDICT: no swap notice; Large 4 was served")
        return 0
    finally:
        agent.close()
        shutil.copy2(backup, CONFIG)
        os.remove(backup)
        print(f"config restored from {backup}")


if __name__ == "__main__":
    sys.exit(main())