"""Independent regression harness for the vibe-mistral-mcp bridge.

Speaks MCP over stdio exactly like the harness does.

    python tests/regress.py            # every check
    python tests/regress.py cold_start # only checks whose name matches

The interpreter, the server and the scratch directory come from the
environment, so the same file runs on any machine:

    VIBE_TEST_PYTHON   interpreter that runs the bridge (default: python)
    VIBE_TEST_SERVER   path to vibe_mcp.py (default: ../vibe_mcp.py)
    VIBE_TEST_WORK     scratch directory (default: a temp directory)
    VIBE_TEST_ACP_EXE  vibe-acp command to use; point it at the stub to run
                       without a Mistral account:
                       VIBE_TEST_ACP_EXE="python3 tests/fake_vibe_acp.py"
"""
import json
import os
import queue
import subprocess
import sys
import tempfile
import threading
import time

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PY = os.environ.get("VIBE_TEST_PYTHON") or sys.executable
SERVER = os.environ.get("VIBE_TEST_SERVER") or os.path.join(REPO, "vibe_mcp.py")
WORK = os.environ.get("VIBE_TEST_WORK") or os.path.join(tempfile.gettempdir(), "vibe-regress")
# The ACP agent to drive: the real vibe-acp, or the stub for a hermetic run.
ACP_EXE = os.environ.get("VIBE_TEST_ACP_EXE", "")

RESULTS = []


def check(name, ok, detail=""):
    RESULTS.append((name, ok, detail))
    print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f"  :: {detail}" if detail else ""))


class Bridge:
    def __init__(self, env=None, cwd=WORK):
        os.makedirs(cwd, exist_ok=True)
        e = dict(os.environ)
        e.update({
            "VIBE_ACP_MODEL": "mistral-large-4-0-3",
            "VIBE_ACP_THINKING": "high",
            "VIBE_ACP_MODE": "accept-edits",
            "VIBE_ACP_TIMEOUT": "300",
            "VIBE_ACP_IDLE_TIMEOUT": "60",
            "VIBE_ACP_CANCEL_GRACE": "5",
        })
        e.update(env or {})
        if ACP_EXE:
            e["VIBE_ACP_EXE"] = ACP_EXE
        self.proc = subprocess.Popen([PY, SERVER], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                     stderr=subprocess.PIPE, text=True, encoding="utf-8",
                                     bufsize=1, env=e)
        self.results = {}
        self.events = {}
        self.err = []
        threading.Thread(target=self._reader, daemon=True).start()
        threading.Thread(target=self._stderr, daemon=True).start()
        self._id = 0
        self._id_lock = threading.Lock()

    def _stderr(self):
        for line in self.proc.stderr:
            self.err.append(line.rstrip())

    def _reader(self):
        for line in self.proc.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                j = json.loads(line)
            except Exception:
                continue
            rid = j.get("id")
            if rid is None or j.get("method") is not None:
                continue
            self.results[rid] = j
            ev = self.events.get(rid)
            if ev is not None:
                ev.set()

    def send(self, obj):
        self.proc.stdin.write(json.dumps(obj) + "\n")
        self.proc.stdin.flush()

    def _new_id(self):
        with self._id_lock:
            self._id += 1
            return self._id

    def call(self, method, params, timeout=60, rid=None):
        rid = rid if rid is not None else self._new_id()
        ev = self.events.setdefault(rid, threading.Event())
        self.send({"jsonrpc": "2.0", "id": rid, "method": method, "params": params})
        if not ev.wait(timeout):
            return None
        return self.results.get(rid)

    def fire(self, method, params, rid=None):
        """Send a request without waiting for its answer."""
        rid = rid if rid is not None else self._new_id()
        self.events.setdefault(rid, threading.Event())
        self.send({"jsonrpc": "2.0", "id": rid, "method": method, "params": params})
        return rid

    def wait_result(self, rid, timeout=60):
        ev = self.events.get(rid)
        if ev is None or not ev.wait(timeout):
            return None
        return self.results.get(rid)

    def tool(self, name, args=None, timeout=60):
        r = self.call("tools/call", {"name": name, "arguments": args or {}}, timeout)
        if r is None:
            return None, "TIMEOUT"
        res = r.get("result") or {}
        try:
            return res["content"][0]["text"], res.get("isError", False)
        except Exception:
            return json.dumps(r)[:300], res.get("isError", False)

    def init(self):
        self.call("initialize", {"protocolVersion": "2024-11-05", "capabilities": {},
                                  "clientInfo": {"name": "regress", "version": "0"}}, 30)
        self.send({"jsonrpc": "2.0", "method": "notifications/initialized"})

    def kill(self):
        try:
            self.proc.kill()
        except Exception:
            pass


