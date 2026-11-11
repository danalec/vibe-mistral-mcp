#!/usr/bin/env python
"""MCP stdio bridge: let DeepSeek Harness delegate work to Mistral Vibe.

Mistral Vibe's subscription token (``vibe-acp --setup``) is a chat-plan
credential, not an `api.mistral.ai` key, so it cannot back a pi-ai provider
route. What it can back is an ACP agent: this server speaks MCP to DSH and ACP
(agentclientprotocol.com, v1) to ``vibe-acp.exe``, exposing Vibe as a tool.

Tools:
  vibe_prompt  - run one prompt through the Vibe agent and return its answer
  vibe_options - models / thinking levels / approval modes Vibe offers
  vibe_reset   - drop the ACP session (and process) so the next prompt is fresh

Stdlib only; MCP over stdio is newline-delimited JSON-RPC 2.0.
"""
import json
import os
import shutil
import subprocess
import sys
import threading
import time
import uuid

import studio


def _default_acp_exe():
    """Locate the Vibe ACP agent on any platform.

    Order: the configured environment variable, the executable on PATH, then
    the usual install locations of each platform.
    """
    configured = os.environ.get("VIBE_ACP_EXE", "").strip()
    if configured:
        return configured
    found = shutil.which("vibe-acp")
    if found:
        return found
    candidates = []
    if os.name == "nt":
        local = os.environ.get("LOCALAPPDATA", "")
        if local:
            candidates.append(os.path.join(local, r"Microsoft\WinGet\Links\vibe-acp.exe"))
    else:
        home = os.path.expanduser("~")
        candidates.extend([
            os.path.join(home, ".local", "bin", "vibe-acp"),
            "/usr/local/bin/vibe-acp",
            "/usr/bin/vibe-acp",
        ])
    for candidate in candidates:
        if os.path.isfile(candidate):
            return candidate
    # Left as a bare name so the failure names the command, not a path.
    return "vibe-acp"


SCRIPT_SUFFIXES = (".py", ".mjs", ".js", ".cjs")


def _absolute_argument(arg):
    """Make a script argument absolute against the bridge's own directory.

    The ACP agent is spawned in another working directory, so a relative
    script path would not be found there.
    """
    if os.path.isabs(arg) or not arg.lower().endswith(SCRIPT_SUFFIXES):
        return arg
    return os.path.abspath(arg)


def resolve_acp_command(exe):
    """Turn a path or a command into the argv that starts the ACP agent.

    A path wins over a command name, so a path containing spaces still works.
    A command may carry arguments ("python3 tests/fake_vibe_acp.py").
    """
    if os.path.isfile(exe):
        return [exe]
    parts = exe.split()
    if len(parts) > 1:
        head = shutil.which(parts[0])
        if head:
            return [head] + [_absolute_argument(part) for part in parts[1:]]
    found = shutil.which(exe)
    if found:
        return [found]
    raise VibeError(
        f"vibe-acp executable not found: {exe}. Install Mistral Vibe "
        f"(vibe-acp --setup) or set VIBE_ACP_EXE to its full path."
    )


def resolve_acp_exe(exe):
    """The runnable path of the ACP agent, for messages and version checks."""
    return resolve_acp_command(exe)[0]


DEFAULT_EXE = _default_acp_exe()
DEFAULT_CWD = os.environ.get("VIBE_ACP_CWD") or os.path.expanduser("~")
# Node's setTimeout ceiling is 2,147,483,647 ms (~24.8 days) and the MCP client
# uses that as its own bound; the bridge sits just under it so its controlled
# timeout wins over the client's generic one.
DEFAULT_TIMEOUT = float(os.environ.get("VIBE_ACP_TIMEOUT", "2100000"))
MAX_TIMEOUT = 2_100_000.0
# Applied when the caller omits the field, so a bare prompt still runs with the
# intended model instead of whatever Vibe last persisted. Empty means "leave
# the model to Vibe": a model the account cannot serve makes Vibe fall back
# silently, and the agent never learns which model actually answered.
DEFAULT_MODEL = os.environ.get("VIBE_ACP_MODEL", "").strip()
DEFAULT_THINKING = os.environ.get("VIBE_ACP_THINKING", "high")
DEFAULT_MODE = os.environ.get("VIBE_ACP_MODE", "accept-edits")
# A Vibe prompt that emits no ACP update for this long is treated as wedged:
# it is cancelled, and killed if cancellation does not settle within the grace.
IDLE_TIMEOUT = float(os.environ.get("VIBE_ACP_IDLE_TIMEOUT", "1200"))
# A tool call may legitimately run for minutes (a build, a test suite) without
# an update, so the silence budget is larger while one is in flight.
TOOL_IDLE_TIMEOUT = float(os.environ.get("VIBE_ACP_TOOL_IDLE_TIMEOUT", "900"))
CANCEL_GRACE = float(os.environ.get("VIBE_ACP_CANCEL_GRACE", "30"))


