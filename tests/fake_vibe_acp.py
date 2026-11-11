#!/usr/bin/env python
"""A minimal ACP agent that answers like Mistral Vibe, for tests.

It speaks the slice of the Agent Client Protocol the bridge uses:

    initialize -> session/new -> session/set_config_option
    -> session/prompt -> session/cancel -> session/close

and it mimics the behaviour the tests depend on:

  - the model list and the thinking levels belong together, the way the real
    agent reports them (Medium accepts off/high, GLM accepts low/high/max);
  - a prompt that says "sleep N" takes N seconds, so a test can have a prompt
    running while other tools are called;
  - the answer is the token the prompt asked for ("reply with exactly: X").

Use it through VIBE_TEST_ACP_EXE to run the suite with no Mistral account:

    VIBE_TEST_ACP_EXE=python3 python3 tests/regress.py
"""
import json
import re
import sys
import threading
import time
import uuid

MODELS = {
    "mistral-medium-3.5": {"thinking": ["off", "high"], "default": "high"},
    "glm-5-3": {"thinking": ["low", "high", "max"], "default": "high"},
    "zai-glm-5-3-flash": {"thinking": ["low", "high", "max"], "default": "high"},
    "mistral-large-4-0-3": {"thinking": ["low", "medium", "high"], "default": "high"},
}
ACTIVE_MODEL = "mistral-medium-3.5"
SESSION_ID = str(uuid.uuid4())
STATE = {"model": ACTIVE_MODEL, "thinking": MODELS[ACTIVE_MODEL]["default"], "mode": "accept-edits"}
# Set by session/cancel, so a long tool run stops like the real agent does.
CANCELLED = threading.Event()


def send(obj):
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()


def config_options():
    levels = MODELS[STATE["model"]]["thinking"]
    return [
        {
            "id": "mode",
            "currentValue": STATE["mode"],
            "options": [
                {"value": "ask", "name": "Ask"},
                {"value": "accept-edits", "name": "Accept Edits"},
                {"value": "smart-approve", "name": "Smart Approve"},
                {"value": "auto-approve", "name": "Auto Approve"},
            ],
        },
        {
            "id": "model",
            "currentValue": STATE["model"],
            "options": [{"value": name, "name": name} for name in MODELS],
        },
        {
            "id": "thinking",
            "currentValue": STATE["thinking"],
            "options": [{"value": level, "name": level} for level in levels],
        },
    ]


def notify(kind, **extra):
    send({
        "jsonrpc": "2.0",
        "method": "session/update",
        "params": {"sessionId": SESSION_ID, "update": {"sessionUpdate": kind, **extra}},
    })


def answer_for(text):
    match = re.search(r"exactly:\s*([A-Za-z0-9_\-]+)", text)
    if match:
        return match.group(1)
    return "STUB-OK"


def sleep_for(text):
    match = re.search(r"sleep\s+(\d+)", text)
    return int(match.group(1)) if match else 0


def handle_prompt(params):
    CANCELLED.clear()
    text = ""
    for block in params.get("prompt") or []:
        if block.get("type") == "text":
            text += block.get("text") or ""
    delay = sleep_for(text)
    if "tool" in text.lower():
        notify("tool_call", title="bash: stub command", toolCallId="stub-1")
        notify("tool_call_update", title="bash: stub command", status="completed", toolCallId="stub-1")
    if delay:
        # A long tool run is silent: no updates while it lasts, and a cancel
        # interrupts it.
        if CANCELLED.wait(delay):
            return {"stopReason": "cancelled", "usage": {"totalTokens": 10}}
    notify("agent_thought_chunk", content={"type": "text", "text": "stub thinking"})
    notify("agent_message_chunk", content={"type": "text", "text": answer_for(text)})
    return {"stopReason": "end_turn", "usage": {"totalTokens": 100, "inputTokens": 90, "outputTokens": 10}}


def handle(msg):
    method = msg.get("method")
    params = msg.get("params") or {}
    if method == "initialize":
        return {
            "protocolVersion": 1,
            "agentCapabilities": {"loadSession": True, "promptCapabilities": {"image": True}},
            "authMethods": [],
            "agentInfo": {"name": "@mistralai/mistral-vibe", "title": "Mistral Vibe (stub)", "version": "0.0.0-stub"},
        }
    if method == "session/new":
        return {
            "sessionId": SESSION_ID,
            "modes": {"currentModeId": STATE["mode"], "availableModes": []},
            "configOptions": config_options(),
        }
    if method == "session/set_config_option":
        config_id = params.get("configId")
        value = params.get("value")
        if config_id == "model":
            if value not in MODELS:
                raise ValueError(f"unknown model {value}")
            STATE["model"] = value
            STATE["thinking"] = MODELS[value]["default"]
        elif config_id == "thinking":
            allowed = MODELS[STATE["model"]]["thinking"]
            if value not in allowed:
                raise ValueError(f"unknown thinking {value!r}; choose one of {allowed}")
            STATE["thinking"] = value
        elif config_id == "mode":
            STATE["mode"] = value
        else:
            raise ValueError(f"unknown config option {config_id}")
        return {"configOptions": config_options()}
    if method == "session/prompt":
        return handle_prompt(params)
    if method == "session/cancel":
        CANCELLED.set()
        return {}
    if method == "$/cancel_request":
        CANCELLED.set()
        return {}
    if method == "session/close":
        return {}
    return {}


def run_request(msg):
    """Answer one request in its own thread.

    A real agent serves a long prompt while still reading the socket, so a
    cancel can arrive in the middle of it. A single-threaded stub would never
    see that cancel.
    """
    try:
        result = handle(msg)
    except Exception as exc:  # noqa: BLE001
        send({"jsonrpc": "2.0", "id": msg["id"], "error": {"code": -32603, "message": str(exc)}})
        return
    send({"jsonrpc": "2.0", "id": msg["id"], "result": result})


def main():
    for raw in sys.stdin:
        raw = raw.strip()
        if not raw:
            continue
        try:
            msg = json.loads(raw)
        except Exception:
            continue
        if "method" not in msg:
            continue
        method = msg["method"]
        if msg.get("id") is None:
            # A notification: the cancel has to reach the running prompt.
            if method in ("session/cancel", "$/cancel_request"):
                CANCELLED.set()
            continue
        threading.Thread(target=run_request, args=(msg,), daemon=True).start()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