def t_tools_list_fast_offline():
    """D3: tools/list must not do network I/O."""
    b = Bridge(env={"MISTRAL_MCP_URL": "http://10.255.255.1:9/mcp", "MISTRAL_MCP_TIMEOUT": "30"})
    try:
        b.init()
        t0 = time.time()
        r = b.call("tools/list", {}, 60)
        dt = time.time() - t0
        names = [t["name"] for t in (r or {}).get("result", {}).get("tools", [])]
        check("D3 tools/list fast with unreachable network", dt < 5.0, f"{dt:.1f}s")
        check("D3 all 12 tools listed", len(names) == 12, ",".join(names))
        check("D13 vibe_status present", "vibe_status" in names)
    finally:
        b.kill()


def t_studio_offline_fails_but_vibe_works():
    """D3/D4: studio fails loudly, vibe still works."""
    b = Bridge(env={"MISTRAL_MCP_URL": "http://10.255.255.1:9/mcp", "MISTRAL_MCP_TIMEOUT": "5"})
    try:
        b.init()
        txt, err = b.tool("studio_usage_limits", {}, 60)
        check("D3 studio_* fails with clear error", err and "Studio" in (txt or ""), (txt or "")[:110])
        txt, err = b.tool("vibe_prompt", {"prompt": "Reply with exactly: REGRESS-OK",
                                          "cwd": WORK, "timeout_s": 120}, 180)
        check("D3 vibe_prompt still works", not err and "REGRESS-OK" in (txt or ""), (txt or "")[:90])
    finally:
        b.kill()


def t_cold_start_no_spurious_error():
    """D1/D2: cold start must not fail with 'reader stopped'."""
    b = Bridge()
    try:
        b.init()
        txt, err = b.tool("vibe_prompt", {"prompt": "Reply with exactly: COLD-OK",
                                          "cwd": WORK, "timeout_s": 120}, 180)
        check("D2 cold start clean", not err and "COLD-OK" in (txt or ""), (txt or "")[:90])
    finally:
        b.kill()


def t_reader_generation_race():
    """D1: after a restart the reader flag must belong to the new process."""
    b = Bridge()
    try:
        b.init()
        b.tool("vibe_prompt", {"prompt": "Reply with exactly: FIRST-OK", "cwd": WORK, "timeout_s": 120}, 180)
        b.tool("vibe_reset", {}, 30)
        time.sleep(1.0)
        txt, err = b.tool("vibe_prompt", {"prompt": "Reply with exactly: SECOND-OK",
                                          "cwd": WORK, "timeout_s": 120}, 180)
        check("D1 restart keeps the reader alive", not err and "SECOND-OK" in (txt or ""), (txt or "")[:90])
    finally:
        b.kill()


def t_vibe_options_does_not_kill_running_prompt():
    """D6: vibe_options must not restart the session while a prompt runs."""
    b = Bridge()
    try:
        b.init()
        b.tool("vibe_prompt", {"prompt": "Reply with exactly: WARM-OK", "cwd": WORK, "timeout_s": 120}, 180)
        long_rid = b.fire("tools/call", {"name": "vibe_prompt", "arguments": {
            "prompt": "Use the shell tool to sleep 30 seconds, then reply with exactly: LONG-DONE",
            "cwd": WORK, "timeout_s": 200}})
        time.sleep(6)
        t0 = time.time()
        txt, err = b.tool("vibe_options", {}, 30)
        dt = time.time() - t0
        check("D6 vibe_options answers while a prompt runs", txt is not None and dt < 20, f"{dt:.1f}s")
        # A second prompt must be refused, not queued behind the first.
        txt2, err2 = b.tool("vibe_prompt", {"prompt": "Reply with exactly: SHOULD-BE-REFUSED",
                                            "cwd": WORK, "timeout_s": 60}, 60)
        refused = txt2 is not None and err2 and "already running" in (txt2 or "")
        check("D6 a second prompt is refused, not queued", refused, (txt2 or "")[:110])
        r3 = b.wait_result(long_rid, 180)
        alive = r3 is not None and "LONG-DONE" in json.dumps(r3)
        check("D6 the running prompt survived vibe_options", alive, json.dumps(r3)[:110] if r3 else "TIMEOUT")
    finally:
        b.kill()


