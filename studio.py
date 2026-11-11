"""Mistral Studio tools for the merged Mistral bridge.

The Studio MCP server at ``https://api.mistral.ai/mcp`` publishes 133 tools;
mounting it directly would put every schema into every request. This module
keeps a five-tool whitelist, fetches their real schemas upstream at startup and
forwards calls. It shares the process and credential handling with the Vibe ACP
half of the bridge.

Exposed names are prefixed ``studio_`` so they read as Studio lookups next to the
``vibe_*`` agent tools.
"""
import json
import os
import re
import sys
import threading
import time
import urllib.request

URL = os.environ.get("MISTRAL_MCP_URL", "https://api.mistral.ai/mcp")
TIMEOUT = float(os.environ.get("MISTRAL_MCP_TIMEOUT", "90"))
# A failed connection is remembered only for this long, so a network blip does
# not disable the Studio tools for the life of the process.
RETRY_COOLDOWN = float(os.environ.get("MISTRAL_STUDIO_RETRY_COOLDOWN", "60"))

EXPOSED = {
    "studio_usage_limits": "get_usage_limits",
    "studio_search_docs": "search_mistral_docs",
    "studio_read_doc": "read_mistral_docs_page",
    "studio_list_skills": "list_skills",
    "studio_read_skill": "read_skill",
}

FALLBACK_SCHEMAS = {
    "studio_usage_limits": {"type": "object", "properties": {}},
    "studio_search_docs": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]},
    "studio_read_doc": {"type": "object", "properties": {"url": {"type": "string"}}, "required": ["url"]},
    "studio_list_skills": {"type": "object", "properties": {}},
    "studio_read_skill": {"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]},
}

DESCRIPTIONS = {
    "studio_usage_limits": (
        "Read the organization's Mistral plan and budgets: API and Vibe Code allowance, "
        "how much is used and left, pay-as-you-go spend against the limit, and the reset date."
    ),
    "studio_search_docs": "Search docs.mistral.ai and return matching pages with excerpts.",
    "studio_read_doc": "Read one docs.mistral.ai page as Markdown.",
    "studio_list_skills": "List the workspace Skill catalogue with descriptions.",
    "studio_read_skill": "Read one Skill and its files.",
}


def log(*parts):
    print("[mistral-studio]", *parts, file=sys.stderr, flush=True)


def resolve_api_key():
    key = os.environ.get("MISTRAL_API_KEY", "").strip()
    if key:
        return key
    # The harness home is $DSH_HOME when set, otherwise ~/.dsh.
    home = os.environ.get("DSH_HOME", "").strip() or os.path.join(os.path.expanduser("~"), ".dsh")
    store = os.path.join(home, ".credentials.yaml")
    try:
        text = open(store, encoding="utf-8").read()
    except OSError as exc:
        raise RuntimeError(f"MISTRAL_API_KEY not set and {store} is unreadable: {exc}")
    m = re.search(r"(?m)^\s*MISTRAL_API_KEY\s*:\s*(\S+)\s*$", text)
    if not m:
        raise RuntimeError(f"MISTRAL_API_KEY not set and not found in {store}")
    return m.group(1).strip().strip('"').strip("'")