def log(*parts):
    print("[vibe-mistral-mcp]", *parts, file=sys.stderr, flush=True)


class VibeError(RuntimeError):
    pass


class VibeACP:
    """One long-lived vibe-acp process with at most one ACP session."""

    def __init__(self, exe):
        self.exe = exe
        self.proc = None
        self.session_id = None
        self.session_cwd = None
        self.options = []
        self._next_id = 0
        self._responses = {}
        self._cond = threading.Condition()
        self._write_lock = threading.Lock()
        self._out_lock = threading.Lock()
        # Guards the process lifecycle only. It is never held while waiting
        # for an answer, so a slow prompt cannot block close() or a restart.
        self._proc_lock = threading.RLock()
        self._prompt_gate = threading.Lock()
        self._prompt_active = False
        self._cancel_requested = False
        # Bumped for every prompt, so a cancel or a kill escalation can name
        # the prompt it was meant for and leave a newer one alone.
        self._prompt_generation = 0
        self._chunks = []
        self._tool_calls = []
        self._stderr_tail = []
        self._last_update = time.time()
        self._reader_alive = False
        # True while a tool call is in flight; a build or a test suite can run
        # for minutes without an update and must not look like a hang.
        self._tool_running = False
        # Bumped for every process; a reader thread only clears _reader_alive
        # while its own generation is still the current one.
        self._generation = 0
        self._progress_note = ""
        # A cancelled prompt leaves the ACP session in an unknown state: the
        # next prompt would inherit the cancellation. The session is replaced
        # instead of reused.
        self._session_dirty = False

    @property
    def busy(self):
        return self._prompt_active

    def cancel_current(self):
        """Ask Vibe to stop the in-flight prompt (ACP session/cancel)."""
        if not self._prompt_active or not self.session_id:
            return False
        self._cancel_requested = True
        self._session_dirty = True
        try:
            self._send(
                {"jsonrpc": "2.0", "method": "session/cancel",
                 "params": {"sessionId": self.session_id}}
            )
            log("sent session/cancel")
            return True
        except Exception as exc:  # noqa: BLE001
            log("session/cancel failed:", exc)
            return False

    # -- process plumbing -------------------------------------------------
    def _ensure_process(self):
        if self.proc is not None and self.proc.poll() is None:
            return
        executable = resolve_acp_command(self.exe)
        log("starting", " ".join(executable))
        self.proc = subprocess.Popen(
            executable,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            cwd=DEFAULT_CWD,
        )
        self.session_id = None
        self.session_cwd = None
        self._responses.clear()
        self._generation += 1
        generation = self._generation
        # The flag is set here, not in the reader thread: a request issued
        # right after the spawn must not see a half-started process.
        self._reader_alive = True
        threading.Thread(target=self._read_stdout, args=(generation,), daemon=True).start()
        threading.Thread(target=self._read_stderr, args=(generation,), daemon=True).start()

    def _read_stdout(self, generation):
        try:
            for line in self.proc.stdout:
                line = line.strip()
                if not line:
                    continue
                try:
                    msg = json.loads(line)
                except Exception:
                    log("non-json stdout:", line[:200])
                    continue
                if msg.get("id") is not None and msg.get("method") is None:
                    with self._cond:
                        self._responses[msg["id"]] = msg
                        self._cond.notify_all()
                elif msg.get("method") and msg.get("id") is not None:
                    self._answer_agent_request(msg)
                elif msg.get("method"):
                    self._note_update(msg.get("params") or {})
        finally:
            # Only the reader of the current process may clear the flag: a
            # reader of a replaced process would otherwise mark the live one
            # as dead.
            if generation == self._generation:
                self._reader_alive = False
            with self._cond:
                self._cond.notify_all()

    def _note_update(self, params):
        self._last_update = time.time()
        update = params.get("update") or {}
        kind = update.get("sessionUpdate") or params.get("sessionUpdate")
        if kind == "agent_message_chunk":
            content = update.get("content") or {}
            if content.get("type") == "text" and content.get("text"):
                with self._cond:
                    self._chunks.append(content["text"])
        elif kind == "tool_call":
            title = update.get("title") or update.get("kind") or "tool"
            self._progress_note = str(title)[:120]
            # A tool may run for minutes without a single update, so the
            # watchdog counts that time against a larger budget.
            self._tool_running = True
            with self._cond:
                self._tool_calls.append(str(title)[:160])
        elif kind == "tool_call_update":
            status = update.get("status")
            if status in ("completed", "failed"):
                title = update.get("title")
                self._tool_running = False
                with self._cond:
                    if title:
                        self._tool_calls.append(f"{title} [{status}]")

    def _read_stderr(self, generation):
        for line in self.proc.stderr:
            line = line.rstrip()
            if not line:
                continue
            self._stderr_tail.append(line)
            del self._stderr_tail[:-40]
            if "not available" in line:
                self._progress_note = line[:160]
            log("vibe:", line[:300])

    def _send(self, obj):
        if self.proc is None or self.proc.poll() is not None:
            raise VibeError("vibe-acp process is gone")
        payload = json.dumps(obj) + "\n"
        with self._write_lock:
            self.proc.stdin.write(payload)
            self.proc.stdin.flush()

    def request(self, method, params, timeout):
        # The lock covers only "make sure there is a process and send"; the
        # wait happens without it, so a running prompt never blocks a close,
        # a restart or another request's bookkeeping.
        with self._proc_lock:
            self._ensure_process()
            self._next_id += 1
            rid = self._next_id
            self._send({"jsonrpc": "2.0", "id": rid, "method": method, "params": params})
        deadline = time.time() + timeout
        idle_limit = IDLE_TIMEOUT if method == "session/prompt" else 0.0
        cancelled_at = None
        with self._cond:
            while rid not in self._responses:
                if self.proc is None or self.proc.poll() is not None:
                    raise VibeError("vibe-acp exited while the request was pending" + self._tail())
                if not self._reader_alive:
                    raise VibeError("vibe-acp output reader stopped; the ACP stream is dead" + self._tail())
                remaining = deadline - time.time()
                if remaining <= 0:
                    raise VibeError(f"timeout after {int(timeout)}s waiting for {method}" + self._tail())
                self._cond.wait(min(remaining, 1.0))
                if idle_limit:
                    now = time.time()
                    budget = TOOL_IDLE_TIMEOUT if self._tool_running else idle_limit
                    if cancelled_at is None and now - self._last_update > budget:
                        # Vibe sometimes wedges on a tool or a model retry and
                        # emits nothing ever again. Silence is the signal.
                        log(f"no ACP update for {int(now - self._last_update)}s; cancelling")
                        self._progress_note = f"cancelled: no progress for {int(now - self._last_update)}s"
                        self.cancel_current()
                        cancelled_at = now
                    elif cancelled_at is not None and now - cancelled_at > CANCEL_GRACE:
                        log("cancel did not settle; killing vibe-acp")
                        self._kill_process()
                        raise VibeError(
                            f"prompt cancelled after {int(idle_limit)}s without progress and the agent did not stop"
                        )
            msg = self._responses.pop(rid)
        if "error" in msg:
            raise VibeError(f"{method} failed: {json.dumps(msg['error'])[:500]}")
        return msg.get("result") or {}

    def _tail(self):
        return f"; stderr: {self._stderr_tail[-1]}" if self._stderr_tail else ""


    def _answer_agent_request(self, msg):
        """Vibe asks for tool permission; approve one call, not a standing grant."""
        method = msg.get("method")
        params = msg.get("params") or {}
        if method == "session/request_permission":
            options = params.get("options") or []
            allow_once, allow_always = None, None
            for opt in options:
                kind = str(opt.get("kind") or "")
                oid = str(opt.get("optionId") or opt.get("id") or "")
                if kind == "allow_once" or oid.lower().startswith("allow_once"):
                    allow_once = allow_once or oid
                elif kind == "allow_always" or oid.lower().startswith("allow_always"):
                    allow_always = allow_always or oid
            # A persistent grant outlives the task, so it is only used when
            # there is no single-call option to pick.
            chosen = allow_once or allow_always
            if chosen is None and options:
                chosen = options[0].get("optionId") or options[0].get("id")
            result = (
                {"outcome": {"outcome": "selected", "optionId": chosen}}
                if chosen
                else {"outcome": {"outcome": "cancelled"}}
            )
            log("auto-approved permission request:", chosen)
        else:
            log("answering agent request", method, "with empty result")
            result = {}
        try:
            self._send({"jsonrpc": "2.0", "id": msg["id"], "result": result})
        except Exception as exc:  # noqa: BLE001 - best effort
            log("failed to answer agent request:", exc)

    # -- ACP surface ------------------------------------------------------
    def ensure_session(self, cwd):
        self._ensure_process()
        if self.session_id and self.session_cwd == cwd and not self._session_dirty:
            return self.session_id
        if self.session_id:
            # A cwd change means a different ACP session, and a cancelled
            # prompt leaves the old one unusable. Restart the process instead
            # of negotiating session/close: a close that never answers left
            # the bridge stalled for minutes before the next prompt.
            log("session unusable (cwd change or cancelled prompt), restarting the ACP process")
            self._kill_process()
        self._session_dirty = False
        init = self.request(
            "initialize",
            {
                "protocolVersion": 1,
                "clientCapabilities": {"fs": {"readTextFile": False, "writeTextFile": False}, "terminal": False},
            },
            60,
        )
        log("agent:", json.dumps(init.get("agentInfo") or {})[:200])
        res = self.request("session/new", {"cwd": cwd, "mcpServers": []}, 120)
        self.session_id = res.get("sessionId")
        self.session_cwd = cwd
        self.options = res.get("configOptions") or []
        return self.session_id

    def option_values(self, config_id):
        for opt in self.options:
            if opt.get("id") == config_id:
                return [o.get("value") for o in (opt.get("options") or [])]
        return []

    def option_current(self, config_id):
        """The value Vibe currently has selected for an option."""
        for opt in self.options:
            if opt.get("id") == config_id:
                return opt.get("currentValue")
        return None

    def set_option(self, config_id, value):
        if not self.session_id:
            raise VibeError("no session")
        res = self.request(
            "session/set_config_option",
            {"sessionId": self.session_id, "configId": config_id, "value": value},
            60,
        )
        self.options = res.get("configOptions") or self.options
        return self.options

    def prompt(self, text, cwd, model, thinking, mode, timeout, on_start=None):
        model = model or DEFAULT_MODEL
        thinking = thinking or DEFAULT_THINKING
        mode = mode or DEFAULT_MODE
        notes = []
        with self._prompt_gate:
            # The session is resolved inside the gate: two prompts with
            # different cwds would otherwise restart each other's session.
            sid = self.ensure_session(cwd)
            if model:
                allowed = self.option_values("model")
                if allowed and model not in allowed:
                    raise VibeError(
                        f"unknown model {model!r}; this Vibe session offers {allowed}"
                    )
                self.set_option("model", model)
            # The thinking levels belong to the model that is active now, and
            # the API rejects any other one: asking Mistral Medium 3.5 for
            # 'low' fails the whole prompt. The model is selected first so
            # the levels below describe it.
            allowed_thinking = self.option_values("thinking")
            if thinking and allowed_thinking and thinking not in allowed_thinking:
                fallback = self.option_current("thinking")
                if not fallback or fallback not in allowed_thinking:
                    fallback = allowed_thinking[-1]
                notes.append(
                    f"thinking {thinking!r} is not offered by this model; used {fallback!r}"
                    f" (levels: {allowed_thinking})"
                )
                thinking = fallback
            if thinking:
                self.set_option("thinking", thinking)
            if mode:
                allowed = self.option_values("mode")
                if allowed and mode not in allowed:
                    raise VibeError(f"unknown mode {mode!r}; choose one of {allowed}")
                self.set_option("mode", mode)
            effective_model = self.option_current("model") or model

            with self._cond:
                self._chunks = []
                self._tool_calls = []
            self._prompt_active = True
            self._cancel_requested = False
            self._prompt_generation += 1
            generation = self._prompt_generation
            self._last_update = time.time()
            self._progress_note = ""
            if on_start is not None:
                on_start()
            try:
                result = self.request(
                    "session/prompt",
                    {"sessionId": sid, "prompt": [{"type": "text", "text": text}]},
                    timeout,
                )
            except VibeError as exc:
                # A live timeout is not retried (the prompt may still be running);
                # cancel it so the next call starts from a known state. A dead
                # process is restarted once and the prompt replayed.
                if self.proc is not None and self.proc.poll() is None:
                    if not self._cancel_requested:
                        log("prompt failed, cancelling:", exc)
                        self.cancel_current()
                    raise
                log("vibe-acp died during the prompt; restarting once")
                self.close()
                self.ensure_session(cwd)
                if model:
                    self.set_option("model", model)
                if thinking:
                    self.set_option("thinking", thinking)
                if mode:
                    self.set_option("mode", mode)
                with self._cond:
                    self._chunks = []
                    self._tool_calls = []
                result = self.request(
                    "session/prompt",
                    {"sessionId": self.session_id, "prompt": [{"type": "text", "text": text}]},
                    timeout,
                )
            finally:
                self._prompt_active = False
                self._cancel_requested = False
        with self._cond:
            rendered = "".join(self._chunks).strip()
            tools = list(self._tool_calls)
        result["_rendered"] = rendered
        result["_tools"] = tools
        result["_model"] = effective_model
        result["_thinking"] = thinking
        result["_notes"] = notes
        result["_generation"] = generation
        return result

    def _kill_process(self):
        """Hard stop: kill the child; no ACP close negotiation."""
        with self._proc_lock:
            proc = self.proc
            self.proc = None
            self.session_id = None
            self.session_cwd = None
            self.options = []
            self._reader_alive = False
        if proc is None:
            return
        try:
            proc.kill()
        except Exception:  # noqa: BLE001
            pass

    def close(self):
        with self._proc_lock:
            proc = self.proc
            session_id = self.session_id
            running = self._prompt_active
        if proc is None:
            return
        if session_id and not running:
            # Politely close so Vibe flushes its own logs, but never let a
            # missing reply stall teardown.
            try:
                self.request("session/close", {"sessionId": session_id}, 5)
            except Exception:  # noqa: BLE001 - teardown is best effort
                pass
        self._kill_process()


