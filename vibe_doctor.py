#!/usr/bin/env python
"""End-to-end health check for the Mistral Vibe bridge.

Answers one question: is Vibe alive and working right now?

It does not trust the bridge's own bookkeeping. It starts a fresh ``vibe-acp``,
negotiates ACP, opens a session, and runs one real prompt end to end, then
checks the Mistral Studio half and the remaining plan budget.

    python vibe_doctor.py            # full check, one real prompt
    python vibe_doctor.py --quick    # skip the prompt (no plan tokens spent)
    python vibe_doctor.py --json     # machine-readable output

Exit code 0 means every check passed.
"""
import argparse
import json
import os
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import studio  # noqa: E402
from vibe_mcp import DEFAULT_EXE, DEFAULT_MODEL, DEFAULT_THINKING, VibeACP  # noqa: E402

PROMPT = "Reply with exactly: VIBE-OK"

class Report:
    def __init__(self):
        self.checks = []

    def add(self, label, ok, detail=""):
        self.checks.append({"check": label, "ok": bool(ok), "detail": str(detail)})
        return ok

    @property
    def ok(self):
        return all(c["ok"] for c in self.checks)

    def render(self):
        lines = []
        for check in self.checks:
            mark = "ok  " if check["ok"] else "FAIL"
            lines.append(f"{mark} {check['check']}" + (f"  :: {check['detail']}" if check["detail"] else ""))
        lines.append("")
        lines.append("vibe is healthy" if self.ok else "vibe has a problem (see the FAIL lines above)")
        return "\n".join(lines)


def check_executable(report, exe):
    try:
        from vibe_mcp import resolve_acp_command

        argv = resolve_acp_command(exe)
    except Exception as exc:  # noqa: BLE001
        report.add("vibe-acp executable", False, str(exc)[:200])
        return False
    version = ""
    try:
        out = subprocess.run(argv + ["--version"], capture_output=True, text=True, timeout=30)
        version = (out.stdout or out.stderr).strip().splitlines()[0] if (out.stdout or out.stderr) else ""
    except Exception as exc:  # noqa: BLE001
        version = f"(version check failed: {exc})"
    report.add("vibe-acp executable", True, f"{' '.join(argv)} — {version}")
    return True


def check_agent(report, agent, timeout):
    started = time.time()
    try:
        agent._ensure_process()
        init = agent.request(
            "initialize",
            {
                "protocolVersion": 1,
                "clientCapabilities": {"fs": {"readTextFile": False, "writeTextFile": False}, "terminal": False},
            },
            60,
        )
    except Exception as exc:  # noqa: BLE001
        report.add("ACP handshake", False, str(exc)[:200])
        return False
    info = init.get("agentInfo") or {}
    name = info.get("name") or "(unnamed)"
    version = info.get("version") or "?"
    report.add("ACP handshake", True, f"{name} {version} in {round(time.time() - started, 1)}s")
    return True


def check_session(report, agent, timeout):
    try:
        agent.ensure_session(os.getcwd())
    except Exception as exc:  # noqa: BLE001
        report.add("ACP session", False, str(exc)[:200])
        return False
    models = agent.option_values("model")
    thinking = agent.option_values("thinking")
    report.add(
        "ACP session",
        True,
        f"cwd={agent.session_cwd} model={agent.option_current('model')} "
        f"models={models or '?'} thinking={thinking or '?'}",
    )
    return True


def check_prompt(report, agent, timeout):
    started = time.time()
    try:
        result = agent.prompt(
            PROMPT,
            cwd=os.getcwd(),
            model=DEFAULT_MODEL,
            thinking=DEFAULT_THINKING,
            mode=None,
            timeout=timeout,
        )
    except Exception as exc:  # noqa: BLE001
        report.add("live prompt", False, str(exc)[:250])
        return False
    text = (result.get("_rendered") or "").strip()
    elapsed = round(time.time() - started, 1)
    if "VIBE-OK" not in text:
        report.add("live prompt", False, f"unexpected answer: {text[:120]!r}")
        return False
    detail = f"VIBE-OK in {elapsed}s (model {result.get('_model')}, thinking {result.get('_thinking')})"
    for note in result.get("_notes") or []:
        detail += f"; {note}"
    report.add("live prompt", True, detail)
    return True


def check_studio(report):
    try:
        limits = studio.client().call("get_usage_limits", {})
    except Exception as exc:  # noqa: BLE001
        report.add("Mistral Studio (api.mistral.ai/mcp)", False, str(exc)[:200])
        return False
    text = ""
    for block in limits.get("content") or []:
        if block.get("type") == "text":
            text = block.get("text") or ""
    detail = "reachable"
    try:
        parsed = json.loads(text)
        budgets = ", ".join(
            f"{b.get('product')} {b.get('remaining')}/{b.get('amount')} {b.get('currency', '')}".strip()
            for b in parsed.get("budgets", [])
        )
        if budgets:
            detail = budgets
    except Exception:  # noqa: BLE001
        pass
    report.add("Mistral Studio (api.mistral.ai/mcp)", True, detail)
    return True


def main():
    parser = argparse.ArgumentParser(description="Check that the Mistral Vibe bridge works end to end")
    parser.add_argument("--quick", action="store_true", help="skip the live prompt (spends no plan tokens)")
    parser.add_argument("--json", action="store_true", help="print JSON instead of a report")
    parser.add_argument("--exe", default=DEFAULT_EXE, help="path to vibe-acp")
    parser.add_argument("--timeout", type=float, default=180.0, help="seconds for the live prompt")
    args = parser.parse_args()

    report = Report()
    agent = VibeACP(args.exe)
    try:
        if check_executable(report, args.exe):
            if check_agent(report, agent, args.timeout):
                check_session(report, agent, args.timeout)
                if not args.quick:
                    check_prompt(report, agent, args.timeout)
        check_studio(report)
    finally:
        agent.close()

    if args.json:
        print(json.dumps({"ok": report.ok, "checks": report.checks}, indent=1))
    else:
        print(report.render())
    return 0 if report.ok else 1


if __name__ == "__main__":
    sys.exit(main())
