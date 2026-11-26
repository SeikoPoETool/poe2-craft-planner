#!/usr/bin/env python3
"""PoE2 item optimizer: local web app.

Path of Building (headless, engine/) computes how much each mod is worth for a build,
the PoE2 trade site finds candidates by that weighted sum, and every candidate is then
equipped in PoB to get the real change of DPS / EHP / other stats.

Runs on 127.0.0.1 only. POESESSID is taken from each request and kept in memory only.
"""
import base64, hashlib, json, math, os, re, subprocess, threading, time, uuid, zlib
import urllib.error, urllib.parse, urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import craft as CRAFT
from gamedata import META_RUNES, NINJA_TYPES, TRADE_CAT

HERE = os.path.dirname(os.path.abspath(__file__))
ENGINE = os.path.join(HERE, "engine")
POB_SRC = os.path.join(ENGINE, "pob2", "src")
UA = "Mozilla/5.0 (compatible; poe2-optimizer-local)"
TRADE = "https://www.pathofexile.com/api/trade2"
PORT = int(os.environ.get("PORT", "8765"))
WINDOWS = os.name == "nt"
# LuaJIT built for this machine (install.sh) or shipped in the release package; on Windows luajit.exe + lua51.dll
LUAJIT = os.path.join(ENGINE, "luajit.exe" if WINDOWS else "luajit")
CLIB_EXT = "dll" if WINDOWS else "so"


# ---------------------------------------------------------------- PoB worker
class Worker:
    def __init__(self):
        self.lock = threading.Lock()
        self.proc = None
        self.loaded_xml = None
        self.main_index = None
        self.config_ops = []  # PoB config changes made in the app (replayed after a worker restart)
        self.main_active = None

    def _start(self):
        lua_paths = 'package.path="../runtime/lua/?.lua;../runtime/lua/?/init.lua;./?.lua;"..package.path; package.cpath="../../clib/?.' + CLIB_EXT + ';"..package.cpath'
        self.proc = subprocess.Popen([LUAJIT, "-e", lua_paths, "../../worker.lua"], cwd=POB_SRC,
                                     stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, bufsize=1,
                                     encoding="utf-8", errors="replace",
                                     creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        self._read()  # ready

    def _read(self, progress=None):
        for line in self.proc.stdout:
            if line.startswith("@@"):
                return json.loads(line[2:])
            if line.startswith("%%") and progress:
                try:
                    progress(json.loads(line[2:]))
                except Exception:
                    pass
        raise RuntimeError("PoB worker exited")

    def call(self, op, progress=None, **kw):
        with self.lock:
            if self.proc is None or self.proc.poll() is not None:
                self._start()
                if self.loaded_xml and op != "load":  # restore state after a crash
                    self._send({"op": "load", "xml": self.loaded_xml})
                    if self.main_index:
                        self._send({"op": "setmain", "index": self.main_index, "active": self.main_active})
                    for cop in self.config_ops:
                        self._send(dict(cop, op="setConfig"))
            res = self._send(dict(kw, op=op), progress)
            if op == "load" and res.get("ok"):
                self.loaded_xml, self.main_index, self.config_ops, self.main_active = kw["xml"], None, [], None
            if op == "setConfig" and res.get("ok"):
                self.config_ops.append(kw)
            if op == "setmain" and res.get("ok"):
                self.main_index, self.main_active = kw["index"], kw.get("active")
            return res

    def _send(self, cmd, progress=None):
        self.proc.stdin.write(json.dumps(cmd) + "\n")
        self.proc.stdin.flush()
        return self._read(progress)


WORKER = Worker()


# ---------------------------------------------------------------- helpers
def http_json(url, data=None, sessid=None, timeout=60):
    headers = {"User-Agent": UA, "Content-Type": "application/json"}
    if sessid:
        headers["Cookie"] = "POESESSID=" + sessid
    req = urllib.request.Request(url, data=json.dumps(data).encode() if data is not None else None, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r), dict(r.headers)


class Cancelled(Exception):
    pass


# the job running in the current thread can be cancelled: every trade call and every wait checks this event
_ctx = threading.local()


def check_cancel():
    ev = getattr(_ctx, "cancel", None)
    if ev is not None and ev.is_set():
        raise Cancelled()


def sleep_cancellable(sec):
    ev = getattr(_ctx, "cancel", None)
    if ev is None:
        time.sleep(sec)
    elif ev.wait(sec):
        raise Cancelled()


class RateLimiter:
    """Follows the trade API's X-Rate-Limit-* rules ("max:period:penalty" per Account / Ip) with a sliding window.
    Every rule is respected exactly instead of spreading requests evenly: our own request times tell when a slot in
    a window frees up; the server's reported count covers requests made elsewhere (e.g. the trade site in a browser).
    Close to the edge of a long window (e.g. 600 per 3 hours, 1 hour ban) requests are spread so it is never crossed."""
    def __init__(self):
        self.lock = threading.Lock()
        self.hist = {}    # kind -> [timestamps of our requests]
        self.rules = {}   # kind -> [(max, period, reported count, time of the report)]

    def _need(self, kind, now):
        hist = self.hist.get(kind, [])
        wait = 0.0
        for mx, per, cur, t_rep in self.rules.get(kind, []):
            mine = [t for t in hist if t > now - per]
            # requests from elsewhere still in the window (assume they stay for the rest of the report's period)
            foreign = max(0, cur - sum(1 for t in hist if t > t_rep - per and t <= t_rep)) if now - t_rep < per else 0
            used = len(mine) + foreign
            margin = 1 if mx <= 20 else max(2, int(mx * 0.03))
            free = mx - margin - used
            if free <= 0:
                k = 1 - free  # how many of our requests must leave the window first
                if len(mine) >= k:
                    wait = max(wait, sorted(mine)[k - 1] + per - now + 0.3)
                else:
                    wait = max(wait, per / mx * k)
            elif per >= 600 and used >= mx * 0.85:
                # near the edge of a long window: keep an even pace so it is never crossed
                last = max(mine) if mine else 0
                wait = max(wait, last + per / mx - now)
        return wait

    def wait(self, kind):
        while True:
            with self.lock:
                now = time.time()
                w = self._need(kind, now)
                if w <= 0:
                    self.hist.setdefault(kind, []).append(now)
                    self.hist[kind] = [t for t in self.hist[kind] if t > now - 11000]
                    return
            sleep_cancellable(min(w, 30))

    def update(self, kind, headers):
        rules = []
        now = time.time()
        for rule in [r.strip() for r in headers.get("X-Rate-Limit-Rules", "").split(",") if r.strip()]:
            pol, st = headers.get(f"X-Rate-Limit-{rule}"), headers.get(f"X-Rate-Limit-{rule}-State")
            if not pol or not st:
                continue
            for pp, ss in zip(pol.split(","), st.split(",")):
                mx, per, _ = map(int, pp.split(":"))
                rules.append((mx, per, int(ss.split(":")[0]), now))
        if rules:
            with self.lock:
                self.rules[kind] = rules


RL = RateLimiter()


def trade_call(kind, url, data=None, sessid=None, log=None):
    # identical requests within a few minutes are answered from memory (no rate limit spent)
    key = (kind, url, json.dumps(data, sort_keys=True) if data is not None else None)
    hit = _trade_cache.get(key)
    if hit and time.time() - hit[0] < (300 if kind == "search" else 600):
        return hit[1]
    for attempt in range(6):
        check_cancel()
        RL.wait(kind)
        try:
            body, headers = http_json(url, data, sessid)
            RL.update(kind, headers)
            _trade_cache[key] = (time.time(), body)
            if len(_trade_cache) > 2000:
                for k in sorted(_trade_cache, key=lambda k: _trade_cache[k][0])[:500]:
                    _trade_cache.pop(k, None)
            return body
        except urllib.error.HTTPError as e:
            if e.code == 429:
                wait = int(e.headers.get("Retry-After", "10")) + 1
                if log:
                    log(f"Лимит трейда, ждём {wait} с")
                RL.update(kind, e.headers)
                sleep_cancellable(wait)
                continue
            if e.code in (500, 502, 503, 504) and attempt < 5:
                if log:
                    log(f"Трейд ответил {e.code}, повторяю через {3 * (attempt + 1)} с")
                sleep_cancellable(3 * (attempt + 1))
                continue
            msg = e.read()[:300].decode("utf-8", "replace")
            raise RuntimeError(f"Трейд вернул {e.code}: {msg}")
        except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as e:
            # a dropped connection or a timeout: the trade site is often slow, one failure must not end the search
            if attempt >= 5:
                raise RuntimeError(f"Трейд недоступен: {e}")
            if log:
                log(f"Нет ответа от трейда ({e.__class__.__name__}), повторяю через {3 * (attempt + 1)} с")
            sleep_cancellable(3 * (attempt + 1))
    raise RuntimeError("Трейд не отвечает из-за лимитов")


_trade_cache = {}
_cache = {}
def cached(key, ttl, fn):
    v = _cache.get(key)
    if v and time.time() - v[0] < ttl:
        return v[1]
    val = fn()
    _cache[key] = (time.time(), val)
    return val


def leagues():
    d, _ = http_json(TRADE + "/data/leagues")
    return [x["id"] for x in d["result"] if x.get("realm") == "poe2"]


def trade_stats():
    d, _ = http_json(TRADE + "/data/stats")
    out = []
    for g in d["result"]:  # every group the trade site has: pseudo totals, explicit, crafted, skill, ...
        for e in g["entries"]:
            out.append({"id": e["id"], "text": e["text"], "type": g["label"]})
    return out


def rates(league):
    def fetch():
        url = "https://poe.ninja/poe2/api/economy/exchange/current/overview?" + urllib.parse.urlencode({"league": league, "type": "Currency"})
        d, _ = http_json(url)
        r = {"divine": 1.0}
        for line in d.get("lines", []):
            if line.get("primaryValue"):
                r[line["id"]] = float(line["primaryValue"])
        return r
    try:
        return cached("rates:" + league, 1800, fetch)
    except Exception:
        return {"divine": 1.0, "_fallback": True}


def energy_stats():
    """trade stats PoB cannot read but the app models (meta-gem energy); weighted only when that option is on"""
    try:
        stats = cached("stats", 6 * 3600, trade_stats)
    except Exception:
        return []
    return [{"id": s["id"], "text": s["text"]} for s in stats
            if s.get("type") == "Explicit" and re.fullmatch(r"Meta Skills gain #% increased Energy", s["text"])]


def decode_build(text):
    text = text.strip()
    m = re.search(r"pobb\.in/(?:u/[^/]+/)?([A-Za-z0-9_-]+)", text)
    if m:
        req = urllib.request.Request(f"https://pobb.in/{m.group(1)}/raw", headers={"User-Agent": UA})
        text = urllib.request.urlopen(req, timeout=30).read().decode().strip()
    elif text.startswith("http"):
        req = urllib.request.Request(text, headers={"User-Agent": UA})
        text = urllib.request.urlopen(req, timeout=30).read().decode().strip()
    if text.lstrip().startswith("<"):
        return text
    raw = base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))
    return zlib.decompress(raw).decode("utf-8")


# ---------------------------------------------------------------- search job
JOBS = {}

# ---------------------------------------------------------------- trade-sum features (what the trade site sums per stat)
ELEMS = ("fire", "cold", "lightning", "chaos")


