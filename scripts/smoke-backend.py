"""Windows frozen-backend security smoke test; fake token and isolated temporary data.

Usage: uv run --locked python scripts/smoke-backend.py --binary <sidecar.exe>
Leaves its temporary log/data folder for diagnosis; only its own processes stop.
No accounts, external site requests or AI provider calls are made.
"""
import argparse
import json, os, socket, subprocess, tempfile, time, urllib.request, urllib.error
from pathlib import Path
import psutil

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--binary", type=Path, required=True)
args = parser.parse_args()
binary = args.binary.resolve(strict=True)
home = Path(tempfile.mkdtemp(prefix="langhire-packaged-smoke-"))
print("Isolated smoke data/log:", home, flush=True)
folder = home / "AppData/Roaming/langhirev2"
folder.mkdir(parents=True, exist_ok=True)
(folder / "settings.json").write_text(json.dumps({"job_collection_enabled": False, "inbox_check_enabled": False, "sensitive_data": {}}))
(folder / "llm_settings.json").write_text(json.dumps({"provider": "", "openai": {"api_key": ""}}))
# Seed locally: /jobs/add would schedule a posting fetch, which this smoke must never do.
note_urls = ["https://jobs.example.test/one", "https://jobs.example.test/two"]
(folder / "jobs.json").write_text(json.dumps({
    url: {"title": "Isolated note test", "status": "failed", "user_note": ""}
    for url in note_urls
}))
env = os.environ.copy()
env.update(HOME=str(home), USERPROFILE=str(home), APPDATA=str(home / "AppData/Roaming"), JOB_APPLICANT_TOKEN="codex-smoke-dummy-token")
# Keep LOCALAPPDATA for read-only discovery of the already-installed Chromium.
cache = Path(env["LOCALAPPDATA"]) / "ms-playwright"
assert any(cache.glob("chromium-*/chrome-win64/chrome.exe")), "Expected installed Chromium; refuse to trigger a download"
with socket.socket() as sock:
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
base = f"http://127.0.0.1:{port}"

def request(path, method="GET", body=None, token=True, origin=None):
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = "Bearer codex-smoke-dummy-token"
    if origin:
        headers["Origin"] = origin
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(base + path, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=5) as response:
            return response.status, json.load(response)
    except urllib.error.HTTPError as error:
        return error.code, json.load(error)

