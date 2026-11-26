#!/usr/bin/env python3
"""Start a packaged app on a spare port and check it end to end: Path of Building starts, a build exports and loads
back, a craft goal is planned. Used by the release workflow on every OS before publishing.

    python tools/smoke_test.py <package folder> [python executable]

Failures are printed as GitHub annotations (::error::), so they can be read without the job log."""
import json, os, subprocess, sys, time, urllib.error, urllib.request

PORT = 8791
B = f"http://127.0.0.1:{PORT}"
H = {"Origin": B, "Referer": B + "/", "Content-Type": "application/json"}
HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def call(path, body=None, timeout=600):
    req = urllib.request.Request(B + path, data=json.dumps(body).encode() if body is not None else None, headers=H)
    try:
        return json.load(urllib.request.urlopen(req, timeout=timeout))
    except urllib.error.HTTPError as e:
        return json.load(e)


def fail(msg, log):
    tail = ""
    try:
        with open(log, encoding="utf-8", errors="replace") as f:
            tail = f.read()[-1500:]
    except OSError:
        pass
    print(f"::error::{msg} | server log: " + tail.replace("\r", "").replace("\n", " ⏎ "))
    sys.exit(1)


def main():
    pkg = os.path.abspath(sys.argv[1])
    py = sys.argv[2] if len(sys.argv) > 2 else sys.executable
    log = os.path.join(pkg, "smoke_server.log")
    env = dict(os.environ, PORT=str(PORT), PYTHONUTF8="1")
    with open(log, "w") as lf:
        proc = subprocess.Popen([py, "server.py"], cwd=pkg, env=env, stdout=lf, stderr=subprocess.STDOUT)
    try:
        for _ in range(120):
            try:
                urllib.request.urlopen(B + "/", timeout=2)
                break
            except Exception:
                if proc.poll() is not None:
                    fail("server exited at start", log)
                time.sleep(0.5)
        else:
            fail("server did not start", log)
        # a fresh server has no build (the page would send one): load PoB's empty starting build first
        with open(os.path.join(HERE, "tests", "smoke_build.txt"), encoding="utf-8") as f:
            ld0 = call("/api/load", {"input": f.read().strip()})
        if not ld0.get("ok"):
            fail(f"PoB load of the test build failed: {ld0.get('error')}", log)
        e = call("/api/export", {})
        if not e.get("ok"):
            fail(f"PoB export failed: {e.get('error')}", log)
        ld = call("/api/load", {"input": e["code"]})
        if not ld.get("ok"):
            fail(f"PoB load failed: {ld.get('error')}", log)
        with open(os.path.join(HERE, "tests", "cases", "wand_alloys.json"), encoding="utf-8") as f:
            st = dict(json.load(f)["state"])
        st["targets"] = [t for t in st["targets"] if t.get("on", True)]
        st["start"] = None
        job = call("/api/craft", dict(st, league="Standard", fill=False, noPutref=False)).get("job")
        if not job:
            fail("craft did not start", log)
        t0 = time.time()
        while True:
            r = call(f"/api/job/{job}")
            if r.get("status") != "running":
                break
            if time.time() - t0 > 900:
                fail("craft took over 15 minutes", log)
            time.sleep(2)
        res = r.get("result") or {}
        if r.get("status") != "done" or not res.get("steps"):
            fail(f"craft failed: {r.get('status')} {r.get('error')}", log)
        print(f"::notice::smoke test ok on {sys.platform}: PoB load, craft {res['mean']:.2f} div in "
              f"{time.time() - t0:.0f} s, {len(res['steps'])} steps")
    finally:
        proc.terminate()
        try:
            proc.wait(10)
        except Exception:
            proc.kill()


if __name__ == "__main__":
    main()