def t_vibe_prompt_refuses_when_job_owns_slot():
    """D12: blocking vibe_prompt must not hang behind a background job."""
    b = Bridge()
    try:
        b.init()
        txt, err = b.tool("vibe_start", {"prompt": "Use the shell tool to sleep 40 seconds, then reply DONE.",
                                         "cwd": WORK, "timeout_s": 200}, 30)
        check("D12 vibe_start accepted", txt is not None and "job_id" in (txt or ""), (txt or "")[:80])
        time.sleep(8)
        t0 = time.time()
        txt2, err2 = b.tool("vibe_prompt", {"prompt": "Reply with exactly: SHOULD-NOT-BLOCK",
                                             "cwd": WORK, "timeout_s": 200}, 90)
        dt = time.time() - t0
        refused = txt2 is not None and err2 and ("vibe_start" in txt2 or "running" in txt2.lower())
        check("D12 vibe_prompt refuses instead of blocking", refused, f"{dt:.1f}s :: {(txt2 or '')[:110]}")
    finally:
        b.kill()


def t_new_session_refused_while_busy():
    """D8: new_session must not kill a running job."""
    b = Bridge()
    try:
        b.init()
        b.tool("vibe_start", {"prompt": "Use the shell tool to sleep 40 seconds, then reply DONE.",
                              "cwd": WORK, "timeout_s": 200}, 30)
        time.sleep(8)
        txt, err = b.tool("vibe_prompt", {"prompt": "Reply with exactly: X", "cwd": WORK,
                                          "timeout_s": 120, "new_session": True}, 60)
        refused = txt is not None and err and "running" in (txt or "").lower()
        check("D8 new_session refused while busy", refused, (txt or "")[:110])
    finally:
        b.kill()


def t_reset_escalation_does_not_kill_new_prompt():
    """D7: the escalation must target the cancelled prompt only."""
    b = Bridge()
    try:
        b.init()
        b.fire("tools/call", {"name": "vibe_prompt", "arguments": {
            "prompt": "Use the shell tool to sleep 60 seconds, then reply DONE",
            "cwd": WORK, "timeout_s": 200}})
        time.sleep(8)
        b.tool("vibe_reset", {}, 30)
        # A client waits for the cancellation to settle before starting again.
        deadline = time.time() + 30
        while time.time() < deadline:
            txt, _ = b.tool("vibe_status", {}, 20)
            if txt and "prompt: idle" in txt:
                break
            time.sleep(1)
        txt, err = b.tool("vibe_prompt", {"prompt": "Reply with exactly: AFTER-RESET",
                                          "cwd": WORK, "timeout_s": 120}, 180)
        check("D7 a new prompt after reset still works", not err and "AFTER-RESET" in (txt or ""), (txt or "")[:90])
    finally:
        b.kill()


def t_status_tool():
    """D13: vibe_status must be fast and informative."""
    b = Bridge()
    try:
        b.init()
        t0 = time.time()
        txt, err = b.tool("vibe_status", {}, 30)
        dt = time.time() - t0
        check("D13 vibe_status fast", txt is not None and dt < 20, f"{dt:.1f}s")
        check("D13 vibe_status informative", txt is not None and len(txt) > 40, (txt or "")[:150].replace("\n", " | "))
    finally:
        b.kill()


def main():
    tests = [
        t_tools_list_fast_offline,
        t_studio_offline_fails_but_vibe_works,
        t_cold_start_no_spurious_error,
        t_reader_generation_race,
        t_vibe_options_does_not_kill_running_prompt,
        t_vibe_prompt_refuses_when_job_owns_slot,
        t_new_session_refused_while_busy,
        t_reset_escalation_does_not_kill_new_prompt,
        t_status_tool,
    ]
    only = sys.argv[1] if len(sys.argv) > 1 else None
    for t in tests:
        if only and only not in t.__name__:
            continue
        print(f"\n--- {t.__name__} ---")
        try:
            t()
        except Exception as exc:  # noqa: BLE001
            check(t.__name__, False, f"EXCEPTION {exc}")
    passed = sum(1 for _, ok, _ in RESULTS if ok)
    print(f"\n===== {passed}/{len(RESULTS)} checks passed =====")
    for name, ok, detail in RESULTS:
        if not ok:
            print(f"  FAILED: {name} :: {detail}")
    return 0 if passed == len(RESULTS) else 1


if __name__ == "__main__":
    sys.exit(main())