VIBE = VibeACP(DEFAULT_EXE)

TOOLS = [
    {
        "name": "vibe_prompt",
        "description": (
            "Delegate one task to the Mistral Vibe agent (Mistral Large 4 and other Mistral-hosted "
            "models) through its ACP interface. Vibe works in the given directory with its own "
            "file/command tools and returns only its final answer; it does not see this conversation, "
            "so restate everything it needs in the prompt."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "prompt": {"type": "string", "description": "Complete, self-contained instruction for Vibe."},
                "cwd": {"type": "string", "description": "Absolute working directory for the Vibe session. Defaults to the user home."},
                "model": {"type": "string", "description": "Vibe model id (see vibe_options). Defaults to Vibe's own choice."},
                "thinking": {"type": "string", "description": "Thinking level: low | medium | high."},
                "mode": {"type": "string", "description": "Approval mode: ask | accept-edits | smart-approve | auto-approve."},
                "timeout_s": {"type": "number", "description": "Seconds to wait for the answer (default and max: 2,100,000 — the bridge does not cut a real task short)."},
                "new_session": {"type": "boolean", "description": "Start a fresh Vibe session (drops prior Vibe context)."},
            },
            "required": ["prompt"],
        },
    },
    {
        "name": "vibe_start",
        "description": (
            "Start a Mistral Vibe task in the BACKGROUND and return a job id immediately. Vibe works "
            "in its own ACP session with its own file/command tools, so it does not see this "
            "conversation: restate everything it needs. Poll with vibe_poll; it is the default way to "
            "delegate coding work."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "prompt": {"type": "string", "description": "Complete, self-contained instruction for Vibe."},
                "cwd": {"type": "string", "description": "Absolute working directory for the Vibe session."},
                "model": {"type": "string", "description": "Vibe model id (see vibe_options). Default mistral-large-4-0-3."},
                "thinking": {"type": "string", "description": "Thinking level: low | medium | high."},
                "mode": {"type": "string", "description": "Approval mode: ask | accept-edits | smart-approve | auto-approve."},
                "timeout_s": {"type": "number", "description": "Seconds before the job is cancelled. Default and max 2,100,000."},
                "new_session": {"type": "boolean", "description": "Start a fresh Vibe session before this job (refused while another job runs)."},
            },
            "required": ["prompt"],
        },
    },
    {
        "name": "vibe_poll",
        "description": "Check a background Vibe job. Returns the final answer once done, or its state while running.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "job_id": {"type": "string", "description": "Job id returned by vibe_start."},
                "wait_s": {"type": "number", "description": "Wait up to this many seconds for completion (max 600)."},
            },
            "required": ["job_id"],
        },
    },
    {
        "name": "vibe_jobs",
        "description": "List recent Vibe jobs with state, elapsed time, model and working directory.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "vibe_options",
        "description": "Show the models, thinking levels and approval modes the connected Mistral Vibe session offers, plus the current selection.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "vibe_status",
        "description": (
            "Health of this bridge in one call: whether the Mistral Vibe process and its ACP session "
            "are alive, whether a prompt is running and how long it has been silent, the job counts, "
            "and whether the Mistral Studio half is reachable. Never blocks on a running prompt."
        ),
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "vibe_reset",
        "description": "Close the Mistral Vibe ACP session and process; the next vibe_prompt starts fresh.",
        "inputSchema": {"type": "object", "properties": {}},
    },
]


