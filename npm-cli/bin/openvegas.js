#!/usr/bin/env node
import {spawnSync} from "node:child_process";
import {constants} from "node:os";

const args = process.argv.slice(2);

function run(cmd, cmdArgs) {
  return spawnSync(cmd, cmdArgs, {stdio: "inherit"});
}

const missingRuntimeStatus = 3;
const runtimeProbe =
  "import importlib.util, sys; " +
  `sys.exit(0 if importlib.util.find_spec('openvegas') is not None else ${missingRuntimeStatus})`;

function exitWithResult(result) {
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
  const probe = spawnSync(py, ["-B", "-c", runtimeProbe], {
    stdio: "ignore",
    timeout: 5000,
  });
  if (probe.error?.code === "ENOENT" || (!probe.error && probe.status === missingRuntimeStatus)) {
    continue;
  }
  if (probe.error || probe.status !== 0) {
    console.error("OpenVegas runtime availability check failed.");
    exitWithResult(probe);
  }
  exitWithResult(run(py, ["-m", "openvegas.cli", ...args]));
}

const runRes = run("pipx", ["run", "--spec", "openvegas[audio]", "openvegas", ...args]);
if (runRes.error?.code !== "ENOENT") exitWithResult(runRes);

console.error(
  "OpenVegas CLI runtime not found. Install one of:\n" +
    "  pipx install openvegas[audio]\n" +
    "or\n" +
    "  pip install openvegas[audio]",
);
process.exit(1);