log = open(home / "process.log", "wb")
proc = subprocess.Popen([str(binary), str(port)], env=env, cwd=home, stdout=log, stderr=subprocess.STDOUT, creationflags=subprocess.CREATE_NO_WINDOW)
checks = []
try:
    deadline = time.monotonic() + 70
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            raise RuntimeError("Packaged backend exited before startup")
        try:
            status, health = request("/health", token=False)
            if status == 200:
                break
        except (OSError, TimeoutError):
            pass
        time.sleep(0.5)
    else:
        raise RuntimeError("Packaged backend startup timed out")
    assert health["frozen"] is True and health["checks"]["database"] is True
    assert health["checks"]["worker_running"] is False and health["checks"]["llm_configured"] is False
    checks.append("frozen startup and isolated database")
    assert request("/settings", token=False)[0] == 401
    checks.append("unauthenticated request rejected")
    assert request("/health", token=False, origin="https://untrusted.example")[0] == 403
    checks.append("untrusted origin rejected")
    assert request("/settings")[0] == 200
    checks.append("authenticated settings read")
    assert request("/plugins")[0] == 200
    checks.append("plugin registry imports")
    assert request("/countries")[0] == 200
    checks.append("countries endpoint")
    def saved_notes():
        status, jobs = request("/jobs")
        assert status == 200
        return {job["url"]: job.get("user_note", "") for job in jobs}
    long_note = "Only this job: " + "x" * 9985
    assert len(long_note) == 10000
    assert request("/jobs/note", method="PUT", body={"url": note_urls[0], "note": long_note})[0] == 200
    assert request("/jobs/note", method="PUT", body={"url": note_urls[1], "note": "Second job only"})[0] == 200
    assert saved_notes() == {note_urls[0]: long_note, note_urls[1]: "Second job only"}
    checks.append("10,000-character retry note round-trips and stays scoped to its job")
    for bad_note in (long_note + "x", None, {"instruction": "invalid"}):
        assert request("/jobs/note", method="PUT", body={"url": note_urls[0], "note": bad_note})[0] == 400
    assert request("/jobs/note", method="PUT", body={"url": "https://jobs.example.test/missing", "note": "no"})[0] == 404
    assert saved_notes() == {note_urls[0]: long_note, note_urls[1]: "Second job only"}
    checks.append("invalid or unknown-job note rejected without overwriting saved notes")
    assert request("/jobs/note", method="PUT", body={"url": note_urls[0], "note": ""}, token=False)[0] == 401
    assert saved_notes()[note_urls[0]] == long_note
    checks.append("unauthenticated retry-note mutation rejected")
    assert request("/jobs/note", method="PUT", body={"url": note_urls[0], "note": ""})[0] == 200
    assert saved_notes() == {note_urls[0]: "", note_urls[1]: "Second job only"}
    checks.append("clearing a retry note leaves the other job untouched")
    assert request("/jobs/collect", method="POST", body={"title": "Dummy job", "max_jobs": 1, "source": "linkedin"})[0] == 409
    assert request("/jobs/collect/status")[1].get("running") is False
    checks.append("collection disabled without starting browser")
    assert (folder / ".api_token").read_text().strip() == "codex-smoke-dummy-token"
    checks.append("token written only to isolated home")
    status, state = request("/apply/status")
    assert status == 200 and state["running"] is False and state["requests"] == []
    checks.append("idle review queue available")
    assert request("/apply/requests/decide", method="POST", body={"ids": ["a" * 32], "approve": "false"})[0] == 400
    checks.append("request approval requires a real boolean")
    assert request("/apply/requests/decide", method="POST", body={"ids": ["invalid"], "approve": True})[0] == 400
    checks.append("malformed request ID rejected")
    status, decision = request("/apply/requests/decide", method="POST", body={"ids": ["a" * 32], "approve": True})
    assert status == 200 and decision["success"] is False
    checks.append("unknown request approval cannot grant future authority")
    assert request("/apply/requests/decide", method="POST", body={"ids": ["a" * 32], "approve": True}, token=False)[0] == 401
    checks.append("unauthenticated request decision rejected")
    def raw_check(headers, method="GET"):
        with socket.create_connection(("127.0.0.1", port), timeout=3) as connection:
            message = method + " /health HTTP/1.1\r\n" + "\r\n".join(headers) + "\r\nConnection: close\r\n\r\n"
            connection.sendall(message.encode("ascii"))
            response = bytearray()
            while b"\r\n" not in response:
                part = connection.recv(1024)
                assert part
                response.extend(part)
            return int(response.split(b" ", 2)[1])
    for label, headers in [
        ("rebound Host", ["Host: rebound.example"]),
        ("Host user-info ambiguity", ["Host: localhost:80@evil.example"]),
        ("Host port suffix", ["Host: 127.0.0.1:80.evil.example"]),
        ("Host nonnumeric port", ["Host: localhost:not-a-port"]),
        ("duplicate Host", ["Host: 127.0.0.1", "Host: evil.example"]),
    ]:
        assert raw_check(headers) == 400, label
        checks.append(label + " rejected over actual HTTP")
    assert raw_check(["Host: evil.example", "Origin: http://localhost:1420", "Access-Control-Request-Method: GET"], "OPTIONS") == 400
    checks.append("Host rejected before CORS preflight over actual HTTP")
    print(json.dumps({"passed": len(checks), "checks": checks, "home": str(home)}))
finally:
    # Stop only the process and descendants started by this script.
    owned = []
    try:
        owned = psutil.Process(proc.pid).children(recursive=True)
    except psutil.NoSuchProcess:
        pass
    for child in reversed(owned):
        try:
            child.terminate()
        except psutil.NoSuchProcess:
            pass
    if proc.poll() is None:
        proc.terminate()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=5)
    psutil.wait_procs(owned, timeout=5)
    for child in owned:
        try:
            if child.is_running():
                child.kill()
        except psutil.NoSuchProcess:
            pass
    log.close()
