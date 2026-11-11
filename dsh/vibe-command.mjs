import { execFile } from "node:child_process";
import { appendFileSync } from "node:fs";

/**
 * `/vibe` — a health command for the Mistral Vibe bridge.
 *
 * The bridge already answers `vibe_status`, but that reports the bridge's own
 * bookkeeping. This command runs `vibe_doctor.py`, which starts a fresh
 * `vibe-acp`, negotiates ACP, opens a session and runs one real prompt, so the
 * answer comes from the agent itself and not from a cached flag.
 *
 * The check is not free: the live prompt spends a little of the Vibe plan
 * allowance. `/vibe quick` skips that prompt and only checks that the agent
 * starts, negotiates and can open a session.
 *
 * @module vibe-command
 */
const name = "vibe-command";
const inject = ["commands"];

const USAGE = "Usage: /vibe [quick]";

/**
 * Record that the command registered, so a profile that fails to load the
 * plugin is visible outside the UI.
 * @param {string|null} traceFile - file to append to, when configured.
 */
function trace(traceFile, message) {
  if (!traceFile) {
    return;
  }
  try {
    appendFileSync(traceFile, `${new Date().toISOString()} ${message}\n`);
  } catch {
    // A trace is a convenience; it must never break the command.
  }
}

/**
 * Run the doctor and capture its exit status.
 * @param {string} python - interpreter that runs the doctor.
 * @param {string} doctor - path to vibe_doctor.py.
 * @param {string[]} extra - extra command-line arguments.
 * @param {number} timeoutMs - how long the doctor may take.
 * @returns {Promise<{code: number, stdout: string, stderr: string, error: Error|null}>}
 */
function runDoctor(python, doctor, extra, timeoutMs) {
  return new Promise((resolve) => {
    execFile(
      python,
      [doctor, ...extra],
      { timeout: timeoutMs, windowsHide: true, maxBuffer: 8 * 1024 * 1024 },
      (error, stdout, stderr) => {
        const code = error && typeof error.code === "number" ? error.code : error ? 1 : 0;
        resolve({ code, stdout: stdout || "", stderr: stderr || "", error: error || null });
      },
    );
  });
}

/**
 * Register `/vibe` for every composed human-command adapter.
 * @param ctx - context carrying the command registry.
 * @param config - plugin configuration.
 */
function apply(ctx, config = {}) {
  const python = config.python || (process.platform === "win32" ? "python" : "python3");
  const doctor = config.doctor;
  const timeoutMs = config.timeoutMs ?? 240000;
  const defaultQuick = config.quick === true;
  const traceFile = config.traceFile || null;
  const active = new Set();

  ctx.effect(function* () {
    yield async () => {
      await Promise.allSettled(active);
    };
    yield ctx.commands.register({
      name: "vibe",
      description: "Check that Mistral Vibe is alive and working (end-to-end probe)",
      input: { hint: "[quick]" },      handler: (invocation) => {
        const raw = (invocation.rawInput || "").trim();
        trace(traceFile, `handler invoked raw=${JSON.stringify(raw)}`);
        if (raw && !/^quick$/i.test(raw)) {
          return { kind: "error", text: USAGE };
        }
        const quick = defaultQuick || /^quick$/i.test(raw);
        const operation = runDoctor(python, doctor, quick ? ["--quick"] : [], timeoutMs)
          .then(({ code, stdout, stderr, error }) => {
            const body = (stdout || "").trim() || (stderr || "").trim();
            trace(traceFile, `doctor finished code=${code}`);
            if (!body) {
              return { kind: "error", text: `vibe check failed to run: ${error ? error.message : "no output"}` };
            }
            return code === 0 ? { kind: "success", text: body } : { kind: "error", text: body };
          });
        active.add(operation);
        const retire = () => {
          active.delete(operation);
        };
        operation.then(retire, retire);
        return operation;
      },
    });
    trace(traceFile, "registered /vibe");
  }, "vibe-command lifecycle");
}

export { apply, inject, name };
