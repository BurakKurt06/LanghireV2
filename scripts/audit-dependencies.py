"""Audit pinned Python and active Windows Cargo packages with OSV.

No advisory is silently discarded: narrow, expiring exceptions are included in
the report. Browser-use overrides are deliberate and validated by the suites.
Run npm audit separately for both package locks (also done in CI).
"""
import argparse
from datetime import date, datetime, timezone
import json
import re
from pathlib import Path
import subprocess
import tomllib

import httpx

ROOT = Path(__file__).resolve().parent.parent
TARGET = "x86_64-pc-windows-msvc"


def locked_packages():
    queries, runtime = [], set()
    for p in tomllib.loads((ROOT / "uv.lock").read_text(encoding="utf-8"))["package"]:
        if p.get("source", {}).get("registry"):
            queries.append({"package": {"name": p["name"], "ecosystem": "PyPI"}, "version": p["version"]})
    metadata = json.loads(subprocess.check_output(["cargo", "metadata", "--locked", "--format-version", "1",
        "--filter-platform", TARGET], cwd=ROOT / "src-tauri", text=True, encoding="utf-8"))
    def tree(edges):
        # Cargo metadata includes optional packages not built with the selected
        # features. cargo tree is the active feature graph, not the whole lock.
        lines = subprocess.check_output(["cargo", "tree", "--locked", "--target", TARGET,
            "--prefix", "none", "--format", "{p}", "-e", edges], cwd=ROOT / "src-tauri", text=True, encoding="utf-8")
        return {match.groups() for line in lines.splitlines()
                if (match := re.match(r"^(\S+) v(\S+)", line))}
    active = tree("normal,build")
    # Proc-macro crates (and what they use) run inside the compiler at build time and are not
    # linked into the app, so they are not runtime packages for build_only exceptions.
    runtime_versions = tree("normal,no-proc-macro")
    for p in metadata["packages"]:
        if (p["name"], p["version"]) in active and (p.get("source") or "").startswith("registry+"):
            queries.append({"package": {"name": p["name"], "ecosystem": "crates.io"}, "version": p["version"]})
    return queries, runtime_versions


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", type=Path, default=ROOT / "security/dependency-audit.json")
    args = parser.parse_args()
    queries, runtime = locked_packages()
    accepted = ROOT / "security/dependency-exceptions.json"
    # The public copy leaves out security/: without the file no advisory is accepted.
    exceptions = json.loads(accepted.read_text(encoding="utf-8")) if accepted.exists() else []
    findings = []
    with httpx.Client(timeout=45, trust_env=False) as client:
        result = client.post("https://api.osv.dev/v1/querybatch", json={"queries": queries}).raise_for_status().json()["results"]
        if len(result) != len(queries):
            raise RuntimeError("Incomplete OSV response")
        for package, matches in zip(queries, result):
            for item in matches.get("vulns", []):
                advisory = client.get("https://api.osv.dev/v1/vulns/" + item["id"]).raise_for_status().json()
                if advisory.get("withdrawn"):
                    continue
                identity = {advisory["id"], *advisory.get("aliases", [])}
                reason = None
                for exception in exceptions:
                    if (exception["ecosystem"] == package["package"]["ecosystem"]
                        and exception["package"] == package["package"]["name"]
                        and exception["version"] == package["version"]
                        and exception["advisory"] in identity
                        and date.fromisoformat(exception["expires"]) >= datetime.now(timezone.utc).date()
                        and (not exception.get("build_only") or (exception["package"], exception["version"]) not in runtime)):
                        reason = exception["reason"]
                        break
                findings.append({**package, "id": advisory["id"], "aliases": advisory.get("aliases", []),
                    "summary": advisory.get("summary", ""), "exception": reason,
                    "references": [r["url"] for r in advisory.get("references", []) if r["type"] == "ADVISORY"]})
    report = {"checked_at": datetime.now(timezone.utc).isoformat(), "cargo_target": TARGET,
              "packages_checked": len(queries), "findings": findings,
              "unaccepted": sum(f["exception"] is None for f in findings)}
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"packages_checked": len(queries), "unaccepted": report["unaccepted"],
                      "findings": [{"name": f["package"]["name"], "id": f["id"], "accepted": f["exception"] is not None} for f in findings]}))
    return 1 if report["unaccepted"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
