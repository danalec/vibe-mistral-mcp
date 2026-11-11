# vibe-mistral-mcp

One MCP stdio server with both halves of Mistral in it:

1. **Vibe agent** — delegate coding work to Mistral Vibe through ACP (`vibe-acp`).
2. **Studio lookups** — a five-tool slice of Mistral's Studio MCP server
   (`https://api.mistral.ai/mcp`): budgets, docs search, Skills catalogue.

They are one process and one configuration entry because they share nothing but the
provider: the Vibe half talks ACP to a local agent (model inference on the Vibe plan), the
Studio half talks Streamable HTTP to `api.mistral.ai` (workspace metadata). Merging them
keeps a single mount, a single repo and a single place to see failures — without pretending
one replaces the other.

Mistral Vibe's subscription credential created by `vibe-acp --setup` is a chat-plan
credential, not an `api.mistral.ai` API key, so it cannot back an OpenAI-compatible
provider route. What it can back is an ACP agent: this bridge speaks MCP to the harness
and ACP v1 ([agentclientprotocol.com](https://agentclientprotocol.com)) to `vibe-acp.exe`,
so a model can hand a self-contained task to Mistral Large 4 (or another Mistral-hosted
model) running with Vibe's own file and shell tools.

Stdlib only, three Python modules (`vibe_mcp.py`, `studio.py`, `vibe_doctor.py`) and one
ES module for the harness command (`dsh/vibe-command.mjs`), no dependencies.

> This project talks to tools you already own and are signed in to: it never holds a
> credential. The Mistral API key is read from `MISTRAL_API_KEY` or from the harness
> credential store; the Vibe plan credential lives in `vibe-acp` itself.

## Tools

Vibe agent (ACP):

| Tool | Purpose |
|---|---|
| `vibe_start` | Start a task in the background; returns a `job_id` immediately |
| `vibe_poll` | Check a job (`wait_s` up to 600); returns the answer when done |
| `vibe_jobs` | List recent jobs (state, elapsed, model, cwd) |
| `vibe_prompt` | Blocking call, kept for quick checks |
| `vibe_options` | Models, thinking levels and approval modes Vibe offers |
| `vibe_reset` | Cancel a running prompt, otherwise close the ACP session/process |
| `vibe_status` | Health in one call: process, session, prompt idleness, job counts, Studio reachability |

Studio workspace (whitelisted slice of `api.mistral.ai/mcp`, 5 of its 133 tools — mounting
it whole would add every schema to every request):

| Tool | Upstream | Purpose |
|---|---|---|
| `studio_usage_limits` | `get_usage_limits` | plan, API and Vibe Code budgets, remaining credit |
| `studio_search_docs` | `search_mistral_docs` | search docs.mistral.ai |
| `studio_read_doc` | `read_mistral_docs_page` | one documentation page as Markdown |
| `studio_list_skills` | `list_skills` | the workspace Skill catalogue |
| `studio_read_skill` | `read_skill` | one Skill, including its files |

A Vibe job keeps running in the bridge process while the caller does other work; the last
20 jobs are retained. Jobs are serialized because ACP allows one prompt per session. Studio
calls are independent of the Vibe session and answer while a prompt is running.

## Requirements

- Python 3.9+ (the bridge and the doctor are stdlib only).
- [Mistral Vibe](https://docs.mistral.ai/vibe/code/cli/install-setup) installed and signed in
  (`vibe-acp --setup`). The bridge looks for `vibe-acp` on `PATH` first, then in the usual
  install locations of each platform, and never handles that credential.
- Linux and Windows are both exercised: the suite runs on CachyOS (Python 3.14) and on
  Windows (3.12), and `dsh/vibe-command.mjs` needs Node only for the `/vibe` command.

## Configuration

Run it with any Python 3.9+ interpreter. Environment:

| Variable | Default | Meaning |
|---|---|---|
| `VIBE_ACP_EXE` | `vibe-acp` on `PATH`, else the platform install location | ACP agent executable, or a command with arguments |
| `VIBE_ACP_CWD` | user home | Default working directory |
| `VIBE_ACP_MODEL` | *(unset: Vibe's own choice)* | Model applied when a call omits one, using the inference-API id |
| `VIBE_ACP_THINKING` | `high` | Thinking level applied when omitted |
| `VIBE_ACP_MODE` | `accept-edits` | Approval mode applied when omitted |
| `VIBE_ACP_TIMEOUT` | `2100000` | Default/maximum seconds per prompt |
| `VIBE_ACP_IDLE_TIMEOUT` | `1200` | Cancel a prompt that emits no ACP update for this long |
| `VIBE_ACP_TOOL_IDLE_TIMEOUT` | `900` | Silence budget while a tool call is in flight |
| `VIBE_ACP_CANCEL_GRACE` | `30` | Seconds to wait for a cancel to settle before killing the process |
| `MISTRAL_API_KEY` | `$DSH_HOME/.credentials.yaml` or `~/.dsh/.credentials.yaml` | Bearer token for the Studio half |
| `MISTRAL_MCP_URL` | `https://api.mistral.ai/mcp` | Studio MCP endpoint |
| `MISTRAL_MCP_TIMEOUT` | `90` | Studio request timeout in seconds |
| `MISTRAL_STUDIO_RETRY_COOLDOWN` | `60` | Seconds before a failed Studio connection is retried |

The Studio key is read from `MISTRAL_API_KEY` when set, otherwise from the harness
credential store. Reading the store matters under DeepSeek Harness: its MCP client scrubs
environment names matching `KEY|TOKEN|PASSWORD|SECRET`, so an exported `MISTRAL_API_KEY`
never reaches this child process.

Leave `VIBE_ACP_MODEL` unset unless you know your account serves the model: asking for a
model the plan does not include makes Vibe fall back to another one *silently*, and the
fallback rejects thinking levels the first model accepted. The bridge clamps the level to
what the selected model offers and reports the substitution, but letting Vibe choose avoids
the mismatch altogether.

### Model and thinking levels belong to the account

Vibe decides which models exist and which thinking levels each one accepts; the inference
API has the final word and rejects anything else with a 400 that fails the whole prompt.
`vibe_options` reports both live.

**A model id is the one the inference API publishes, not the label in the picker.** Vibe's
built-in catalogue ships a stale id for Mistral Large 4 (`mistral-large-4-0-3`); the API
answers `400 Invalid model` for it and the agent then serves a different model without
saying so — the prompt succeeds, the answer is not from the model you picked. The docs list
`mistral-large-4` and `mistral-large-4-0`. Define the preset yourself so the correct id is
what gets sent:

```toml
allowed_models = ["mistral-large-4", "mistral-medium-3.5", "zai-glm-5-3", "zai-glm-5-3-flash"]
active_model = "mistral-large-4"

[[models]]
name = "mistral-large-4"     # the id the inference API accepts
provider = "mistral"
alias = "mistral-large-4"
thinking_levels = ["low", "medium", "high"]
max_context_length = 1048576
supports_images = true
```

`allowed_models` matters: it filters what the picker offers, matched against the names sent
to the API, so listing only the ids you configured also hides the broken built-in entry.

The bridge does not hide this. It reports the model the session actually selected, clamps a
thinking level the selected model does not offer, and `tests/probe_served_model.py` compares
that report against Vibe's own log, which is where the silent substitution is recorded.

Example DSH profile patch (`cordis.patch.yml`):

```yaml
- insert:
    - id: mcp-vibe
      name: '@deepseek-ai/dsh-mcp-client'
      config:
        serverName: vibe
        transport: stdio
        command: C:\path\to\python.exe
        args:
          - C:\path\to\vibe_mcp.py
        env:
          VIBE_ACP_EXE: C:\Users\you\AppData\Local\Microsoft\WinGet\Links\vibe-acp.exe
          VIBE_ACP_MODEL: mistral-large-4-0-3
          VIBE_ACP_THINKING: high
          VIBE_ACP_MODE: accept-edits
        toolCallTimeoutMs: 2147483646   # Node setTimeout ceiling; default 60 s is far too low
```

Reaching the ACP protocol version: `initialize` (protocolVersion 1), `session/new`
(cwd + MCP servers), `session/set_config_option` (model/thinking/mode), `session/prompt`,
`session/cancel`; output is read from `session/update` notifications
(`agent_message_chunk`, `tool_call`, `tool_call_update`).

## Notes

- `vibe-acp` must already be signed in (`vibe-acp --setup`); this bridge never handles
  the token.
- Tool-permission requests from Vibe are answered with the single-call allow option when
  there is one; a persistent `allow_always` grant is only used when it is the only allow
  option, so Vibe edits files and runs commands in the given `cwd` without a standing grant.
- A prompt that times out is cancelled rather than left running unattended; a dead
  `vibe-acp` process is restarted once and the prompt replayed.
- **A wedged prompt cannot block the bridge forever.** Prompts are serialized, so a silent
  hang used to queue every later job indefinitely. Now a prompt that sends no `session/update`
  for `VIBE_ACP_IDLE_TIMEOUT` is cancelled; if `session/cancel` does not settle within
  `VIBE_ACP_CANCEL_GRACE`, `vibe-acp` is killed and the job fails with a clear reason.
  `vibe_reset` cancels queued jobs and escalates the same way.
- Job state is `queued` until it actually owns the prompt slot, then `running`; `vibe_poll`
  reports how long the running prompt has been silent and what it last touched.
- **One prompt at a time, and that is visible.** `vibe_prompt` refuses while another prompt
  owns the slot instead of blocking for hours behind the harness' ~24-day tool timeout; use
  `vibe_start` + `vibe_poll` for background work.
- **A cancelled prompt leaves its ACP session unusable**, so the next prompt gets a fresh
  process instead of inheriting the cancellation. The same happens when the working
  directory changes.
- The process reader is generation-stamped: a reader of a replaced process can no longer
  mark the live one as dead, which used to break delegation right after a restart.
- Mistral may answer `Model 'X' is not available — running 'Y'` after a model selection:
  Vibe falls back silently, and a thinking level the fallback does not accept surfaces as a
  400 from the prompt. Keep `thinking: high`, which both Mistral Large 4 and Medium 3.5 accept.
- The Studio schemas are fetched in the background, so `tools/list` answers immediately even
  when `api.mistral.ai` is unreachable — the Vibe tools stay available either way. A failed
  Studio connection is retried after `MISTRAL_STUDIO_RETRY_COOLDOWN` seconds.
- Chunk accumulation, cancellation and job state are kept in memory; restarting the
  bridge drops unfinished jobs.

## The `/vibe` command

`dsh/vibe-command.mjs` registers a `/vibe` slash command in the DeepSeek Harness chat.
It runs `vibe_doctor.py`, which starts a **fresh** `vibe-acp`, negotiates ACP, opens a
session and runs **one real prompt**, then checks the Studio half and the remaining plan
budget. The answer therefore comes from the agent itself, not from a cached flag.

```
/vibe          full check, one live prompt
/vibe quick    skip the prompt (spends no plan tokens)
```

Enable it in the profile patch:

```yaml
- id: vibe-command
  name: "file:///C:/path/to/vibe-mistral-mcp/dsh/vibe-command.mjs"
  config:
    doctor: C:\path\to\vibe-mistral-mcp\vibe_doctor.py
    python: C:\path\to\python.exe
    timeoutMs: 240000
    traceFile: C:\path\to\vibe-mistral-mcp\vibe-command.log   # optional
```

The doctor also runs on its own:

```
python vibe_doctor.py           # full check
python vibe_doctor.py --quick   # no live prompt
python vibe_doctor.py --json    # machine-readable
```

It exits 0 when every check passed.

## Tests

`tests/regress.py` drives the server over MCP stdio exactly like the harness does and
checks the concurrency and failure paths (cold start, process restart, a prompt running
while other tools are called, cancel and restart, unreachable Studio upstream):

```
python tests/regress.py
```

It needs no Mistral account: `tests/fake_vibe_acp.py` is an ACP stub that answers like the
agent, including a cancel that interrupts a long tool run, so the suite is hermetic and
runs on any machine:

```
VIBE_TEST_ACP_EXE="python3 tests/fake_vibe_acp.py" python3 tests/regress.py
```

The interpreter, the server path and the scratch directory come from the environment
(`VIBE_TEST_PYTHON`, `VIBE_TEST_SERVER`, `VIBE_TEST_WORK`), so no path is hardcoded.

Against a real account, these probes answer "what does my plan actually serve?":

| Probe | Question |
|---|---|
| `tests/probe_models.py` | which models are selectable, and does one silently fall back to another |
| `tests/probe_served_model.py` | did the model I selected really answer, or did Vibe substitute one |
| `tests/probe_large4_ids.py` | which inference-API id for Large 4 the API actually accepts |
| `tests/probe_thinking.py` | which thinking levels each model really accepts |
| `tests/probe_clamp.py` | does the bridge substitute an unsupported level instead of failing |
| `tests/probe_resolver.py` | how the ACP executable is located on this platform |

They spend plan tokens, unlike the hermetic suite.

## Contributing

Small, focused pull requests are welcome. `python tests/regress.py` must pass, and a new
behaviour needs a check in it. Please do not commit local paths, credentials or anything
that names a private host.

## License

Dual license with a commercial option — see [LICENSE](LICENSE):

- **MIT** ([LICENSE-MIT](LICENSE-MIT)) or
- **Apache License 2.0** ([LICENSE-APACHE-2.0](LICENSE-APACHE-2.0)), or
- **Commercial** ([LICENSE-COMMERCIAL.md](LICENSE-COMMERCIAL.md)), for organizations that
  cannot adopt an open-source license or need terms the open-source ones do not offer.

Pick one; the open-source options need no purchase or approval. The names of the upstream
projects this project talks to (Mistral, Vibe, DeepSeek Harness, ACP) belong to their
owners and are used only descriptively.

## Security

See [SECURITY.md](SECURITY.md). The project holds no credentials of its own.