def item_lines(raw):
    """(implicit lines, explicit lines, all mod lines) of a PoB item text, without {tags}."""
    lines = (raw or "").splitlines()
    impl, expl, alll = [], [], []
    start, n_impl = None, 0
    for k, l in enumerate(lines):
        m = re.match(r"^Implicits: (\d+)", l)
        if m:
            n_impl, start = int(m.group(1)), k + 1
            break
    if start is None:
        return impl, expl, alll
    for k in range(start, len(lines)):
        l = lines[k]
        if l in ("Corrupted", "Mirrored", "Sanctified", "Unidentified") or l.startswith("Requires"):
            continue
        clean = re.sub(r"\{[^}]*\}", "", l).strip()
        alll.append(clean)
        if "{rune}" in l or "{enchant}" in l:
            continue
        (impl if k < start + n_impl else expl).append(clean)
    return impl, expl, alll


def _sum_value(tmpl, lines):
    rx = re.escape(tmpl.replace("(Local)", "").strip()).replace(r"\#", r"([+-]?\d+(?:\.\d+)?)").replace(r"\+", r"\+?")
    tot = 0.0
    for ln in lines:
        m = re.fullmatch(rx, ln[8:] if ln.startswith("Bonded: ") else ln)
        if m:
            tot += float(m.group(1))
    return tot


def _pseudo_res(lines):
    t = {}
    for l in lines:
        l = l.replace("Bonded: ", "")
        m = re.fullmatch(r"\+?(\d+)% to all Elemental Resistances", l)
        if m:
            for e in ("fire", "cold", "lightning"):
                t[e] = t.get(e, 0) + int(m.group(1))
            continue
        m = re.fullmatch(r"\+?(\d+)% to (\w+) and (\w+) Resistances", l)
        if m:
            for e in (m.group(2).lower(), m.group(3).lower()):
                t[e] = t.get(e, 0) + int(m.group(1))
            continue
        m = re.fullmatch(r"\+?(\d+)% to (\w+) Resistance", l)
        if m and m.group(2).lower() in ELEMS:
            t[m.group(2).lower()] = t.get(m.group(2).lower(), 0) + int(m.group(1))
    return {f"pseudo.pseudo_total_{e}_resistance": v for e, v in t.items()}


def features(raw, weights):
    """Value of every weighted trade stat on an item, summed the way the trade site does."""
    impl, expl, alll = item_lines(raw)
    ps = _pseudo_res(alll)
    x = []
    for w in weights:
        wid = w["id"]
        if wid.startswith("pseudo."):
            x.append(float(ps.get(wid, 0)))
        elif wid.startswith("implicit."):
            x.append(_sum_value(w["text"], impl))
        else:
            x.append(_sum_value(w["text"], expl))
    return x


def _solve(A, b):
    n = len(A)
    M = [row[:] + [b[i]] for i, row in enumerate(A)]
    for c in range(n):
        p = max(range(c, n), key=lambda r: abs(M[r][c]))
        if abs(M[p][c]) < 1e-12:
            continue
        M[c], M[p] = M[p], M[c]
        for r in range(n):
            if r != c and M[r][c]:
                f = M[r][c] / M[c][c]
                for k in range(c, n + 1):
                    M[r][k] -= f * M[c][k]
    return [M[i][n] / M[i][i] if abs(M[i][i]) > 1e-12 else 0.0 for i in range(n)]


def refit_weights(X, y, w0, strength=0.3):
    """Ridge regression of the real PoB score on the trade stat values, shrunk towards the PoB mod weights w0.
    Free intercept (only the ranking matters for the trade sum); negative weights clipped to 0.
    Checked on 141 real gloves: held-out rank agreement with PoB 0.60 -> 0.70."""
    n, k = len(X), len(w0)
    mx = [sum(X[i][j] for i in range(n)) / n for j in range(k)]
    my = sum(y) / n
    Xc = [[X[i][j] - mx[j] for j in range(k)] for i in range(n)]
    yc = [v - my for v in y]
    A = [[sum(Xc[i][a] * Xc[i][b] for i in range(n)) for b in range(k)] for a in range(k)]
    rhs = [sum(Xc[i][a] * yc[i] for i in range(n)) for a in range(k)]
    for j in range(k):
        lam = strength * A[j][j] + 1e-9
        A[j][j] += lam
        rhs[j] += lam * w0[j]
    return [max(0.0, b) for b in _solve(A, rhs)]


def collapse_same(rows):
    """Identical offers (same name, same mods with the same values, same target slot): keep the cheapest, count the rest."""
    groups = {}
    for r in rows:
        mods = tuple(sorted(m.get("text", "") for m in ((r.get("view") or {}).get("mods") or [])))
        key = (r["name"], mods, r.get("slot"))
        groups.setdefault(key, []).append(r)
    out = []
    for g in groups.values():
        g.sort(key=lambda r: (r["priceDiv"] is None, r["priceDiv"] or 0))
        best = g[0]
        best["dupes"] = len(g) - 1
        if len(g) > 1:
            prices = [r["priceDiv"] for r in g if r["priceDiv"]]
            best["dupeMax"] = max(prices) if prices else None
        out.append(best)
    return out


def find_threshold(total_for, start, log, enough=5000):
    """Lowest weight threshold whose result set is complete (< 10000 items, so the trade site sorts all of it).
    clipped = highest threshold known to give >= 10000, complete = lowest known to give 5..9999,
    too_few = lowest known to give < 5; always search strictly between what is known. Lowering 'complete' only
    widens the pool for the cheapest-items pass, so it is refined only while the set is far from the cap (< 5000)."""
    clipped = complete = too_few = None
    res_complete = res_few = res_clipped = None
    t = start
    for step in range(9):
        r = total_for(t, {"statgroup.0": "desc"})
        tot = r.get("total", 0)
        log(f"{'Поиск по весу, порог' if step == 0 else 'Уточняю порог'} {t:.1f}: найдено {tot}")
        if tot >= 10000:
            if clipped is None or t > clipped:
                clipped, res_clipped = t, r
        elif tot < 5:
            if too_few is None or t < too_few:
                too_few, res_few = t, r
        else:
            if complete is None or t < complete:
                complete, res_complete = t, r
        if clipped is None:
            if complete is not None:
                break
            if t <= 0.02:
                break
            t *= 0.5
            continue
        upper = complete if complete is not None else too_few
        if upper is None:
            t = clipped * 1.5
            continue
        if complete is not None and (complete / clipped < 1.06 or res_complete.get("total", 0) >= enough):
            break
        # the number of matches falls roughly exponentially with the threshold: aim straight at ~7000 matches by
        # interpolating log(count) between the two known points (instead of halving the interval)
        n_up = max(1, (res_complete or res_few).get("total", 1))
        frac = (math.log(10000) - math.log(7000)) / max(1e-9, math.log(10000) - math.log(n_up))
        if tot >= 10000 and step > 1:
            frac = max(frac, 0.5)  # the estimate undershot (a steep edge): fall back to halving
        t = clipped + (upper - clipped) * min(0.9, max(0.1, frac))
    res = res_complete or res_few or res_clipped
    min_w = complete if res_complete else too_few if res_few else clipped
    return res, min_w


def search_slot(p, slot, sw, log, n_top, n_cheap, required=True, refine=True, eval_slots=None, group=None):
    """Weighted trade search for one slot + PoB evaluation of the candidates (+ a refined second pass).
    Returns {"result": {...rows...}, "tradeUrl": str, "weights": [...]}.
    The trade site allows a logged-in user (a valid POESESSID) a query twice as complex as an anonymous one: when it
    answers "too complex" (an expired POESESSID), the search runs again on fewer of the most valuable mods"""
    for budget in (200, 110, 70):
        try:
            return _search_slot(p, slot, sw, log, n_top, n_cheap, required, refine, eval_slots, group, budget)
        except RuntimeError as ex:
            if "too complex" not in str(ex) or budget == 70:
                if "too complex" in str(ex):
                    raise RuntimeError("Трейд не принимает такой сложный запрос: похоже, POESESSID устарел (трейд считает вас "
                                       "не вошедшим). Войдите на pathofexile.com и вставьте свежий POESESSID") from None
                raise
            log("Трейд счёл запрос слишком сложным — похоже, POESESSID устарел (без входа лимит меньше). Ищу по самым "
                "ценным модам; со свежим POESESSID поиск точнее")