class StudioMCP:
    """Minimal MCP Streamable HTTP client for api.mistral.ai/mcp."""

    def __init__(self, url, key):
        self.url = url
        self.key = key
        self.session_id = None
        self.tools = []
        self._next_id = 0
        self._lock = threading.Lock()

    def _post(self, payload):
        headers = {
            "Authorization": "Bearer " + self.key,
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }
        if self.session_id:
            headers["Mcp-Session-Id"] = self.session_id
        req = urllib.request.Request(self.url, data=json.dumps(payload).encode(), headers=headers)
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            sid = resp.headers.get("Mcp-Session-Id")
            if sid:
                self.session_id = sid
            return resp.read().decode("utf-8", "replace")

    @staticmethod
    def _messages(body):
        if body.lstrip().startswith("{"):
            try:
                return [json.loads(body)]
            except Exception:
                return []
        out = []
        for line in body.splitlines():
            if line.startswith("data:"):
                try:
                    out.append(json.loads(line[5:].strip()))
                except Exception:
                    pass
        return out

    def request(self, method, params):
        with self._lock:
            self._next_id += 1
            rid = self._next_id
        body = self._post({"jsonrpc": "2.0", "id": rid, "method": method, "params": params})
        for msg in self._messages(body):
            if msg.get("id") == rid:
                if "error" in msg:
                    raise RuntimeError(f"{method}: {json.dumps(msg['error'])[:400]}")
                return msg.get("result") or {}
            if msg.get("method") and msg.get("id") is not None:
                self._post({"jsonrpc": "2.0", "id": msg["id"], "result": {}})
        raise RuntimeError(f"{method}: no response matched request {rid}")

    def connect(self):
        self.request("initialize", {
            "protocolVersion": "2025-03-26",
            "capabilities": {},
            "clientInfo": {"name": "mistral-bridge", "version": "1.0.0"},
        })
        try:
            self._post({"jsonrpc": "2.0", "method": "notifications/initialized"})
        except Exception as exc:  # noqa: BLE001 - notification is best effort
            log("initialized notification failed:", exc)
        tools = (self.request("tools/list", {}) or {}).get("tools") or []
        wanted = set(EXPOSED.values())
        self.tools = [t for t in tools if t.get("name") in wanted]
        missing = sorted(wanted - {t.get("name") for t in self.tools})
        if missing:
            log("upstream does not publish:", ", ".join(missing))
        log(f"studio: {len(self.tools)} of {len(tools)} upstream tools exposed")

    def call(self, upstream_name, arguments):
        return self.request("tools/call", {"name": upstream_name, "arguments": arguments or {}})


STUDIO = None
STUDIO_ERROR = None
STUDIO_ERROR_AT = 0.0
STUDIO_LOCK = threading.Lock()
# Upstream schemas, filled by the background prefetch. The tools/list path
# must never do network I/O: a hanging upstream would keep the whole MCP
# server from loading and take the Vibe agent tools down with it.
SCHEMAS = {}


def client():
    global STUDIO, STUDIO_ERROR, STUDIO_ERROR_AT
    with STUDIO_LOCK:
        if STUDIO is not None:
            return STUDIO
        if STUDIO_ERROR is not None and time.time() - STUDIO_ERROR_AT < RETRY_COOLDOWN:
            raise RuntimeError(STUDIO_ERROR)
        try:
            c = StudioMCP(URL, resolve_api_key())
            c.connect()
            STUDIO = c
            STUDIO_ERROR = None
            return STUDIO
        except Exception as exc:  # noqa: BLE001 - cached briefly, then retried
            STUDIO_ERROR = str(exc)
            STUDIO_ERROR_AT = time.time()
            raise


def _prefetch():
    try:
        c = client()
        with STUDIO_LOCK:
            for tool in c.tools:
                if tool.get("name"):
                    SCHEMAS[tool["name"]] = tool.get("inputSchema")
    except Exception as exc:  # noqa: BLE001 - the fallback schemas stay in place
        log("schema prefetch failed:", exc)


def start_prefetch():
    """Warm the upstream schemas in the background; never blocks the caller."""
    threading.Thread(target=_prefetch, daemon=True).start()


def status():
    """One-line reachability report for the bridge's health tool."""
    if SCHEMAS:
        return f"reachable ({len(SCHEMAS)} schemas loaded)"
    if STUDIO is not None:
        return "reachable (connected)"
    if STUDIO_ERROR:
        wait = max(0, int(RETRY_COOLDOWN - (time.time() - STUDIO_ERROR_AT)))
        return f"unreachable: {STUDIO_ERROR} (retrying in {wait}s)"
    return "not contacted yet"


def tool_definitions():
    """Tool schemas for the exposed slice; never performs network I/O."""
    out = []
    for name, upstream in EXPOSED.items():
        out.append({
            "name": name,
            "description": DESCRIPTIONS[name],
            "inputSchema": SCHEMAS.get(upstream) or FALLBACK_SCHEMAS[name],
        })
    return out


def call_tool(name, arguments):
    upstream = EXPOSED.get(name)
    if upstream is None:
        raise KeyError(name)
    result = client().call(upstream, arguments)
    blocks = []
    for block in result.get("content") or []:
        if block.get("type") == "text":
            blocks.append(block.get("text") or "")
        else:
            blocks.append(json.dumps(block))
    text = "\n".join(b for b in blocks if b).strip() or "(empty result)"
    return {"content": [{"type": "text", "text": text}], "isError": bool(result.get("isError"))}
