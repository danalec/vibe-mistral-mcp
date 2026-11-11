import os
import sys

sys.path.insert(0, ".")
os.environ["VIBE_ACP_EXE"] = ""

from vibe_mcp import resolve_acp_command, resolve_acp_exe  # noqa: E402

print("bare command ->", resolve_acp_command("python3"))
print("command+args ->", resolve_acp_command("python3 tests/fake_vibe_acp.py"))
print("exe          ->", resolve_acp_exe("python3 tests/fake_vibe_acp.py"))
try:
    resolve_acp_command("definitely-not-installed")
except Exception as exc:  # noqa: BLE001
    print("missing exe  ->", type(exc).__name__, str(exc)[:100])