def _search_slot(p, slot, sw, log, n_top, n_cheap, required=True, refine=True, eval_slots=None, group=None, cap=200):
    sess, league = p["poesessid"], p["league"]
    lg = urllib.parse.quote(league)
    ignore_res = bool(p.get("ignoreRes"))
    use_heur = p.get("heur", True) is not False  # indirect mods PoB does not count (low confidence)
    jewel_base = p.get("jewelBase") or None
    is_jewel = slot.startswith("Jewel")
    log("Считаю веса модов в PoB")
    wr = WORKER.call("weights", slot=slot, statWeights=sw, ignoreRes=ignore_res, base=jewel_base, extraStats=energy_stats(), heur=use_heur)
    if not wr.get("ok"):
        raise RuntimeError("PoB: " + str(wr.get("error")))
    W = wr["data"]
    log(f"Весов: {len(W['weights'])}, категория {W['category']}" + (f", база {jewel_base}" if jewel_base else ""))
    if not W["weights"]:
        raise RuntimeError("Ни один мод этого слота не улучшает выбранные метрики")
    if p.get("ceilingMin") is not None and W.get("ceiling") is not None:
        log(f"Проверка в PoB: даже предмет из 4 лучших модов дал бы {W['ceiling']:+.1f}%")
        if W["ceiling"] < p["ceilingMin"]:
            raise RuntimeError(f"Ни один мод этого слота не улучшает выбранные метрики (потолок {W['ceiling']:+.1f}%)")

    andf = []
    if required:
        andf = [{"id": m["id"], "value": {k: v for k, v in (("min", m.get("min")), ("max", m.get("max"))) if v not in (None, "")}}
                for m in p.get("requiredMods", [])]
    filters = {"type_filters": {"filters": {"category": {"option": W["category"]}}}}
    # exclusions chosen in the UI: trade misc filters set to "false", uniques via rarity "nonunique"
    MISC = {"mirrored", "sanctified", "corrupted", "fractured_item", "desecrated", "veiled"}
    excl = [x for x in (p.get("exclude") or []) if x in MISC]
    if excl:
        filters["misc_filters"] = {"filters": {x: {"option": "false"} for x in excl}}
    if "unique" in (p.get("exclude") or []):
        filters["type_filters"]["filters"]["rarity"] = {"option": "nonunique"}
    # wanted open affixes: a rare with at most 3 - N prefixes / suffixes. Corrupted / mirrored / sanctified items are
    # NOT excluded here: the user excludes them by hand in «Исключения» when needed
    op_n, os_n = int(p.get("openPrefixes") or 0), int(p.get("openSuffixes") or 0)
    if (op_n or os_n) and not p.get("uniqueName"):
        filters["type_filters"]["filters"]["rarity"] = {"option": "rare"}
        if op_n:
            andf.append({"id": "pseudo.pseudo_number_of_prefix_mods", "value": {"max": 3 - op_n}})
        if os_n:
            andf.append({"id": "pseudo.pseudo_number_of_suffix_mods", "value": {"max": 3 - os_n}})
        log(f"Только редкие со свободными: префиксов ≥ {op_n}, суффиксов ≥ {os_n}")
    # armour / evasion / energy shield of the item, min / max as the trade's "Equipment Filters" (max 0 drops hybrid bases).
    # Armour slots only: jewellery and weapons have no such property and a range would make the trade drop every listing
    dm = {}
    for k, v in (p.get("defRange") or {}).items():
        if k in ("ar", "ev", "es") and isinstance(v, dict):
            r = {m: float(v[m]) for m in ("min", "max") if v.get(m) not in (None, "")}
            if r:
                dm[k] = {m: int(x) if x == int(x) else x for m, x in r.items()}
    if dm and not str(W.get("category") or "").startswith("armour."):
        dm = {}
    if dm:
        filters["equipment_filters"] = {"filters": dm}
        names = {"ar": "броня", "ev": "уклонение", "es": "ES"}
        log("Защита предмета: " + ", ".join(f"{names[k]} {r.get('min', '')}–{r.get('max', '')}" for k, r in dm.items()))
    if p.get("maxPrice") or p.get("minPrice"):
        price = {"option": "divine"}
        if p.get("minPrice"):
            price["min"] = float(p["minPrice"])
        if p.get("maxPrice"):
            price["max"] = float(p["maxPrice"])
        filters["trade_filters"] = {"filters": {"price": price}}
    if "jewelType" in p:
        type_name = p.get("jewelType")
    else:
        type_name = jewel_base or (p.get("baseType") if required else None)
    uname = p.get("uniqueName")

    def pick_filters(weights, values):
        """the weighted stats that fit the query complexity budget (200, as PoB), most valuable first"""
        budget = cap - 54 - 4 - 4 - 4 * len(andf) - (3 if (p.get("maxPrice") or p.get("minPrice")) else 0) - 3 * len(dm)
        order = sorted(range(len(weights)), key=lambda i: -values[i])
        # a mod worth under a fifth of the most valuable one only shifts the trade's ranking (boots: +Int at 3 against
        # flat ES at 19 for a build where PoB gives Int nothing): the search leaves it out, PoB still values the finds
        top = max([values[i] for i in order if values[i] > 0], default=0)
        used = []
        for i in order:
            if budget < 8 or values[i] <= 0 or values[i] < 0.2 * top:
                break
            used.append(i)
            budget -= 4
        return used

    def make_query(weights, wvals, used):
        wf = [{"id": weights[i]["id"], "value": {"weight": round(wvals[i], 4)}} for i in used]

        def query(min_w, sort):
            stats = [{"type": "weight", "filters": wf, "value": {"min": round(min_w, 2)} if min_w is not None else {}}]
            if andf:
                stats.append({"type": "and", "filters": andf})
            qq = {"status": {"option": p.get("status", "available")}, "filters": json.loads(json.dumps(filters)), "stats": stats}
            if type_name:
                qq["type"] = type_name
            return {"query": qq, "sort": sort}
        return query

    def total_for_q(query):
        return lambda min_w, sort: trade_call("search", f"{TRADE}/search/poe2/{lg}", query(min_w, sort), sess, log)

    weights = W["weights"]
    w0 = [w["weight"] for w in weights]
    cur_raw = W.get("currentRaw") or ""
    x_cur = features(cur_raw, weights)
    used = pick_filters(weights, [w["meanStatDiff"] for w in weights])
    query = make_query(weights, w0, used)
    ids, qid_of = [], {}

    def add(result_ids, qid, limit):
        n = 0
        for i in result_ids[:limit]:
            if i not in qid_of:
                ids.append(i); qid_of[i] = qid; n += 1
        return n

    def unique_query(name, base_name, sort, variant_stats=None):
        """one unique by name (and base), best rolls first by the weights; no weight threshold"""
        q = query(None, sort)
        q["query"]["name"] = name
        if base_name:
            q["query"]["type"] = base_name
        q["query"]["filters"]["type_filters"]["filters"].pop("rarity", None)
        if variant_stats:
            q["query"]["stats"].append({"type": "and", "filters": [{"id": sid} for sid in variant_stats]})
        return q

    if uname:
        # a chosen unique: all its listings, best rolls (by weights) + the cheapest
        log(f"Ищу только «{uname}»" + (f" ({p.get('uniqueBase')})" if p.get("uniqueBase") else ""))
        # with a known variant line (e.g. "+# to Level of all Comet Skills") the best rolls are the highest values of
        # that stat — the generic weights usually do not even contain it
        best_sort = {f"stat.{p['variantStats'][0]}": "desc"} if p.get("variantStats") else {"statgroup.0": "desc"}
        ru = trade_call("search", f"{TRADE}/search/poe2/{lg}", unique_query(uname, p.get("uniqueBase"), best_sort, p.get("variantStats")), sess, log)
        if not ru.get("total") and p.get("variantStats"):
            log("С нужным вариантом не найдено — беру все варианты")
            ru = trade_call("search", f"{TRADE}/search/poe2/{lg}", unique_query(uname, p.get("uniqueBase"), {"statgroup.0": "desc"}), sess, log)
        trade_url = f"https://www.pathofexile.com/trade2/search/poe2/{lg}/{ru.get('id')}"
        add(ru.get("result", []), ru.get("id"), n_top)
        if ru.get("total", 0) > n_top and n_cheap:
            rc = trade_call("search", f"{TRADE}/search/poe2/{lg}", unique_query(uname, p.get("uniqueBase"), {"price": "asc"}), sess, log)
            add(rc.get("result", []), rc.get("id"), n_cheap)
        log(f"«{uname}» на трейде: {ru.get('total', 0)}, кандидатов: {len(ids)}")
        refine = False
    else:
        cur = sum(w0[i] * x_cur[i] for i in used)
        log(f"Текущий предмет по весам: {cur:.0f}")
        start = max(cur * 1.02, 0.01)
        # A current item worth ~0 by weights (a unique whose power is its special effect, e.g. Mageblood, or an empty
        # socket) would start near zero: start from half of a typical roll of the best mod. PoB still scores the real effect.
        top = max((w["meanStatDiff"] for w in weights), default=0)
        # with an empty socket or a current item that trade weights do not see, start where good items are:
        # half of the PoB "4 best mods" item (if known), else half of a typical roll of the best mod
        guess = max(top * 0.5, (W.get("ceilingUnits") or 0) * 0.5)
        if guess > 0 and start < guess:
            start = guess
            log(f"Текущий предмет почти ничего не стоит по модам трейда — начинаю с порога {start:.1f}")
        res, min_w = find_threshold(total_for_q(query), start, log)
        trade_url = f"https://www.pathofexile.com/trade2/search/poe2/{lg}/{res['id']}"
        add(res["result"], res["id"], n_top)
        if n_cheap:  # the cheapest items with the same (complete) threshold
            res2 = total_for_q(query)(min_w, {"price": "asc"})
            log(f"Поиск по цене (порог {min_w:.1f}): найдено {res2.get('total')}")
            add(res2["result"], res2["id"], n_cheap)
        # Better-rolled copies of the current unique: they weigh about as much as the current item, so a threshold
        # raised past the 10000 cap cuts them off. One extra search by name, no threshold, best by weight + cheapest.
        if (W.get("currentRarity") == "UNIQUE" and W.get("currentTitle") and "unique" not in (p.get("exclude") or [])
                and not (type_name and type_name != W.get("currentBase"))):
            ru = trade_call("search", f"{TRADE}/search/poe2/{lg}", unique_query(W["currentTitle"], W.get("currentBase"), {"statgroup.0": "desc"}), sess, log)
            added = add(ru.get("result", []), ru.get("id"), 10)
            if ru.get("total", 0) > 10:
                rc = trade_call("search", f"{TRADE}/search/poe2/{lg}", unique_query(W["currentTitle"], W.get("currentBase"), {"price": "asc"}), sess, log)
                added += add(rc.get("result", []), rc.get("id"), 10)
            log(f"Копии вашего уникального «{W['currentTitle']}» на трейде: {ru.get('total', 0)}, добавлено кандидатов: {added}")
    if not ids:
        raise RuntimeError("На трейде ничего не найдено")

    rt = rates(league)
    if rt.get("_fallback"):
        log("⚠ Курсы валют с poe.ninja не получены: цены не в divine останутся без пересчёта (не попадут в «дешёвые» и «Цену доп. 1%»)")
    total_w = sum(w["weightMult"] for w in sw)
    state = {"base": None, "baseScore": None}

    def fetch_eval(id_list, tag):
        items = []
        for k in range(0, len(id_list), 10):
            chunk = id_list[k:k + 10]
            d = trade_call("fetch", f"{TRADE}/fetch/{','.join(chunk)}?query={qid_of[chunk[0]]}&realm=poe2", None, sess, log)
            items += [r for r in d["result"] if r]
            log(f"{tag}Скачано {len(items)} из {len(id_list)}")
        if not items:
            return []
        t0 = time.time()
        ev = WORKER.call("eval", slot=slot, slots=eval_slots, statWeights=sw, ignoreRes=ignore_res, heur=use_heur, items=[json.loads(json.dumps(r["item"])) for r in items])
        if not ev.get("ok"):
            raise RuntimeError("PoB: " + str(ev.get("error")))
        E = ev["data"]
        state["base"], state["baseScore"] = E["base"], E["baseScore"]
        log(f"{tag}Посчитано в PoB: {len(items)} предметов за {time.time() - t0:.1f} с")
        rows = []
        for r, e in zip(items, E["items"]):
            if "stats" not in e:
                continue
            it, lst = r["item"], r["listing"]
            price = lst.get("price") or {}
            amt, cur_id = price.get("amount"), price.get("currency")
            div = amt * rt.get(cur_id, 0) if amt and cur_id in rt else None
            acc = (lst.get("account") or {}).get("name")
            lq = {"query": {"status": {"option": "any"}, "type": it.get("typeLine"),
                            "filters": {"trade_filters": {"filters": {"account": {"input": acc}}}} if acc else {}},
                  "sort": {"price": "asc"}}
            link = f"https://www.pathofexile.com/trade2/search/poe2/{lg}?q=" + urllib.parse.quote(json.dumps(lq, separators=(",", ":")))
            base = E["base"]
            delta = {s: ((e["stats"][s] / base[s] - 1) * 100 if base.get(s) else None) for s in e["stats"]}
            rows.append({
                "name": f"{it.get('name') or ''} {it.get('typeLine') or ''}".strip(),
                "gain": (e["score"] - E["baseScore"]) / total_w * 100, "score": e["score"],
                "delta": delta, "stats": e["stats"], "warn": e.get("warn") or [],
                # blocking problems: missing attributes always; resistance drops only when resistances are not ignored
                "block": any(not (ignore_res and w.startswith("резист")) for w in (e.get("warn") or [])),
                "price": f"{amt} {cur_id}" if amt else "—", "priceDiv": div,
                "online": bool((lst.get("account") or {}).get("online")), "seller": acc,
                "indexed": lst.get("indexed"), "link": link, "raw": e.get("raw"), "icon": it.get("icon"), "view": e.get("view"),
                "heur": e.get("heur"),
                "pass": 2 if tag else 1, "slot": e.get("slot") or slot, "group": group,
            })
        return rows

    rows = fetch_eval(ids, "")
    out_weights = [dict(w) for w in weights[:40]]

    # Second pass: fit the trade weights to the real PoB scores of the evaluated candidates (ridge towards the PoB
    # mod weights) and search again with them. Catches what per-mod weights misjudge: caps (gem levels, crit),
    # interactions, mods whose value depends on the rest of the item.
    if refine and len(rows) >= 12:
        X = [features(r["raw"], weights) for r in rows]
        beta = refit_weights(X, [r["score"] * 1000 for r in rows], w0)
        for i, w in enumerate(out_weights):
            if i < len(beta):
                w["refined"] = beta[i]
        vals = [beta[i] * (weights[i].get("testValue") or 1) for i in range(len(weights))]
        used2 = pick_filters(weights, vals)
        if used2:
            query2 = make_query(weights, beta, used2)
            cur2 = sum(beta[i] * x_cur[i] for i in used2)
            top2 = max(vals, default=0)
            start2 = max(cur2 * 1.02, 0.01, top2 * 0.5 if cur2 < top2 * 0.5 else 0)
            # the first pass ended with a complete set above min_w; with the new weights the same items sit around the
            # lowest refined sum among the strongest first-pass candidates — start there instead of from scratch
            sums = sorted(sum(beta[i] * x[i] for i in used2) for x, r in zip(X, rows) if r["pass"] == 1)
            if len(sums) >= 10:
                start2 = max(start2, sums[len(sums) // 3] * 0.95)
            log(f"Второй проход: веса уточнены по {len(rows)} предметам, текущий предмет по новым весам {cur2:.0f}")
            # only the strongest items are taken from the second pass: any complete set of n_top+ items will do
            res_b, _ = find_threshold(total_for_q(query2), start2, lambda m: log("[2] " + m), enough=max(n_top, 10))
            before = len(ids)
            add(res_b["result"], res_b["id"], n_top)
            new_ids = ids[before:]
            if new_ids:
                rows2 = fetch_eval(new_ids, "[2] ")
                good = sorted(rows + rows2, key=lambda r: -r["gain"])[:10]
                log(f"Второй проход: новых кандидатов {len(rows2)}, из них в топ-10 по приросту: {sum(1 for r in good if r['pass'] == 2)}")
                rows += rows2
            else:
                log("Второй проход: новых кандидатов нет — первый проход уже нашёл лучшее")

    n0 = len(rows)
    rows = collapse_same(rows)
    if len(rows) < n0:
        log(f"Одинаковых предложений схлопнуто: {n0 - len(rows)} (оставлено самое дешёвое)")
    rows.sort(key=lambda x: (-x["gain"], x["priceDiv"] if x["priceDiv"] is not None else 1e18))
    # cheapest-for-the-gain frontier, only among items without problems (requirements, resistance drops)
    best = -1e9
    for x in sorted([r for r in rows if r["priceDiv"] and not r["block"]], key=lambda r: (r["priceDiv"], -r["gain"])):
        if x["gain"] > best and x["gain"] > 0:
            x["pareto"] = True
            best = x["gain"]
    log(f"Готово: {sum(1 for r in rows if r['gain'] > 0)} улучшений из {len(rows)}")
    return {"result": {"base": state["base"], "baseScore": state["baseScore"], "rows": rows, "metrics": sw,
                       "ignoreRes": ignore_res, "heur": use_heur, "slot": slot, "jewelBase": jewel_base},
            "tradeUrl": trade_url, "weights": out_weights}


def _search_prelude(p):
    sw = [w for w in p["statWeights"] if w.get("weightMult", 0) > 0]
    if not sw:
        raise RuntimeError("Не выбрано ни одной метрики")
    p["poesessid"] = (p.get("poesessid") or "").strip()
    if not p["poesessid"]:
        raise RuntimeError("Нужен POESESSID: без входа трейд не принимает взвешенный поиск")
    return sw


def run_search(job, p):
    log = lambda msg: job["log"].append([round(time.time() - job["t0"], 1), msg])
    try:
        sw = _search_prelude(p)
        out = search_slot(p, p["slot"], sw, log, int(p.get("topN", 10)), int(p.get("cheapN", 10)), refine=p.get("refine", True) is not False)
        job.update(out)
        job["status"] = "done"
    except Cancelled:
        job["status"] = "cancelled"
        log("Поиск отменён")
    except Exception as ex:
        job["status"] = "error"
        job["error"] = str(ex)
        log("Ошибка: " + str(ex))


def run_scan(job, p):
    """'What to upgrade first': the same search for every equipped gear slot, fewer candidates per slot."""
    log = lambda msg: job["log"].append([round(time.time() - job["t0"], 1), msg])
    try:
        sw = _search_prelude(p)
        slots = p.get("slots") or []
        n_top, n_cheap = min(int(p.get("topN", 10)), 15), min(int(p.get("cheapN", 10)), 15)
        job["scan"] = []
        for k, slot in enumerate(slots):
            pre = f"[{k + 1}/{len(slots)} {slot}] "
            slog = lambda msg, pre=pre: log(pre + msg)
            entry = {"slot": slot}
            try:
                out = search_slot(p, slot, sw, slog, n_top, n_cheap, required=False, refine=bool(p.get("refine")))
                rows = out["result"]["rows"]
                ok = [r for r in rows if not r["block"]]
                best = max(rows, key=lambda r: r["gain"], default=None)
                cheap = min([r for r in ok if r.get("pareto")], key=lambda r: r["priceDiv"], default=None)
                pick = lambda r: r and {"name": r["name"], "gain": r["gain"], "price": r["price"], "priceDiv": r["priceDiv"], "warn": r["warn"]}
                entry.update({"best": pick(best), "cheapest": pick(cheap),
                              "upgrades": sum(1 for r in ok if r["gain"] > 0), "checked": len(rows), "search": out})
            except Cancelled:
                raise
            except Exception as ex:
                entry["error"] = str(ex)
                slog("Ошибка: " + str(ex))
            job["scan"].append(entry)
        job["status"] = "done"
        log("Готово: все слоты проверены")
    except Cancelled:
        job["status"] = "cancelled"
        log(f"Отменено: проверено слотов {len(job.get('scan') or [])}")
    except Exception as ex:
        job["status"] = "error"
        job["error"] = str(ex)
        log("Ошибка: " + str(ex))


def _stat_tmpl(t):
    t = re.sub(r"\{[^}]*\}", "", t.split("\n")[0]).lower()
    t = re.sub(r"\(-?[\d.]+-(-?[\d.]+)\)", "#", t)
    t = re.sub(r"[+-]?\d+(?:\.\d+)?", "#", t)
    return t.replace("+#", "#").strip()


def variant_stats(lines):
    """trade stat ids for the lines that tell a unique's variant apart (to search that variant)"""
    stats = cached("stats", 6 * 3600, trade_stats)
    idx = {}
    for st in stats:
        if st.get("type") == "Explicit":
            idx.setdefault(_stat_tmpl(st["text"]), st["id"])
    out = []
    for l in lines or []:
        sid = idx.get(_stat_tmpl(l))
        if sid and sid not in out:
            out.append(sid)
    return out


def run_jewels(job, p):
    """Jewels for the whole build: every candidate goes to the socket where it gains the most.
    Sockets with a unique jewel are compared only with copies of that unique; the others take rares and uniques."""
    log = lambda msg: job["log"].append([round(time.time() - job["t0"], 1), msg])
    try:
        sw = _search_prelude(p)
        n_top, n_cheap = int(p.get("topN", 10)), int(p.get("cheapN", 10))
        info = WORKER.call("info")
        if not info.get("ok"):
            raise RuntimeError("PoB: " + str(info.get("error")))
        socks = info["data"].get("jewelSockets") or []
        if not socks:
            raise RuntimeError("В дереве нет взятых джевел-сокетов")
        target = p.get("target") or "auto"
        if target != "auto":
            t = next((s for s in socks if s["slot"] == target), None)
            if not t:
                raise RuntimeError("Этого сокета нет среди взятых в дереве")
            is_uniq = t.get("rarity") == "UNIQUE"
            # the chosen socket: anything that fits; a socket holding your unique keeps "copies only" unless asked otherwise
            repl = [] if is_uniq and not p.get("anyForUnique") else [t["slot"]]
            uniq = [t] if is_uniq else []
            log((f"Заменяем {t['item']}" if t.get("item") else "Пустой сокет")
                + (" — только копии этого уника" if is_uniq and not repl else ""))
        else:
            repl = [s["slot"] for s in socks if s.get("rarity") != "UNIQUE"]
            uniq = [s for s in socks if s.get("rarity") == "UNIQUE"]
            log(f"Сокетов: {len(socks)}; под редкие и новые уники: {len(repl)}; с вашими униками (только их копии): {len(uniq)}")
        rep_slot = next((x for x in repl if not next((s for s in socks if s["slot"] == x), {}).get("item")), repl[0] if repl else None)
        rows, parts = [], []

        def part(label, fn, quiet_no_weights=None):
            plog = lambda m: log(f"[{label}] {m}")
            try:
                out = fn(plog)
                parts.append(out)
                rows.extend(out["result"]["rows"])
            except Cancelled:
                raise
            except Exception as ex:
                if quiet_no_weights and str(ex).startswith("Ни один мод"):
                    plog(quiet_no_weights)
                else:
                    plog("Ошибка: " + str(ex))

        excl = list(set(p.get("exclude") or []) | {"unique"})
        if p.get("mode") == "unique":
            name, base = p.get("uniqueName"), p.get("uniqueBase")
            mine = [s for s in uniq if s.get("item") == name]
            slots = [mine[0]["slot"]] if mine else repl
            if not slots:
                raise RuntimeError("Нет сокета для этого уника: выбранный сокет занят вашим уником — отметьте «искать и другие джевелы» или выберите другой сокет")
            vs = []
            if not mine:
                uj = WORKER.call("uniqueJewels", sockets=slots, statWeights=sw, only=name)
                best = (uj.get("data") or {}).get("items") or []
                if best and best[0].get("variant"):
                    vs = variant_stats(best[0].get("variantLines"))
                    log(f"Лучший вариант по PoB: {best[0]['variant']}" + (f" + {best[0]['variantAlt']}" if best[0].get("variantAlt") else "")
                        + (f" ({pct_s(best[0]['gain'])})" if best[0].get("gain") is not None else ""))
            part(name, lambda plog: search_slot(dict(p, uniqueName=name, uniqueBase=base, variantStats=vs, exclude=[x for x in excl if x != "unique"]),
                                                slots[0], sw, plog, n_top, n_cheap, refine=False, eval_slots=slots, group="copy" if mine else "unique"))
        else:
            if repl:
                part("редкие", lambda plog: search_slot(dict(p, jewelBase="Diamond", jewelType=None, exclude=excl, ceilingMin=0.3),
                                                        rep_slot, sw, plog, n_top, n_cheap, refine=bool(p.get("refine")), eval_slots=repl, group="rare"),
                     quiet_no_weights="даже лучший редкий джевел не лучше текущего — пропускаю, на трейд не хожу")
                part("Time-Lost", lambda plog: search_slot(dict(p, jewelBase="Time-Lost Diamond", jewelType=None, exclude=excl, ceilingMin=0.3),
                                                           rep_slot, sw, plog, max(10, n_top // 2), max(10, n_cheap // 2), refine=False, eval_slots=repl, group="rare"),
                     quiet_no_weights="Time-Lost джевел здесь ничего заметного не даст (радиус не задевает полезных нод) — пропускаю, на трейд не хожу")
                # unique jewels of PoB's database, best variant in the best free socket; the strongest are looked up on trade
                uj = WORKER.call("uniqueJewels", sockets=repl, statWeights=sw,
                                 exclude=[s["item"] for s in socks if s.get("rarity") == "UNIQUE" and s["slot"] not in repl])
                ranked = [u for u in (uj.get("data") or {}).get("items", []) if u.get("gain") is not None and not u.get("skipped")]
                log("Уники из базы PoB (лучший вариант, лучший сокет): " + ", ".join(f"{u['name']} {pct_s(u['gain'])}" for u in ranked[:8]))
                top_gain = ranked[0]["gain"] if ranked else 0
                worth = [u for u in ranked if u["gain"] >= max(1.0, top_gain * 0.25)][:4]
                if ranked and not worth:
                    log("Ни один уник из базы PoB не даёт заметного прироста (от 1%) — на трейде их не ищу")
                for u in worth:
                    vs = variant_stats(u.get("variantLines")) if u.get("variant") else []
                    part(u["name"], lambda plog, u=u, vs=vs: search_slot(
                        dict(p, uniqueName=u["name"], uniqueBase=u["base"], variantStats=vs, exclude=[x for x in excl if x != "unique"]),
                        rep_slot, sw, plog, 10, 10, refine=False, eval_slots=repl, group="unique"))
            for s in uniq:
                part(f"ваш {s['item']}", lambda plog, s=s: search_slot(
                    dict(p, uniqueName=s["item"], uniqueBase=s.get("base"), exclude=[x for x in excl if x != "unique"]),
                    s["slot"], sw, plog, 10, 10, refine=False, eval_slots=[s["slot"]], group="copy"))
        if not parts:
            raise RuntimeError("Ничего не найдено")
        seen, uniq_rows = set(), []
        for r in sorted(rows, key=lambda r: -r["gain"]):
            key = r.get("raw")
            if key in seen:
                continue
            seen.add(key)
            uniq_rows.append(r)
        uniq_rows = collapse_same(uniq_rows)
        uniq_rows.sort(key=lambda x: (-x["gain"], x["priceDiv"] if x["priceDiv"] is not None else 1e18))
        best = -1e9
        for r in uniq_rows:
            r.pop("pareto", None)
        for x in sorted([r for r in uniq_rows if r["priceDiv"] and not r["block"]], key=lambda r: (r["priceDiv"], -r["gain"])):
            if x["gain"] > best and x["gain"] > 0:
                x["pareto"] = True
                best = x["gain"]
        first = parts[0]
        job["result"] = {"base": first["result"]["base"], "baseScore": first["result"]["baseScore"], "rows": uniq_rows, "metrics": sw,
                         "ignoreRes": bool(p.get("ignoreRes")), "slot": rep_slot, "jewels": True, "split": bool(p.get("split"))}
        job["tradeUrl"] = first["tradeUrl"]
        job["weights"] = first["weights"]
        log(f"Готово: {sum(1 for r in uniq_rows if r['gain'] > 0)} улучшений из {len(uniq_rows)}")
        job["status"] = "done"
    except Cancelled:
        job["status"] = "cancelled"
        log("Поиск отменён")
    except Exception as ex:
        job["status"] = "error"
        job["error"] = str(ex)
        log("Ошибка: " + str(ex))


def pct_s(v):
    return f"{v:+.1f}%"


def make_plan(p):
    """Purchase plan for a budget from a finished 'what to upgrade first' scan: one item per slot, the best total gain
    for the money (gains added up as an estimate), then the best few plans are measured in PoB with all items on."""
    j = JOBS.get(p.get("job"))
    if not j or not j.get("scan"):
        raise RuntimeError("Сначала запустите «Что улучшать первым» — план строится по его результатам")
    budget = float(p.get("budget") or 0)
    if budget <= 0:
        raise RuntimeError("Укажите бюджет в divine")
    sw = [w for w in p.get("statWeights", []) if w.get("weightMult", 0) > 0] or (j["scan"][0].get("search") or {}).get("result", {}).get("metrics")
    opts = []
    for e in j["scan"]:
        rows = ((e.get("search") or {}).get("result") or {}).get("rows") or []
        good = [r for r in rows if r.get("priceDiv") and r["priceDiv"] <= budget and r["gain"] > 0 and not r.get("block") and r.get("raw")]
        # per slot only offers that are not beaten by a cheaper, stronger one
        good.sort(key=lambda r: (r["priceDiv"], -r["gain"]))
        front, best = [], -1e9
        for r in good:
            if r["gain"] > best:
                front.append(r); best = r["gain"]
        if front:
            opts.append((e["slot"], front))
    if not opts:
        raise RuntimeError("В пределах бюджета нет улучшений без проблем (атрибуты, резисты)")
    # knapsack over slots, states pruned to the price/gain frontier
    states = [(0.0, 0.0, ())]
    for slot, front in opts:
        nxt = list(states)
        for price, gain, pick in states:
            for r in front:
                if price + r["priceDiv"] <= budget:
                    nxt.append((price + r["priceDiv"], gain + r["gain"], pick + ((slot, r),)))
        nxt.sort(key=lambda s: (s[0], -s[1]))
        pruned, best = [], -1e9
        for s_ in nxt:
            if s_[1] > best + 1e-9:
                pruned.append(s_); best = s_[1]
        states = pruned
    cands = sorted([s_ for s_ in states if s_[2]], key=lambda s_: -s_[1])[:4]
    plans = []
    for price, est, pick in cands:
        r = WORKER.call("evalCombo", statWeights=sw, heur=p.get("heur", True) is not False, items=[{"slot": slot, "raw": row["raw"]} for slot, row in pick])
        if not r.get("ok"):
            continue
        plans.append({"price": price, "estimate": est, "real": r["data"],
                      "items": [{"slot": slot, "name": row["name"], "gain": row["gain"], "price": row["price"], "priceDiv": row["priceDiv"],
                                 "link": row.get("link"), "raw": row["raw"], "view": row.get("view")} for slot, row in pick]})
    plans.sort(key=lambda x: -x["real"]["gain"])
    return {"budget": budget, "plans": plans}


# ---------------------------------------------------------------- crafting



def craft_prices(league):
    return craft_ninja(league)["prices"]


def craft_icons(league):
    return craft_ninja(league)["icons"]


def craft_ninja(league):
    def fetch():
        out, icons, raw = {}, {}, []
        for t in NINJA_TYPES:
            url = "https://poe.ninja/poe2/api/economy/exchange/current/overview?" + urllib.parse.urlencode({"league": league, "type": t})
            try:
                d, _ = http_json(url)
            except Exception:
                continue
            pv = {l["id"]: l.get("primaryValue") for l in d.get("lines", [])}
            vol = {l["id"]: (l.get("maxVolumeCurrency"), l.get("maxVolumeRate")) for l in d.get("lines", [])}
            for it in d.get("items", []):
                if pv.get(it["id"]):
                    out[it["name"]] = float(pv[it["id"]])
                    raw.append((it["name"], vol.get(it["id"])))
                if it.get("image"):
                    icons[it["name"]] = "https://web.poecdn.com" + it["image"]
        if not out:
            raise RuntimeError("poe.ninja не ответил — цены валюты недоступны")
        # a thinly traded item's headline value jumps around; the rate of the most traded exchange pair is what people
        # actually pay: when the two disagree by more than 3x, that rate wins
        cur = {"chaos": out.get("Chaos Orb"), "exalted": out.get("Exalted Orb"), "divine": 1.0}
        for name, (vc, vr) in raw:
            if vc in cur and cur[vc] and vr:
                real = cur[vc] / float(vr)
                if real > 0 and not (1 / 3 <= out[name] / real <= 3):
                    out[name] = real
        out["Divine Orb"] = 1.0
        return {"prices": out, "icons": icons}
    return cached("craftninja:" + league, 1800, fetch)





def craft_parse(p):
    raw = (p.get("raw") or "").strip()
    r = WORKER.call("craftData", raw=raw, base=p.get("base") or "")
    if not r.get("ok"):
        raise ValueError(r.get("error"))
    cd = r["data"]
    item = cd.get("item")
    ilvl = int(p.get("ilvl") or (item or {}).get("ilvl") or 82)
    rl = (item or {}).get("runeLines") or []
    runes = {k: any(t in l for l in rl) for k, t in META_RUNES.items()}
    kolr = bool(p.get("kolr")) or runes["kolr"] or cd["cls"] == "Gloves"
    nat, ess, alloys = CRAFT.catalog(cd, ilvl, kolr)
    targets, unknown = CRAFT.targets_from_item(cd, ilvl, kolr) if item else ([], [])
    for c in ess + alloys:
        c.pop("lines", None)
    # the same item as an editable "my item": rarity, sockets, runes and every recognised mod with its tier and kind
    start = None
    if item:
        start = {"rarity": item.get("rarity") or "RARE", "sockets": int(item.get("sockets") or 0),
                 "runes": [k for k, v in runes.items() if v],
                 "mods": [{"kind": t["kind"], "side": t["side"], "group": t["group"], "level": t["minLevel"], "text": t["text"],
                           "name": t.get("name"),
                           "how": "fr" if t.get("frac") else "cr" if t["kind"] in ("alloy", "essence") else
                                  "de" if t.get("des") and not t.get("lord") else ""} for t in targets]}
    return {"base": cd["base"], "cls": cd["cls"], "ilvl": ilvl, "runes": runes,
            "item": item and {k: item.get(k) for k in ("name", "rarity", "ilvl", "corrupted")},
            "targets": targets, "unknown": unknown, "catalog": nat + ess + alloys, "start": start,
            "quality": int((item or {}).get("catalystQuality") or 0), "baseInfo": cd.get("baseInfo"),
            "baseIcon": base_icon(cd["base"], p.get("league") or "Standard")}


_ICONS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cache", "base_icons.json")


def base_icon(base, league):
    """the base's picture: from one trade listing of it (the art is the same for every item of a base), kept on disk"""
    try:
        icons = json.load(open(_ICONS, encoding="utf-8"))
    except Exception:
        icons = {}
    if base in icons:
        return icons[base]
    url = None
    try:
        lg = urllib.parse.quote(league)
        q = {"query": {"status": {"option": "any"}, "type": base}, "sort": {"price": "asc"}}
        r = trade_call("search", f"{TRADE}/search/poe2/{lg}", q, None, None)
        if r.get("result"):
            f = trade_call("fetch", f"{TRADE}/fetch/{r['result'][0]}?query={r['id']}&realm=poe2", None, None, None)
            url = ((f.get("result") or [{}])[0] or {}).get("item", {}).get("icon")
    except Exception:
        url = None
    if url:
        icons[base] = url
        os.makedirs(os.path.dirname(_ICONS), exist_ok=True)
        json.dump(icons, open(_ICONS, "w", encoding="utf-8"))
    return url


def page_background():
    """the page background: static/bg.webp when there is one (not in the repository: it is Grinding Gear Games' art);
    without it the page keeps its plain dark background"""
    p = os.path.join(HERE, "static", "bg.webp")
    if os.path.exists(p):
        with open(p, "rb") as f:
            return f.read()
    return None


def craft_bases():
    r = WORKER.call("craftData", base="Vaal Gloves", listBases=True)
    if not r.get("ok"):
        raise ValueError(r.get("error"))
    return sorted(r["data"]["bases"], key=lambda b: (b["cls"], b["level"], b["name"]))




def run_craft(job, p):
    def log(m):
        if job["cancel"].is_set():
            raise Cancelled()
        job["log"] = [[round(time.time() - job["t0"], 1), m]]
    try:
        spec = p.get("start") if p.get("mode") == "current" and isinstance(p.get("start"), dict) else None
        current = not spec and p.get("mode") == "current" and (p.get("raw") or "").strip()
        r = WORKER.call("craftData", raw=current or "", base=p.get("base") or "")
        if not r.get("ok"):
            raise ValueError(r.get("error"))
        cd = r["data"]
        ilvl = int(p.get("ilvl") or (cd.get("item") or {}).get("ilvl") or 82)
        log("Цены poe.ninja")
        prices = craft_prices(p.get("league") or "Standard")
        base_price = float(p.get("basePrice") or 0)
        # runes are chosen by the planner; a pasted current item keeps (and does not pay again for) what is socketed
        rl = (cd.get("item") or {}).get("runeLines") or [] if current else []
        have = {k for k, t in META_RUNES.items() if any(t in l for l in rl)}
        start = cd.get("item") if current else None
        if spec:  # the item entered by hand (or the parsed one, edited)
            have = {k for k in spec.get("runes") or [] if k in META_RUNES}
            start = dict(spec, spec=True)
        pn = p.get("poolNeed") or 0
        pn = pn if isinstance(pn, dict) else ({"1": int(pn)} if pn else {})
        # pseudo goals (total resistances, empty affixes) as the trade site has them -> ordinary groups / open slots
        tg, pn, (op, os_), pmeta = CRAFT.expand_pseudo(cd, ilvl, p.get("targets") or [], pn)
        # a Count group that needs all of its mods is just "And" (one mod, need 1): those mods are plain requirements
        gid = lambda o: 0 if not o else 1 if o is True else int(o)
        for g in list(pn):
            mem = [t for t in tg if t.get("on", True) and gid(t.get("opt")) == int(g) and not t.get("pseudo_of")]
            if mem and len(mem) <= int(pn[g]) and not any(gid(t.get("opt")) == int(g) and t.get("pseudo_of") for t in tg):
                for t in mem:
                    t["opt"] = False
                pn.pop(g)
        prob = CRAFT.Problem(cd, tg, prices, ilvl, base_price,
                             start, runes_have=have, need_opt=pn,
                             open_affix=(max(op, int(p.get("openP") or 0)), max(os_, int(p.get("openS") or 0))))
        prob.no_putref = bool(p.get("noPutref"))
        # a goal pasted with catalyst quality (+40% caster): the craft has to reach it too
        qi = cd.get("item") if current else None
        if not current and not spec and (p.get("raw") or "").strip():
            try:
                qi = WORKER.call("craftData", raw=p["raw"].strip(), base="")["data"].get("item")
            except Exception:
                qi = None
        # quality is not asked: the pasted item's catalyst quality, or what a wanted mod's number needs (+4 spell levels on
        # an amulet whose best tier is +3); over 20% means the Breach essence route to 40%
        need = CRAFT.implied_quality(cd, ilvl, p.get("targets") or [])
        q = max(int((qi or {}).get("catalystQuality") or 0), need)
        prob.quality = 40 if 20 < q <= 40 else q
        final, alts, ctx = CRAFT.plan_auto(prob, budget=float(p.get("budget") or 0.35), fill=bool(p.get("fill")), progress=log)
        out = CRAFT.summarize(final, ctx)
        out.update(alts=alts, chances=CRAFT.chances(ctx, final["v"]), runes=ctx.runes_used, notes=prob.notes,
                   missingPrices=sorted(ctx.missing_prices), ilvl=ilvl, base=cd["base"],
                   icons=craft_icons(p.get("league") or "Standard"))
        job["ctx"], job["v"], job["cd"], job["mean"], job["median"] = ctx, final["v"], cd, out.get("mean"), out.get("median")
        # the goal as asked (with its "any K of" groups), for the trade check: not the concrete pick the plan went for
        job["goal"] = [{"src": t["fixed"] if t["fixed"] in ("alloy", "essence") else "natural", "side": t["side"], "group": t["group"],
                        "minLevel": t["minLevel"], "text": t["text"], "name": t.get("name"), "opt": t["grp"] or False}
                       for t, raw in zip(prob.T, prob.raw) if not raw.get("pseudo_of")]
        # a pseudo stat inside a Count group: the trade check uses the stat itself as one member of the group
        for pid, g, x in {(r["pseudo_of"], r["opt"], r["pseudo_min"]) for r in prob.raw if r.get("pseudo_of")}:
            job["goal"].append({"pseudo": pid, "min": x, "opt": g, "text": CRAFT.PSEUDO[pid][0]})
        job["need_by"] = dict(prob.need_by)
        job["group_meta"] = dict(p.get("groupMeta") or {}, **{str(g): m for g, m in pmeta.items()})  # pseudo groups: their stat
        job["open"] = prob.open
        job["pct"] = out.get("pct")
        job["result"], job["status"] = out, "done"
    except Cancelled:
        job["status"], job["error"] = "error", "Расчёт отменён"
    except ValueError as ex:
        job["status"], job["error"] = "error", str(ex)
    except Exception as ex:
        job["status"], job["error"] = "error", f"{type(ex).__name__}: {ex}"


_RANGE = re.compile(r"\((-?\d+(?:\.\d+)?)-(-?\d+(?:\.\d+)?)\)|(-?\d+(?:\.\d+)?)")


def _line_min(line):
    """lowest value a tier line allows; 'Adds a to b' counts as the average, as the trade site does"""
    nums = [float(m.group(1)) if m.group(1) is not None else float(m.group(3)) for m in _RANGE.finditer(line)]
    if not nums:
        return None
    v = sum(nums) / len(nums)
    return int(v) if v == int(v) else round(v, 1)


def _tier_lines(cd, t):
    """the target's lines at its lowest allowed tier (the item's own roll would be too strict a filter)"""
    if t["src"] in ("essence", "alloy"):
        return t["text"].replace(" или выше", "").split(" / ")
    for m in cd["pool"] + (cd.get("marksmanPool") or []) + (cd.get("desPool") or []):
        if m.get("side") == t["side"] and m["group"] == t["group"] and m["level"] == t["minLevel"]:
            return m["lines"]
    return t["text"].replace(" или выше", "").split(" / ")





def run_craft_trade(job, src, p):
    """items like the craft goal on the trade site (same base, every wanted mod at least at its tier) and their prices"""
    log = lambda msg: job["log"].append([round(time.time() - job["t0"], 1), msg])
    try:
        ctx, cd = src["ctx"], src["cd"]
        league, sess = p.get("league") or "Standard", p.get("poesessid") or None
        lg = urllib.parse.quote(league)
        by_t = {}
        for st in cached("stats", 6 * 3600, trade_stats):
            if st["type"] in ("Explicit", "Fractured", "Desecrated", "Crafted"):
                by_t.setdefault((_stat_tmpl(st["text"]), st["type"]), st["id"])
        lines, skipped = [], []
        need_by = src.get("need_by") or ({1: ctx.need_opt} if ctx.need_opt else {})
        goal = list(src.get("goal") or ctx.targets)
        t_stats = {}  # id(target) -> (stat numbers of its lines, all lines found on the trade site)
        for t in goal:
            if t.get("pseudo"):
                tm = "__pseudo__:" + t["pseudo"]
                by_t[(tm, "Explicit")] = t["pseudo"]
                lines.append((t, tm, float(t["min"])))
                continue
            for line in _tier_lines(cd, t):
                tm, v = _stat_tmpl(line), _line_min(line)
                # a local defence mod (ES / armour / evasion on the item itself) is its own "(Local)" stat on the trade site
                if str(t.get("group", "")).startswith("Local") and any((tm + " (local)", k) in by_t for k in
                                                                       ("Explicit", "Fractured", "Desecrated", "Crafted")):
                    tm = tm + " (local)"
                if not any((tm, k) in by_t for k in ("Explicit", "Fractured", "Desecrated", "Crafted")) or not v or v <= 0:
                    skipped.append(line)
                    ts = t_stats.setdefault(id(t), [set(), True]); ts[1] = False
                    continue
                lines.append((t, tm, v))
                ts = t_stats.setdefault(id(t), [set(), True])
                for k in ("Explicit", "Fractured", "Desecrated", "Crafted"):
                    if (tm, k) in by_t:
                        ts[0].add(re.search(r"stat_\d+", by_t[(tm, k)]).group(0))
        # the same stat from two wanted mods (+4 spell levels and an alloy's +1) is one value on the item: one filter for
        # the sum (+5), or a +3 would count twice and make up for mods it does not have
        merged, at = [], {}
        for t, tm, v in lines:
            if not t.get("opt") and tm in at:
                k = at[tm]
                merged[k] = (merged[k][0], tm, merged[k][2] + v)
                continue
            if not t.get("opt"):
                at[tm] = len(merged)
            merged.append((t, tm, v))
        lines = merged
        if not lines:
            raise RuntimeError("Ни один нужный мод не нашёлся в фильтрах трейда")
        need = sum(1 for t, _, _ in lines if not t.get("opt")) + sum(k for g, k in need_by.items()
                                                                     if any(t.get("opt") and (t["opt"] is True or t["opt"] == g) for t, _, _ in lines))
        slack = float(p.get("slack") or 0.9)

        def scoped(q, scope):
            if scope == "class":
                q["query"].pop("type", None)
                q["query"]["filters"]["type_filters"]["filters"]["category"] = {"option": TRADE_CAT[cd["cls"]]}
            return q

        meta = src.get("group_meta") or {}

        def group_filter(g, fs):
            m = meta.get(str(g)) or meta.get(g) or {}
            if m.get("pseudo"):  # a pseudo total (e.g. +#% total Elemental Resistance >= 60): that stat itself
                return {"type": "and", "filters": [{"id": m["pseudo"], "value": {"min": float(m.get("min") or 0)}}]}
            if m.get("type") == "sum":  # a sum of the group's values (e.g. total resistances), like the site's Sum
                return {"type": "sum", "filters": [{"id": f["id"]} for f in fs], "value": {"min": float(m.get("min") or 0)}}
            return {"type": "count", "filters": fs, "value": {"min": need_by.get(g) or need_by.get(str(g)) or 1}}

        def empty_filters():
            op, os_ = src.get("open") or (0, 0)
            f = []
            if op:
                f.append({"id": "pseudo.pseudo_number_of_empty_prefix_mods", "value": {"min": op}})
            if os_:
                f.append({"id": "pseudo.pseudo_number_of_empty_suffix_mods", "value": {"min": os_}})
            return [{"type": "and", "filters": f}] if f else []

        def floor_min(v):
            # every mod at least slack x its tier floor, rounded up for whole numbers: +5 spell levels stays 5 (4.5 -> 5),
            # 188 mana -> 170, 28% cast speed -> 26%
            x = v * slack
            return math.ceil(x - 1e-9) if float(v).is_integer() else round(x, 1)

        def count_groups(sid):
            """per Count group one filter per wanted mod, each stat once: a hybrid mod (% ES / + mana) is matched by its
            line that no other member of the group shares (+ mana), or the same % ES would count once per member and
            one ES mod would pass "any 2 of 4"; a stat two members share (rarity as prefix and suffix) is one filter
            with the lower minimum"""
            by_g = {}
            for t, tm, v in lines:
                k = sid(tm)
                if k and t.get("opt"):
                    by_g.setdefault(1 if t["opt"] is True else t["opt"], {}).setdefault(id(t), []).append((k, v))
            out = {}
            for g, mods in by_g.items():
                use = {}
                for key, ls in mods.items():
                    other = {k for kk, l2 in mods.items() if kk != key for k, _ in l2}
                    own = [(k, v) for k, v in ls if k not in other] or ls[:1]
                    for k, v in own[:1]:
                        use[k] = min(use.get(k, v), v)
                out[g] = [{"id": k, "value": {"min": floor_min(v)}} for k, v in use.items()]
            return out

        def build(kinds, scope):
            # weighted like the upgrade search: a mod at the low end of its tier counts 1, a better roll more; the item
            # needs `slack` of the full sum. And every mod on its own at least slack x its floor: a strong mod must not make
            # up for a weaker one (+4 spell levels instead of +5 is not "similar" however good the rest is)
            def sid(tm):
                k = next((k for k in kinds + ("Crafted", "Fractured", "Desecrated") if (tm, k) in by_t), None)
                return by_t[(tm, k)] if k else None
            wv, mins = {}, []
            for t, tm, v in lines:
                k = sid(tm)
                if k:
                    wv[k] = min(wv.get(k, v), v)  # each stat once (the site sums its value once)
                    if not t.get("opt"):
                        mins.append({"id": k, "value": {"min": floor_min(v)}})
            f = [{"id": k, "value": {"weight": round(1.0 / v, 6)}} for k, v in wv.items()]
            stats = [{"type": "weight", "filters": f, "value": {"min": round(need * slack, 3)}}]
            if mins:
                stats.append({"type": "and", "filters": mins})
            cnt = count_groups(sid)  # "any K of the group", each at least slack x its floor
            for g, fs in sorted(cnt.items()):
                stats.append(group_filter(g, fs))
            stats += empty_filters()
            return scoped({"query": {"status": {"option": "available"}, "type": cd["base"], "stats": stats,
                                     "filters": {"type_filters": {"filters": {"rarity": {"option": "nonunique"}}}}},
                           "sort": {"price": "asc"}}, scope)

        def build_plain(scope):
            # no weights (the cheapest query the site accepts without login): every mod at least slack × its tier floor
            def sid(tm):
                return by_t[(tm, "Explicit" if (tm, "Explicit") in by_t else "Crafted")]
            groups = [{"id": sid(tm), "value": {"min": floor_min(v)}} for t, tm, v in lines if not t.get("opt")]
            pool_f = count_groups(sid)
            stats = [{"type": "and", "filters": groups}] if groups else []
            for g, fs in sorted(pool_f.items()):  # "any K of these", one count filter per group, as on the site
                stats.append(group_filter(g, fs))
            stats += empty_filters()
            return scoped({"query": {"status": {"option": "available"}, "type": cd["base"], "stats": stats,
                                     "filters": {"type_filters": {"filters": {"rarity": {"option": "nonunique"}}}}},
                           "sort": {"price": "asc"}}, scope)
        rt = rates(league)

        def verify(it):
            """does a found item really have the goal: the site's Count cannot say "this member = these two stats" (a
            hybrid % ES / + life counts as + life alone, and life + mana pass as two ES mods), so every mod of the item is
            rebuilt from its lines (a hybrid is one mod on two lines) and matched to the wanted mods by its stats and tier
            level. None = the item has no mod details: cannot tell"""
            mods = {}
            for key in ("explicitMods", "fracturedMods", "desecratedMods", "craftedMods"):
                for m in it.get(key) or []:
                    if not isinstance(m, dict) or not m.get("mods"):
                        return None
                    st = re.search(r"stat_\d+", m.get("hash") or "")
                    for mm in m["mods"]:
                        e = mods.setdefault((key, mm.get("name"), mm.get("tier")), {"lv": mm.get("level") or 0, "st": set()})
                        if st:
                            e["st"].add(st.group(0))
            mem, free = [], {}
            for t in goal:
                g = None if not t.get("opt") else "1" if t["opt"] is True else str(t["opt"])
                if g is not None and (meta.get(g) or meta.get(int(g)) or {}).get("pseudo"):
                    continue  # a pseudo total (total resistances >= 75) split into mods for the craft: the site checks the total
                if t.get("pseudo") or id(t) not in t_stats or not t_stats[id(t)][0]:
                    if g is not None:  # a pseudo total in a Count: the site checks it, count it as there
                        free[g] = free.get(g, 0) + 1
                    continue
                need_st, full = t_stats[id(t)]
                cand = [k for k, e in mods.items() if (e["st"] == need_st if full else need_st <= e["st"])
                        and e["lv"] >= (t.get("minLevel") or 0)]
                mem.append((g, cand))
            match = {}  # mod -> member

            def aug(i, seen):
                for k in mem[i][1]:
                    if k in seen:
                        continue
                    seen.add(k)
                    if k not in match or aug(match[k], seen):
                        match[k] = i
                        return True
                return False
            for i in sorted(range(len(mem)), key=lambda i: (mem[i][0] is not None, len(mem[i][1]))):
                aug(i, set())
            got = set(match.values())
            if any(g is None and i not in got for i, (g, _) in enumerate(mem)):
                return False
            for g, k in need_by.items():
                g = str(g)
                if (meta.get(g) or meta.get(int(g) if g.isdigit() else g) or {}).get("pseudo"):
                    continue
                have = free.get(g, 0) + sum(1 for i in got if mem[i][0] == g)
                if any(mem[i][0] == g for i in range(len(mem))) and have < k:
                    return False
            return True

        def search(scope):
            # the trade site's explicit filter also matches fractured, desecrated and crafted mods (its weighted sum
            # shows it), so one id per mod; without login a weighted query may be too big: then plain minimums
            d, note, query = None, "", None
            for kinds, note in ((("Explicit",), ""), (None, "без весов: каждый мод не ниже 90% своего тира")):
                log("Ищу на трейде" + (" (все базы этого типа)" if scope == "class" else ""))
                try:
                    query = build(kinds, scope) if kinds else build_plain(scope)
                    d = trade_call("search", f"{TRADE}/search/poe2/{lg}", query, sess, log)
                    break
                except RuntimeError as ex:
                    if "too complex" not in str(ex):
                        raise
            if d is None:
                raise RuntimeError("Запрос слишком сложный для трейда. Вставьте POESESSID (вкладка «Поиск замены»): с входом лимит больше")
            total, qid0 = d.get("total") or 0, d.get("id")
            rows, checked, rejected, seen, last_ex, exhausted = [], 0, 0, set(), None, False
            # the site gives at most 100 results per search, the cheapest first; the Count filter lets through many items
            # without the wanted mods (a plain + life passes for a hybrid % ES / + life), so the real ones may come later:
            # the next 100 are a new search from the last price checked (no currency option = Exalted Orb equivalent)
            for page in range(3):
                ids = [i for i in d.get("result") or [] if i not in seen]
                qid = d.get("id")
                for at in range(0, len(ids), 10):  # the cheapest first, until 5 really match
                    if len(rows) >= 5:
                        break
                    log(f"Найдено {total}, проверяю самые дешёвые ({checked + 1}–{checked + len(ids[at:at + 10])})")
                    r = trade_call("fetch", f"{TRADE}/fetch/{','.join(ids[at:at + 10])}?query={qid}&realm=poe2", None, sess, log)
                    for x in r.get("result") or []:
                        if not x:
                            continue
                        seen.add(x.get("id"))
                        checked += 1
                        pr = (x.get("listing") or {}).get("price") or {}
                        if pr.get("amount") and pr.get("currency") in rt and rt.get("exalted"):
                            last_ex = pr["amount"] * rt[pr["currency"]] / rt["exalted"]
                        if verify(x["item"]) is False:
                            rejected += 1
                            continue
                        rows.append(x)
                if len(rows) >= 5:
                    break
                if len(d.get("result") or []) < 100 or checked >= total:
                    exhausted = True
                    break
                if last_ex is None or page == 2:
                    break
                q = json.loads(json.dumps(query))
                q["query"].setdefault("filters", {}).setdefault("trade_filters", {"filters": {}})["filters"]["price"] = {"min": math.floor(last_ex)}
                log(f"Найдено {total}, ищу следующие по цене")
                d = trade_call("search", f"{TRADE}/search/poe2/{lg}", q, sess, log)
                if not [i for i in d.get("result") or [] if i not in seen]:
                    exhausted = True
                    break
            found, rows = rows, []
            for x in found:
                pr = (x.get("listing") or {}).get("price") or {}
                amt, cur = pr.get("amount"), pr.get("currency")
                if not amt:
                    continue
                it = x["item"]
                def lines_of(key, kind):
                    out = []
                    for m in it.get(key) or []:
                        k = kind
                        if isinstance(m, dict):  # extended form: description with [Tag|Text] markup and flags
                            fl = m.get("flags") or {}
                            k = "fr" if fl.get("fractured") else "de" if fl.get("desecrated") else "cr" if fl.get("crafted") else kind
                            m = m.get("description") or ""
                        out.append({"t": re.sub(r"\[(?:[^|\]]*\|)?([^\]]*)\]", r"\1", m), "k": k})
                    return out
                mods = (lines_of("fracturedMods", "fr") + lines_of("explicitMods", "") + lines_of("desecratedMods", "de")
                        + lines_of("craftedMods", "cr"))
                acc = ((x.get("listing") or {}).get("account") or {}).get("name")
                # this seller's items of this base: the listing itself is among the first (like "Примерить" links)
                lq = {"query": {"status": {"option": "any"}, "type": it.get("typeLine"),
                                "filters": {"trade_filters": {"filters": {"account": {"input": acc}}}} if acc else {}},
                      "sort": {"price": "asc"}}
                rows.append({"name": it.get("name") or "", "base": it.get("typeLine") or "", "price": f"{amt} {cur}",
                             "div": amt * rt[cur] if cur in rt else None, "mods": [m["t"] for m in mods], "lines": mods,
                             "implicit": [l["t"] for l in lines_of("implicitMods", "")], "icon": it.get("icon"),
                             "ilvl": it.get("ilvl"), "rarity": it.get("rarity") or it.get("frameType"),
                             "corrupted": bool(it.get("corrupted")),
                             "link": f"https://www.pathofexile.com/trade2/search/poe2/{lg}?q=" +
                                     urllib.parse.quote(json.dumps(lq, separators=(",", ":")))})
            divs = sorted(x["div"] for x in rows if x["div"] is not None)
            return {"total": total, "rows": rows, "note": note, "cheapest": divs[0] if divs else None,
                    "checked": checked, "rejected": rejected, "exhausted": exhausted,
                    "url": f"https://www.pathofexile.com/trade2/search/poe2/{lg}/{qid0}" if qid0 else None}

        res = search("base")
        res.update(skipped=skipped, base=cd["base"], slack=slack, craftMean=src.get("mean"), craftMedian=src.get("median"),
                   craftPct=src.get("pct"))
        if res["total"] < 5 and cd["cls"] in TRADE_CAT:  # one base is too narrow a market: every base of this item type
            try:
                res["cls"] = dict(search("class"), label=cd["cls"])
            except RuntimeError as ex:
                log(f"Поиск по всем базам не вышел: {ex}")
        job["result"] = res
        job["status"] = "done"
    except Cancelled:
        job["status"] = "cancelled"
    except Exception as ex:
        job["status"], job["error"] = "error", str(ex)


CRAFT_LINKS = os.path.join(HERE, "cache", "craft_links.json")
_links_lock = threading.Lock()


def craft_link_save(state):
    """a short code for a craft setup (base, item level, wanted mods with tiers, pool); kept in cache/craft_links.json"""
    blob = json.dumps(state, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    if len(blob) > 20000:
        raise ValueError("Слишком большая ссылка")
    code = hashlib.sha1(blob.encode()).hexdigest()[:8]
    with _links_lock:
        try:
            links = json.load(open(CRAFT_LINKS, encoding="utf-8"))
        except Exception:
            links = {}
        links[code] = state
        os.makedirs(os.path.dirname(CRAFT_LINKS), exist_ok=True)
        json.dump(links, open(CRAFT_LINKS, "w", encoding="utf-8"), ensure_ascii=False)
    return code


def craft_link_load(code):
    try:
        return json.load(open(CRAFT_LINKS, encoding="utf-8")).get(code)
    except Exception:
        return None


def busy_trade_job():
    for j in list(JOBS.values()):
        if j["status"] == "running" and j.get("kind") == "trade":
            return j
    return None


def cleanup_jobs():
    now = time.time()
    for jid in [k for k, j in list(JOBS.items())
                if j["status"] != "running" and now - j["t0"] > (24 * 3600 if j.get("kind") == "craft" else 3600)]:
        JOBS.pop(jid, None)


MUTATING = {"/api/load", "/api/main", "/api/config"}
# requests that work on the loaded build: after a server restart PoB has none, though the page still shows it — the
# page then sends its build again (code NO_BUILD) and repeats the request
NEED_BUILD = {"/api/search", "/api/scan", "/api/jewels", "/api/compare", "/api/main", "/api/export", "/api/config",
              "/api/config/try", "/api/config/view", "/api/config/auto"}


# ---------------------------------------------------------------- HTTP
class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, obj, ctype="application/json"):
        body = obj if isinstance(obj, (bytes, bytearray)) else json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype + ("; charset=utf-8" if "json" in ctype or "html" in ctype else ""))
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "max-age=86400" if ctype.startswith("image/") else "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(n) or b"{}")

    def do_GET(self):
        try:
            if self.path.split("?")[0] in ("/", "/index.html"):
                with open(os.path.join(HERE, "static", "index.html"), "rb") as f:
                    return self._send(200, f.read(), "text/html")
            if self.path.split("?")[0] == "/bg.webp":  # the page background (the PoE2 site's own)
                data = page_background()
                return self._send(200, data, "image/webp") if data else self._send(404, {"error": "no background"})
            m = re.match(r"^/img/(.+)$", self.path)
            if m:
                name = urllib.parse.unquote(m.group(1))
                safe = re.sub(r"[^A-Za-z0-9 '\-,.]", "_", name)[:120]
                cdir = os.path.join(HERE, "cache", "img"); os.makedirs(cdir, exist_ok=True)
                fp = os.path.join(cdir, safe + ".webp")
                if not os.path.exists(fp) and not os.path.exists(fp + ".missing"):
                    try:
                        req = urllib.request.Request("https://assets.pobb.in/2/" + urllib.parse.quote(name) + ".webp", headers={"User-Agent": UA})
                        data = urllib.request.urlopen(req, timeout=20).read()
                        open(fp, "wb").write(data)
                    except urllib.error.HTTPError as e:
                        if e.code == 404:
                            open(fp + ".missing", "w").close()
                    except Exception:
                        pass
                if os.path.exists(fp):
                    with open(fp, "rb") as f:
                        return self._send(200, f.read(), "image/webp")
                return self._send(404, {"error": "no image"})
            if self.path == "/api/leagues":
                return self._send(200, {"leagues": cached("leagues", 3600, leagues)})
            if self.path == "/api/craft/bases":
                return self._send(200, {"bases": cached("craftbases", 24 * 3600, craft_bases)})
            if self.path == "/api/stats":
                return self._send(200, {"stats": cached("stats", 6 * 3600, trade_stats)})
            m = re.match(r"^/api/craft/link/([a-f0-9]{8})$", self.path)
            if m:
                st = craft_link_load(m.group(1))
                return self._send(200, {"state": st}) if st else self._send(404, {"error": "Ссылка не найдена"})
            m = re.match(r"^/api/job/([a-f0-9-]+)$", self.path)
            if m and m.group(1) in JOBS:
                j = JOBS[m.group(1)]
                out = {k: j.get(k) for k in ("status", "log", "error", "result", "weights", "tradeUrl")}
                if "scan" in j:  # full per-slot results only once the scan is finished (keeps polling light)
                    out["scan"] = [e if j["status"] != "running" else {k: v for k, v in e.items() if k != "search"} for e in j["scan"]]
                return self._send(200, out)
            self._send(404, {"error": "not found"})
        except Exception as ex:
            self._send(500, {"error": str(ex)})

    def do_POST(self):
        try:
            # only this app's own page may drive the server (another site open in the browser must not)
            origin = self.headers.get("Origin")
            if origin and origin not in (f"http://127.0.0.1:{PORT}", f"http://localhost:{PORT}"):
                return self._send(403, {"ok": False, "error": "forbidden origin"})
            p = self._body()
            if self.path in NEED_BUILD and WORKER.loaded_xml is None:
                return self._send(409, {"ok": False, "code": "NO_BUILD",
                                        "error": "Сервер перезапускался — билд нужно загрузить заново"})
            if self.path in MUTATING and busy_trade_job():
                return self._send(409, {"ok": False, "error": "Идёт поиск на трейде — дождитесь окончания или отмените его: иначе часть предметов посчитается против изменённого билда"})
            if self.path == "/api/load":
                xml = decode_build(p["input"])
                r = WORKER.call("load", xml=xml)
                return self._send(200 if r.get("ok") else 400, r)
            if self.path == "/api/main":
                r = WORKER.call("setmain", index=int(p["index"]), active=int(p["active"]) if p.get("active") else None)
                return self._send(200 if r.get("ok") else 400, r)
            if self.path == "/api/compare":
                r = WORKER.call("compare", slot=p["slot"], raw=p["raw"])
                return self._send(200 if r.get("ok") else 400, r)
            if self.path == "/api/parse":
                r = WORKER.call("parse", raw=p["raw"])
                return self._send(200 if r.get("ok") else 400, r)
            if self.path == "/api/export":
                r = WORKER.call("export")
                if not r.get("ok"):
                    return self._send(400, r)
                code = base64.urlsafe_b64encode(zlib.compress(r["data"]["xml"].encode("utf-8"), 9)).decode()
                return self._send(200, {"ok": True, "code": code})
            if self.path == "/api/plan":
                return self._send(200, {"ok": True, "data": make_plan(p)})
            if self.path == "/api/craft/link":
                try:
                    return self._send(200, {"code": craft_link_save(p.get("state") or {})})
                except ValueError as ex:
                    return self._send(400, {"error": str(ex)})
            if self.path == "/api/craft/trade":
                src = JOBS.get(p.get("job"))
                if not src or "ctx" not in src:
                    return self._send(400, {"error": "Расчёт не найден (сервер перезапускался?) — нажмите «Рассчитать» ещё раз"})
                if busy_trade_job():
                    return self._send(409, {"error": "Уже идёт поиск на трейде — дождитесь его"})
                jid = str(uuid.uuid4())
                JOBS[jid] = {"status": "running", "log": [], "t0": time.time(), "cancel": threading.Event(), "kind": "trade"}
                threading.Thread(target=run_craft_trade, args=(JOBS[jid], src, p), daemon=True).start()
                return self._send(200, {"job": jid})
            if self.path == "/api/craft/step":
                j = JOBS.get(p.get("job"))
                if not j or "ctx" not in j:
                    return self._send(400, {"ok": False, "error": "Расчёт не найден (сервер перезапускался?) — нажмите «Рассчитать» ещё раз"})
                try:
                    return self._send(200, {"ok": True, "data": CRAFT.step_mode(j["ctx"], j["v"], [int(x) for x in p.get("answers") or []])})
                except ValueError as ex:
                    return self._send(400, {"ok": False, "error": str(ex)})
            if self.path == "/api/craft/parse":
                try:
                    return self._send(200, {"ok": True, "data": craft_parse(p)})
                except ValueError as ex:
                    return self._send(400, {"ok": False, "error": str(ex)})
            if self.path == "/api/craft":
                cleanup_jobs()
                for j in list(JOBS.values()):  # one craft at a time: an older one would halve the speed of the new one
                    if j["status"] == "running" and j.get("kind") == "craft":
                        j["cancel"].set()
                jid = str(uuid.uuid4())
                JOBS[jid] = {"status": "running", "log": [], "t0": time.time(), "cancel": threading.Event(), "kind": "craft"}
                threading.Thread(target=run_craft, args=(JOBS[jid], p), daemon=True).start()
                return self._send(200, {"job": jid})
            if self.path == "/api/config":
                kw = {k: p[k] for k in ("vars", "custom", "extras", "reset", "clearInvalid", "auto") if k in p}
                r = WORKER.call("setConfig", **kw)
                return self._send(200 if r.get("ok") else 400, r)
            if self.path in ("/api/config/view", "/api/config/auto"):
                r = WORKER.call("configView" if self.path.endswith("view") else "configAuto")
                return self._send(200 if r.get("ok") else 400, r)
            if self.path == "/api/config/try":
                r = WORKER.call("configTry", var=p["var"], value=p.get("value"))
                return self._send(200 if r.get("ok") else 400, r)
            if self.path in ("/api/search", "/api/scan", "/api/jewels"):
                jid = str(uuid.uuid4())
                cleanup_jobs()
                JOBS[jid] = {"status": "running", "log": [], "t0": time.time(), "cancel": threading.Event(), "kind": "trade"}
                fn = {"/api/scan": run_scan, "/api/jewels": run_jewels}.get(self.path, run_search)

                def runner(job=JOBS[jid], fn=fn):
                    _ctx.cancel = job["cancel"]
                    fn(job, p)
                threading.Thread(target=runner, daemon=True).start()
                return self._send(200, {"job": jid})
            if self.path == "/api/cancel":
                j = JOBS.get(p.get("job"))
                if j and j["status"] == "running":
                    j["cancel"].set()
                return self._send(200, {"ok": True})
            self._send(404, {"error": "not found"})
        except Exception as ex:
            self._send(500, {"ok": False, "error": str(ex)})


if __name__ == "__main__":
    url = f"http://127.0.0.1:{PORT}"
    # OPEN_BROWSER=1 (start.bat / start.command): open the page; if the app already runs, just open it
    want_browser = os.environ.get("OPEN_BROWSER") == "1"
    try:
        srv = ThreadingHTTPServer(("127.0.0.1", PORT), H)
    except OSError:
        if want_browser:
            import webbrowser
            webbrowser.open(url)
            print(f"Already running: {url}")
            raise SystemExit(0)
        raise
    print(f"PoE2 optimizer: {url}")
    if want_browser:
        import webbrowser
        threading.Timer(0.5, webbrowser.open, args=(url,)).start()
    srv.serve_forever()