def text_result(text, is_error=False):
    return {"content": [{"type": "text", "text": text}], "isError": is_error}


JOBS = {}
JOB_ORDER = []
JOBS_LOCK = threading.Lock()
MAX_JOBS = 20


def render_result(res, started, model_arg):
    usage = res.get("usage") or {}
    header = (
        f"stopReason={res.get('stopReason')} model={res.get('_model') or model_arg or '(vibe default)'} "
        f"thinking={res.get('_thinking') or '-'} "
        f"tokens={usage.get('totalTokens')} elapsed={round(time.time() - started, 1)}s"
    )
    body = res.get("_rendered") or f"(Vibe returned no assistant text; stopReason={res.get('stopReason')})"
    tools = res.get("_tools") or []
    if tools:
        shown = "\n".join(f"  - {t}" for t in tools[:12])
        more = f"\n  ... {len(tools) - 12} more" if len(tools) > 12 else ""
        header += f"\nvibe tool activity ({len(tools)}):\n{shown}{more}"
    for note in res.get("_notes") or []:
        header += f"\nnote: {note}"
    return f"{body}\n\n[{header}]"


def _run_job(jid, args):
    with JOBS_LOCK:
        job = JOBS.get(jid)
    if job is None:
        return
    if job.get("cancelled"):
        with JOBS_LOCK:
            job.update(status="cancelled", finished=time.time(), error="cancelled before it started")
        return

    def on_start():
        with JOBS_LOCK:
            if job.get("cancelled"):
                raise RuntimeError("cancelled before start")
            job.update(status="running", running_since=time.time())

    try:
        timeout = min(float(args.get("timeout_s") or DEFAULT_TIMEOUT), MAX_TIMEOUT)
        res = VIBE.prompt(
            (args.get("prompt") or "").strip(),
            cwd=args.get("cwd") or DEFAULT_CWD,
            model=args.get("model"),
            thinking=args.get("thinking"),
            mode=args.get("mode"),
            timeout=timeout,
            on_start=on_start,
        )
        text = render_result(res, job["started"], job["model"])
        with JOBS_LOCK:
            job.update(status="done", text=text, finished=time.time())
    except Exception as exc:  # noqa: BLE001 - reported by vibe_poll
        with JOBS_LOCK:
            if job.get("cancelled"):
                job.update(status="cancelled", error=job.get("error") or str(exc), finished=time.time())
            else:
                job.update(status="error", error=str(exc), finished=time.time())


