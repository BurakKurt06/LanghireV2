"""Run the Windows CI workflow (.github/workflows/test.yml) on this computer, step by step. Free.

GitHub-hosted runners cost money on a private repository once its free minutes are used up; this
runs the same job here instead. It reads test.yml itself, so it never drifts from CI:

- every `run:` step of the `test` job, in order, with the step's `env`, `working-directory` and
  `timeout-minutes`; each line of a multi-line step stops the step when it fails, as on GitHub;
- `uses:` steps (checkout, setup-node, setup-python) are skipped: this checkout and the locally
  installed Node, uv and Python 3.13 are used instead;
- `pip install uv` is skipped (uv is already here; it would change the system Python);
- `${{ matrix.target }}` is the matrix target; `${{ secrets.* }}` are empty, so provider checks that
  need a paid key skip themselves, as they do on GitHub without the secret.

Usage:  python scripts/run-ci-locally.py [--from "<step name>"] [--only "<step name>"] [--log tmp/ci-local.log]
Exit code 0 only when every step passed. The summary lists each step with its time.
"""
import argparse
import os
import re
import subprocess
import sys
import time
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
WORKFLOW = ROOT / ".github" / "workflows" / "test.yml"
SKIP_LINES = (re.compile(r"^\s*pip install uv\s*$"),)


def expand(text: str, matrix: dict) -> str:
    text = re.sub(r"\$\{\{\s*matrix\.(\w+)\s*\}\}", lambda m: str(matrix.get(m.group(1), "")), text)
    return re.sub(r"\$\{\{\s*secrets\.\w+\s*\}\}", "", text)


def run_line(line: str, cwd: Path, env: dict, timeout: float, log) -> int:
    """One command line in Windows PowerShell (the runner's default shell is PowerShell too)."""
    command = f"$ErrorActionPreference = 'Stop'; {line}; exit $LASTEXITCODE"
    process = subprocess.Popen(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", command],
                               cwd=cwd, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    started = time.monotonic()
    try:
        for raw in process.stdout:
            text = raw.decode("utf-8", errors="replace")
            log.write(text)
            log.flush()
            if time.monotonic() - started > timeout:
                raise subprocess.TimeoutExpired(line, timeout)
        return process.wait(timeout=max(1, timeout - (time.monotonic() - started)))
    except subprocess.TimeoutExpired:
        subprocess.run(["taskkill", "/T", "/F", "/PID", str(process.pid)], capture_output=True)
        log.write(f"\n[run-ci-locally] step timed out after {timeout / 60:.0f} minutes\n")
        return 124


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--from", dest="start", help="start at this step (name as in test.yml)")
    parser.add_argument("--only", help="run only this step")
    parser.add_argument("--log", default=str(ROOT / "tmp" / "ci-local.log"), help="full output of every step (tmp/ is git-ignored)")
    args = parser.parse_args()

    workflow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    job = workflow["jobs"]["test"]
    matrix = (job.get("strategy", {}).get("matrix", {}).get("include") or [{}])[0]
    steps = [s for s in job["steps"] if "run" in s]
    if args.start:
        names = [s.get("name") for s in steps]
        if args.start not in names:
            raise SystemExit(f"No step named {args.start!r}. Steps: {names}")
        steps = steps[names.index(args.start):]
    if args.only:
        steps = [s for s in steps if s.get("name") == args.only]

    results = []
    with open(args.log, "w", encoding="utf-8") as log:
        for step in steps:
            name = step.get("name", "(unnamed)")
            env = {**os.environ, **{k: expand(str(v), matrix) for k, v in (step.get("env") or {}).items()}}
            cwd = ROOT / step.get("working-directory", ".")
            timeout = float(step.get("timeout-minutes", job.get("timeout-minutes", 120))) * 60
            lines = [expand(line, matrix) for line in str(step["run"]).splitlines() if line.strip()]
            lines = [line for line in lines if not any(p.match(line) for p in SKIP_LINES)]
            print(f"▶ {name}", flush=True)
            log.write(f"\n{'=' * 78}\n▶ {name}\n{'=' * 78}\n")
            began, code = time.monotonic(), 0
            for line in lines:
                log.write(f"$ {line}\n")
                code = run_line(line, cwd, env, timeout - (time.monotonic() - began), log)
                if code != 0:
                    break
            seconds = time.monotonic() - began
            results.append((name, code, seconds))
            print(f"  {'passed' if code == 0 else f'FAILED (exit {code})'} in {seconds / 60:.1f} min", flush=True)
            if code != 0:
                break           # like GitHub: the job stops at the first failed step
    print("\nSummary (full output in " + args.log + "):")
    for name, code, seconds in results:
        print(f"  {'✔' if code == 0 else '✘'} {name}  ({seconds / 60:.1f} min)")
    skipped = len([s for s in job["steps"] if "run" in s]) - len(results)
    if skipped and not args.only:
        print(f"  ({skipped} step(s) not run: an earlier step failed or --from was given)")
    return 0 if results and all(code == 0 for _, code, _ in results) else 1


if __name__ == "__main__":
    sys.exit(main())
