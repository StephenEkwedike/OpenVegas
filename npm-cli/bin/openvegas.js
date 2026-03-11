#!/usr/bin/env node
import {spawnSync} from "node:child_process";

const args = process.argv.slice(2);

let run = spawnSync("openvegas", args, {stdio: "inherit"});
if (run.status === 0) process.exit(0);

run = spawnSync("pipx", ["run", "openvegas", ...args], {stdio: "inherit"});
if (run.status === 0) process.exit(0);

console.error(
  "OpenVegas CLI not found. Install one of:\n" +
    "  pipx install openvegas\n" +
    "or\n" +
    "  pip install openvegas",
);
process.exit(1);