def start_job(args):
    prompt = (args.get("prompt") or "").strip()
    if not prompt:
        return text_result("vibe_start needs a non-empty prompt", True)
    if args.get("new_session"):
        if VIBE.busy:
            return text_result("a Vibe task is already running; poll it first or reset after it settles", True)
        VIBE.close()
    jid = f"v{int(time.time() * 1000) % 1000000}-{uuid.uuid4().hex[:6]}"
    job = {
        "id": jid,
        "status": "queued",
        "started": time.time(),
        "running_since": None,
        "finished": None,
        "cwd": args.get("cwd") or DEFAULT_CWD,
        "model": args.get("model") or DEFAULT_MODEL,
        "preview": prompt[:120].replace("\n", " "),
        "text": None,
        "error": None,
        "cancelled": False,
    }
    with JOBS_LOCK:
        JOBS[jid] = job
        JOB_ORDER.append(jid)
        while len(JOB_ORDER) > MAX_JOBS:
            evicted_id = JOB_ORDER.pop(0)
            evicted = JOBS.pop(evicted_id, None)
            if evicted is not None and evicted["status"] in ("queued", "running"):
                # The evicted prompt may still hold the single prompt slot;
                # marking it cancelled stops its worker instead of orphaning it.
                evicted["cancelled"] = True
                evicted["error"] = "evicted: too many jobs retained"
                if evicted["status"] == "queued":
                    evicted.update(status="cancelled", finished=time.time())
    threading.Thread(target=_run_job, args=(jid, args), daemon=True).start()
    return text_result(
        f"started job {jid} in the background (model {job['model'] or 'vibe default'}, cwd {job['cwd']}).\n"
        f"job_id: {jid}\n"
        f"Poll with vibe_poll(job_id=\"{jid}\") or keep working; vibe_jobs lists everything."
    )


