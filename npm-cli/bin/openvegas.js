#!/usr/bin/env node
import {spawnSync} from "node:child_process";

const args = process.argv.slice(2);

function run(cmd, cmdArgs) {
  return spawnSync(cmd, cmdArgs, {stdio: "inherit"});
}

const pythonCandidates = [
  process.env.OPENVEGAS_PYTHON,
  "python3",
  "python",
].filter(Boolean);

for (const py of pythonCandidates) {
  const runRes = run(py, ["-m", "openvegas.cli", ...args]);
  if (runRes.status === 0) process.exit(0);
}

const runRes = run("pipx", ["run", "--spec", "openvegas[audio]", "openvegas", ...args]);
if (runRes.status === 0) process.exit(0);

console.error(
  "OpenVegas CLI runtime not found. Install one of:\n" +
    "  pipx install openvegas[audio]\n" +
    "or\n" +
    "  pip install openvegas[audio]",
);
process.exit(1);
