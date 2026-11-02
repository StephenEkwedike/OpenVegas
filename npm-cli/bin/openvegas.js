#!/usr/bin/env node
import {spawn} from "node:child_process";
import {constants} from "node:os";

const args = process.argv.slice(2);
// First interactive npx run uses existing onboarding, never an npm lifecycle hook.
if (!args.length && process.stdin.isTTY && process.stdout.isTTY && !process.env.CI) args.push("go");

const loading = args[0] === "go" && !args.includes("--dry-run") &&
  !args.includes("--verbose") && process.stderr.isTTY && !process.env.CI;
const notify = loading && process.platform !== "win32";
let timer;
let shown = false;
let label = "Starting OpenVegas...";
function clearLoading() {
  clearInterval(timer);
  if (shown) process.stderr.write("\r\x1b[2K");
  shown = false;
}
if (notify) {
  let frame = 0;
  const draw = () => {
    shown = true;
    process.stderr.write(`\r\x1b[34m${["|", "/", "-", "\\"][frame++ % 4]} ${label}\x1b[0m`);
  };
  draw();
  timer = setInterval(draw, 100);
} else if (loading) {
  process.stderr.write(label + "\n");
}

function run(cmd, cmdArgs, {probe = false} = {}) {
  return new Promise(resolve => {
    const env = {...process.env};
    delete env.OPENVEGAS_LAUNCH_NOTIFY_FD;
    if (notify && !probe) env.OPENVEGAS_LAUNCH_NOTIFY_FD = "3";
    const child = spawn(cmd, cmdArgs, {
      stdio: probe ? "ignore" : ["inherit", "inherit", "inherit", "pipe"],
      shell: false, env,
    });
    let error;
    let interrupted;
    const timeout = probe ? setTimeout(() => {
      error = new Error("Runtime probe timed out");
      child.kill("SIGKILL");
    }, 5000) : undefined;
    const signals = ["SIGINT", "SIGTERM"];
    const handlers = signals.map(signal => {
      const handler = () => { interrupted = signal; clearLoading(); child.kill(signal); };
      process.on(signal, handler);
      return handler;
    });
    if (!probe) {
      // pipx execs the app on POSIX, retaining this pipe and all three TTYs.
      // Acknowledge only after clearing so the spinner never erases a prompt.
      let ready = "";
      child.stdio[3].on("data", data => {
        ready = (ready + data.toString()).slice(0, 64);
        if (ready === "ready\n") {
          clearLoading();
          child.stdio[3].end("1");
        }
      });
      child.stdio[3].on("error", () => {});
    }
    child.once("error", err => { error = err; });
    child.once("close", (status, signal) => {
      clearTimeout(timeout);
      signals.forEach((name, i) => process.removeListener(name, handlers[i]));
      resolve({status, signal: interrupted || signal, error: interrupted ? undefined : error});
    });
  });
}

const missingRuntimeStatus = 3;
const runtimeProbe =
  "import importlib.util, sys; " +
  `sys.exit(0 if importlib.util.find_spec('openvegas') is not None else ${missingRuntimeStatus})`;

function exitWithResult(result) {
  clearLoading();
  if (result.error) {
    console.error("OpenVegas could not start the selected runtime.");
    process.exit(result.error.code === "ENOENT" ? 127 : 1);
  }
  if (result.signal) {
    const signalNumber = constants.signals[result.signal];
    process.exit(signalNumber ? 128 + signalNumber : 1);
  }
  process.exit(Number.isInteger(result.status) ? result.status : 1);
}

const pythonCandidates = [...new Set([
  process.env.OPENVEGAS_PYTHON,
  "python3",
  "python",
].filter(Boolean))];

for (const py of pythonCandidates) {
  // Do not import the application or replay a command to discover its runtime.
  const probe = await run(py, ["-B", "-c", runtimeProbe], {probe: true});
  if (probe.error?.code === "ENOENT" || (!probe.error && !probe.signal && probe.status === missingRuntimeStatus)) {
    continue;
  }
  if (probe.error || probe.signal || probe.status !== 0) {
    clearLoading();
    console.error("OpenVegas runtime availability check failed.");
    exitWithResult(probe);
  }
  exitWithResult(await run(py, ["-m", "openvegas.cli", ...args]));
}

label = "Preparing OpenVegas runtime...";
const runRes = await run("pipx", ["run", "--spec", "openvegas[audio]",
  ...(args.includes("--verbose") ? [] : ["--quiet"]), "openvegas", ...args]);
if (runRes.error?.code !== "ENOENT") exitWithResult(runRes);

clearLoading();
console.error(
  "OpenVegas CLI runtime not found. Install one of:\n" +
    "  pipx install openvegas[audio]\n" +
    "or\n" +
    "  pip install openvegas[audio]",
);
process.exit(1);