def _job_state(job):
    """Report the job's own phase, not the process-wide busy flag."""
    state = job["status"]
    if state == "queued" and not VIBE.busy:
        state = "waiting"
    return state


def poll_job(args):
    jid = str(args.get("job_id") or "").strip()
    if not jid:
        return text_result("vibe_poll needs job_id (see vibe_jobs)", True)
    wait_s = min(max(float(args.get("wait_s") or 0), 0.0), 600.0)
    deadline = time.time() + wait_s
    while True:
        with JOBS_LOCK:
            job = JOBS.get(jid)
            done = job is not None and job["status"] not in ("queued", "running")
        if done or time.time() >= deadline:
            break
        time.sleep(0.5)
    if job is None:
        return text_result(f"unknown job {jid!r}; see vibe_jobs", True)
    elapsed = round((job["finished"] or time.time()) - job["started"], 1)
    if job["status"] == "done":
        return text_result(job["text"])
    if job["status"] == "cancelled":
        return text_result(f"Vibe job {jid} was cancelled after {elapsed}s: {job.get('error') or 'no reason recorded'}", True)
    if job["status"] == "error":
        return text_result(f"Vibe job {jid} failed after {elapsed}s: {job['error']}", True)
    state = _job_state(job)
    detail = ""
    if job["status"] == "running":
        idle = int(time.time() - VIBE._last_update)
        detail = f", idle {idle}s"
        if VIBE._progress_note:
            detail += f", last activity: {VIBE._progress_note}"
    return text_result(
        f"job {jid} {state} ({elapsed}s so far{detail}). "
        f"Poll again or continue other work; the bridge cancels a prompt that emits no update for {int(IDLE_TIMEOUT)}s."
    )


