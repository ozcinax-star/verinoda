#!/usr/bin/env node
// `npx verinoda ...` / `npm i -g verinoda`: runs the Python package `verinoda` of the same version.
//
// Verinoda is a Python program. This wrapper finds a way to run the matching PyPI release, in order:
//   1. uvx       (uv's tool runner: downloads a suitable Python itself, caches the environment)
//   2. pipx run  (the same with pipx)
//   3. a private virtual environment made with a Python 3.10+ found on PATH, kept per version in the
//      user's cache folder (created once, reused afterwards)
// It passes every argument through, keeps stdin/stdout/stderr attached (so `npx -y verinoda mcp serve`
// works as an MCP stdio server) and exits with the program's exit code.
// VERINODA_NPM_RUNNER=uvx|pipx|venv forces one route; VERINODA_PYPI_SPEC overrides the requirement.
"use strict";

const { spawnSync } = require("child_process");
const fs = require("fs");
const os = require("os");
const path = require("path");

const pkg = require("../package.json");
const WIN = process.platform === "win32";

// npm "0.1.0-alpha.1" -> PyPI "0.1.0a1"; "0.1.0" stays; "0.1.0-rc.2" -> "0.1.0rc2"
function pypiVersion(v) {
  const m = /^(\d+\.\d+\.\d+)(?:-(alpha|beta|rc)\.(\d+))?$/.exec(v);
  if (!m) return v;
  if (!m[2]) return m[1];
  return m[1] + { alpha: "a", beta: "b", rc: "rc" }[m[2]] + m[3];
}

const SPEC = process.env.VERINODA_PYPI_SPEC || `verinoda[precise]==${pypiVersion(pkg.version)}`;
const ARGS = process.argv.slice(2);

function which(cmd) {
  const exts = WIN ? (process.env.PATHEXT || ".EXE;.CMD;.BAT").split(";") : [""];
  for (const dir of (process.env.PATH || "").split(path.delimiter)) {
    if (!dir) continue;
    for (const ext of exts) {
      const p = path.join(dir, cmd + ext.toLowerCase());
      const q = path.join(dir, cmd + ext);
      for (const c of [p, q]) {
        try {
          if (fs.statSync(c).isFile()) return c;
        } catch (_) { /* not here */ }
      }
    }
  }
  return null;
}

function run(cmd, args, opts = {}) {
  const r = spawnSync(cmd, args, { stdio: "inherit", windowsHide: true, ...opts });
  if (r.error) throw r.error;
  if (r.signal) process.kill(process.pid, r.signal);
  return r.status === null ? 1 : r.status;
}

function quiet(cmd, args) {
  const r = spawnSync(cmd, args, { encoding: "utf8", windowsHide: true });
  return r.status === 0 ? (r.stdout || "").trim() : null;
}

function viaUvx() {
  const uvx = which("uvx");
  if (!uvx) return null;
  return run(uvx, ["--quiet", "--from", SPEC, "verinoda", ...ARGS]);
}

function viaPipx() {
  const pipx = which("pipx");
  if (!pipx) return null;
  return run(pipx, ["run", "--quiet", "--spec", SPEC, "verinoda", ...ARGS]);
}

function findPython() {
  const cands = WIN ? [["py", ["-3"]], ["python", []], ["python3", []]] : [["python3", []], ["python", []]];
  for (const [name, pre] of cands) {
    const exe = which(name);
    if (!exe) continue;
    const ver = quiet(exe, [...pre, "-c", "import sys;print('%d.%d' % sys.version_info[:2])"]);
    if (!ver) continue;
    const [maj, min] = ver.split(".").map(Number);
    if (maj === 3 && min >= 10) return [exe, pre];
  }
  return null;
}

function cacheDir() {
  if (process.env.VERINODA_NPM_CACHE) return process.env.VERINODA_NPM_CACHE;
  if (WIN) return path.join(process.env.LOCALAPPDATA || path.join(os.homedir(), "AppData", "Local"), "verinoda", "npm");
  if (process.platform === "darwin") return path.join(os.homedir(), "Library", "Caches", "verinoda", "npm");
  return path.join(process.env.XDG_CACHE_HOME || path.join(os.homedir(), ".cache"), "verinoda", "npm");
}

function viaVenv() {
  const py = findPython();
  if (!py) return null;
  const env = path.join(cacheDir(), pypiVersion(pkg.version));
  const bin = WIN ? path.join(env, "Scripts") : path.join(env, "bin");
  const exe = path.join(bin, WIN ? "verinoda.exe" : "verinoda");
  if (!fs.existsSync(exe)) {
    process.stderr.write(`verinoda (npm): installing ${SPEC} into ${env} (once)\n`);
    fs.mkdirSync(path.dirname(env), { recursive: true });
    const [pyExe, pre] = py;
    if (run(pyExe, [...pre, "-m", "venv", env], { stdio: ["ignore", 2, 2] }) !== 0) return null;
    const pip = path.join(bin, WIN ? "python.exe" : "python");
    const code = run(pip, ["-m", "pip", "install", "--quiet", "--disable-pip-version-check", SPEC],
      { stdio: ["ignore", 2, 2] });  // stdout stays clean: an MCP client may be reading it
    if (code !== 0 || !fs.existsSync(exe)) {
      fs.rmSync(env, { recursive: true, force: true });
      return null;
    }
  }
  return run(exe, ARGS);
}

function main() {
  const forced = (process.env.VERINODA_NPM_RUNNER || "").toLowerCase();
  const routes = { uvx: viaUvx, pipx: viaPipx, venv: viaVenv };
  const order = forced && routes[forced] ? [routes[forced]] : [viaUvx, viaPipx, viaVenv];
  for (const route of order) {
    const code = route();
    if (code !== null) process.exit(code);
  }
  process.stderr.write(
    "verinoda (npm): no way to run the Python package was found.\n" +
    "Install uv (recommended), then run the same command again:\n" +
    (WIN ? "  winget install --id=astral-sh.uv -e\n"
         : "  curl -LsSf https://astral.sh/uv/install.sh | sh    (or: brew install uv)\n") +
    "Or install Python 3.10+ (it is used to make a private environment), or pipx.\n");
  process.exit(127);
}

main();
