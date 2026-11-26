#!/usr/bin/env python3
"""Control crafts: run every goal in tests/cases/ through the running app and compare with tests/baseline.json.

  python3 server.py &                         # the app must be running (http://127.0.0.1:8765)
  python3 tests/regress.py                    # all cases, compare with the baseline
  python3 tests/regress.py tiara belt         # only cases whose name contains one of these words
  python3 tests/regress.py --save             # write the current results as the new baseline
  python3 tests/regress.py --league "Standard"

What it checks (a patch day or a big change in craft.py):
- every case finishes (status done, steps present) — FAIL otherwise;
- currency the planner wanted but poe.ninja has no price for (renamed / removed items) — FAIL, listed;
- mean cost vs the baseline: prices move every day, so only a change beyond x0.6..x1.6 is a WARN (look at it);
- run time vs the baseline: x2 slower is a WARN.
Each case is a saved craft goal (the same JSON as a «Ссылка на крафт» state), so it does not depend on cache/.
"""
import argparse, glob, json, os, sys, time, urllib.error, urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
from gamedata import DEFAULT_LEAGUE  # noqa: E402

B = os.environ.get("APP_URL", "http://127.0.0.1:8765")
BASELINE = os.path.join(HERE, "baseline.json")


def call(path, body=None):
    req = urllib.request.Request(B + path, data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Content-Type": "application/json", "Origin": B, "Referer": B + "/"})
    return json.load(urllib.request.urlopen(req, timeout=900))


def run_case(case, league):
    st = dict(case["state"])
    # as the page sends it: only the mods whose checkbox is on
    st["targets"] = [t for t in st.get("targets") or [] if t.get("on", True)]
    body = dict(st, league=league, fill=False, noPutref=False)
    if st.get("mode") != "current":
        body["start"] = None
    t0 = time.time()
    job = call("/api/craft", body)["job"]
    while True:
        j = call("/api/job/" + job)
        if j["status"] != "running":
            break
        time.sleep(2)
    r = j.get("result") or {}
    return {"status": j["status"], "error": j.get("error"), "mean": r.get("mean"), "steps": len(r.get("steps") or []),
            "first": ((r.get("steps") or [{}])[0].get("step") or "")[:90], "missing": r.get("missingPrices") or [],
            "time": round(time.time() - t0, 1)}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("names", nargs="*")
    ap.add_argument("--save", action="store_true")
    ap.add_argument("--league", default=DEFAULT_LEAGUE)
    a = ap.parse_args()
    try:
        call("/api/craft/link/00000000")
    except urllib.error.HTTPError:
        pass
    except Exception:
        sys.exit(f"The app is not running at {B}: start it first (python3 server.py)")
    base = json.load(open(BASELINE, encoding="utf-8")) if os.path.exists(BASELINE) else {}
    files = sorted(glob.glob(os.path.join(HERE, "cases", "*.json")))
    cases = [json.load(open(f, encoding="utf-8")) for f in files]
    if a.names:
        cases = [c for c in cases if any(n in c["name"] for n in a.names)]
    out, bad = {}, 0
    print(f"league: {a.league}; {len(cases)} cases\n")
    for c in cases:
        print(f"{c['name']:<18} … ", end="", flush=True)
        try:
            r = run_case(c, a.league)
        except Exception as ex:  # noqa: BLE001
            r = {"status": "error", "error": str(ex)}
        out[c["name"]] = r
        b = base.get(c["name"]) or {}
        flags = []
        if r["status"] != "done" or not r.get("steps"):
            flags.append(f"FAIL {r['status']}: {r.get('error')}")
        if r.get("missing"):
            flags.append("FAIL нет цены на poe.ninja: " + ", ".join(r["missing"]))
        if b.get("mean") and r.get("mean"):
            k = r["mean"] / b["mean"]
            if not 0.6 <= k <= 1.6:
                flags.append(f"WARN цена ×{k:.2f} к эталону ({b['mean']:.2f} → {r['mean']:.2f} div)")
        if b.get("time") and r.get("time") and r["time"] > 2 * b["time"] + 10:
            flags.append(f"WARN медленнее: {b['time']:.0f} → {r['time']:.0f} с")
        bad += any(f.startswith("FAIL") for f in flags)
        mean = f"{r['mean']:.2f} div" if r.get("mean") else "—"
        print(f"{mean:>11}  {r.get('time', 0):>5.0f} с  {r.get('steps', 0):>2} шагов  " + ("; ".join(flags) or "ok"))
    if a.save:
        base.update(out)
        json.dump(base, open(BASELINE, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
        print(f"\nbaseline saved: {BASELINE}")
    sys.exit(1 if bad else 0)


if __name__ == "__main__":
    main()