def list_jobs(_args=None):
    with JOBS_LOCK:
        items = [JOBS[j] for j in JOB_ORDER if j in JOBS]
    if not items:
        return text_result("no Vibe jobs yet")
    lines = []
    for job in items:
        state = _job_state(job)
        elapsed = round((job["finished"] or time.time()) - job["started"], 1)
        extra = ""
        if job["status"] == "running":
            extra = f" idle={int(time.time() - VIBE._last_update)}s"
        lines.append(f'{job["id"]} [{state}] {elapsed}s{extra} {job["model"]} {job["cwd"]} :: {job["preview"]}')
    return text_result("\n".join(lines))


def render_status():
    """One-shot health of the bridge. Never touches the ACP session."""
    lines = []
    with VIBE._proc_lock:
        alive = VIBE.proc is not None and VIBE.proc.poll() is None
        pid = VIBE.proc.pid if alive else None
        session_cwd = VIBE.session_cwd
        has_session = bool(VIBE.session_id)
        reader_alive = VIBE._reader_alive
    lines.append(f"acp process: {'alive' if alive else 'not running'}"
                 + (f" (pid {pid})" if pid else ""))
    lines.append(f"acp session: {'open' if has_session else 'none'}"
                 + (f" cwd={session_cwd}" if session_cwd else ""))
    lines.append(f"acp reader: {'alive' if reader_alive else 'dead'}")
    if VIBE.busy:
        idle = int(time.time() - VIBE._last_update)
        lines.append(f"prompt: running, idle {idle}s"
                     + (f", last activity: {VIBE._progress_note}" if VIBE._progress_note else ""))
    else:
        lines.append("prompt: idle")
    with JOBS_LOCK:
        counts = {}
        for job in JOBS.values():
            counts[job["status"]] = counts.get(job["status"], 0) + 1
    lines.append("jobs: " + (", ".join(f"{k}={v}" for k, v in sorted(counts.items())) or "none"))
    try:
        studio_status = studio.status()
    except Exception as exc:  # noqa: BLE001
        studio_status = f"unreachable ({exc})"
    lines.append(f"studio: {studio_status}")
    return "\n".join(lines)


def call_tool(name, args):
    if name in studio.EXPOSED:
        try:
            return studio.call_tool(name, args)
        except Exception as exc:  # noqa: BLE001 - surfaced to the calling model
            return text_result(f"Mistral Studio failed: {exc}", True)
    if name == "vibe_start":
        return start_job(args)
    if name == "vibe_poll":
        return poll_job(args)
    if name == "vibe_jobs":
        return list_jobs(args)
    if name == "vibe_prompt":
        prompt = (args.get("prompt") or "").strip()
        if not prompt:
            return text_result("vibe_prompt needs a non-empty prompt", True)
        if VIBE.busy:
            # The harness timeout for this tool is ~24 days, so blocking here
            # would be an invisible multi-hour hang.
            return text_result(
                "a Vibe task is already running; use vibe_start + vibe_poll for "
                "background work, or wait for the running one to finish",
                True,
            )
        timeout = min(float(args.get("timeout_s") or DEFAULT_TIMEOUT), MAX_TIMEOUT)
        if args.get("new_session"):
            VIBE.close()
        started = time.time()
        try:
            res = VIBE.prompt(
                prompt,
                cwd=args.get("cwd") or DEFAULT_CWD,
                model=args.get("model"),
                thinking=args.get("thinking"),
                mode=args.get("mode"),
                timeout=timeout,
            )
        except Exception as exc:  # noqa: BLE001 - surfaced to the calling model
            return text_result(f"Vibe failed: {exc}", True)
        return text_result(render_result(res, started, args.get("model")))
    if name == "vibe_options":
        # The cached options answer instantly even while a prompt runs; only a
        # cold start needs the ACP round trip, and that must never run while a
        # prompt owns the session.
        if not VIBE.options:
            if VIBE.busy:
                return text_result(
                    "a Vibe task is running and no session options are cached yet; "
                    "call vibe_options again once it finishes",
                    True,
                )
            try:
                VIBE.ensure_session(DEFAULT_CWD)
            except Exception as exc:  # noqa: BLE001
                return text_result(f"Vibe unavailable: {exc}", True)
        lines = []
        for opt in VIBE.options:
            values = [o.get("value") for o in (opt.get("options") or [])]
            lines.append(f"{opt.get('id')}: current={opt.get('currentValue')} options={values}")
        if VIBE.busy:
            lines.append("state: busy (a prompt is still running)")
        return text_result("\n".join(lines) or "no config options reported")
    if name == "vibe_status":
        return text_result(render_status())
    if name == "vibe_reset":
        # Cancel queued work first, then the running prompt, then escalate to a
        # hard kill if ACP cancellation never settles — a wedged Vibe otherwise
        # blocks every later delegation forever.
        cancelled_queued = 0
        with JOBS_LOCK:
            for other in JOBS.values():
                if other["status"] in ("queued", "running"):
                    other["cancelled"] = True
                    other["error"] = "cancelled by vibe_reset"
                    if other["status"] == "queued":
                        other.update(status="cancelled", finished=time.time())
                        cancelled_queued += 1
        if VIBE.busy:
            generation = VIBE._prompt_generation
            VIBE.cancel_current()

            def escalate(gen=generation):
                time.sleep(CANCEL_GRACE)
                # Only the prompt that was cancelled may be killed: a newer
                # prompt must survive the escalation.
                if VIBE.busy and VIBE._prompt_generation == gen:
                    log("vibe_reset: cancel did not settle; killing vibe-acp")
                    VIBE._progress_note = "killed by vibe_reset"
                    VIBE._kill_process()

            threading.Thread(target=escalate, daemon=True).start()
            return text_result(
                f"Vibe cancel requested for the running prompt"
                + (f"; {cancelled_queued} queued job(s) cancelled" if cancelled_queued else "")
                + f". If it does not settle in {int(CANCEL_GRACE)}s the process is killed."
            )
        VIBE.close()
        return text_result(
            "Vibe session closed"
            + (f"; {cancelled_queued} queued job(s) cancelled" if cancelled_queued else "")
        )
    return text_result(f"unknown tool {name}", True)


def write_message(obj):
    with VIBE._out_lock:
        sys.stdout.write(json.dumps(obj) + "\n")
        sys.stdout.flush()


def run_call(rid, name, args):
    try:
        result = call_tool(name, args)
    except Exception as exc:  # noqa: BLE001
        result = text_result(f"bridge error: {exc}", True)
    write_message({"jsonrpc": "2.0", "id": rid, "result": result})


def main():
    # The Studio schemas are warmed in the background: tools/list must answer
    # immediately even when the upstream is unreachable.
    studio.start_prefetch()
    for raw in sys.stdin:
        raw = raw.strip()
        if not raw:
            continue
        try:
            msg = json.loads(raw)
        except Exception:
            log("bad request line:", raw[:200])
            continue
        if "method" not in msg:
            continue
        method = msg["method"]
        rid = msg.get("id")
        params = msg.get("params") or {}
        if method == "initialize":
            result = {
                "protocolVersion": params.get("protocolVersion") or "2024-11-05",
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": "vibe", "version": "1.0.0"},
            }
        elif method in ("notifications/initialized", "initialized"):
            continue
        elif method in ("notifications/cancelled", "$/cancel_request"):
            # DSH aborted a tool call: stop Vibe so it stops burning the plan.
            VIBE.cancel_current()
            continue
        elif method == "ping":
            result = {}
        elif method == "tools/list":
            result = {"tools": TOOLS + studio.tool_definitions()}
        elif method == "tools/call":
            # Each call runs on its own thread: reading stdin stays free, so a
            # cancel/options/reset is answered while a long prompt is running.
            threading.Thread(
                target=run_call,
                args=(rid, params.get("name"), params.get("arguments") or {}),
                daemon=True,
            ).start()
            continue
        else:
            if rid is not None:
                write_message({"jsonrpc": "2.0", "id": rid,
                               "error": {"code": -32601, "message": f"unknown method {method}"}})
            continue
        if rid is not None:
            write_message({"jsonrpc": "2.0", "id": rid, "result": result})


if __name__ == "__main__":
    try:
        main()
    finally:
        VIBE.close()
