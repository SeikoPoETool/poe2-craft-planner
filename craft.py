"""Crafting planner for PoE2 (0.5 Forbidden Rites rules).

Mod tiers come from Path of Building. Spawn weights are not in the game files; poe2db publishes them per
item class (engine/mod_weights.json, e.g. 1000 / 500 on hybrid bases / lower for top tiers). Mods without a
published weight (Kolr's Marksman mods, Abyssal lord mods) get the mean published weight of tiers of the same
rank (1st, 2nd... from the top) on that base type. Strategies are played out many times (Monte Carlo) with poe.ninja prices and
the cheapest expected cost wins; which mod is fractured, desecrated or made by an essence is chosen the same
way. Alloy -> modifier per item class comes from poe2db (engine/alloys.json).

Rules modelled:
- magic item: 1 prefix + 1 suffix; rare: 3 + 3 (Serle's Triumph: +1 suffix);
- one mod per group; Greater/Perfect currency only rolls mods of level >= 35/50 (Transmute/Augment 44/70);
- 1 crafted mod (essence, alloy); Astrid's Creativity: +1; 1 desecrated mod;
- Perfect / corrupted essences and alloys remove a random mod (Crystallisation omen: only that side;
  with nothing removable on that side nothing is removed) and add their mod;
- Fracturing Orb: rare with >= 4 mods, locks a random one; an unrevealed desecrated mod is not picked;
- desecration: bone adds an unrevealed mod; revealing offers 3 mods of that side (Abyssal Echoes: reroll once).
"""
import bisect, copy, itertools, json, math, os, random, re
from concurrent.futures import ProcessPoolExecutor

from gamedata import *  # noqa: F401,F403 — game rules and names (currency, omens, bones, classes, limits)

HERE = os.path.dirname(os.path.abspath(__file__))
try:
    ALLOYS = json.load(open(os.path.join(HERE, "engine", "alloys.json"), encoding="utf-8"))
except Exception:
    ALLOYS = {}
try:
    MODW = json.load(open(os.path.join(HERE, "engine", "mod_weights.json"), encoding="utf-8"))
except Exception:
    MODW = {}


def weight_page(cls, tags):
    p = PAGE.get(cls)
    if p in ("Gloves", "Helmets", "Body_Armours", "Boots", "Shields"):
        kind = "shield" if p == "Shields" else "armour"
        a = next((a for a in ATTR if a + "_" + kind in tags), None)
        p = p + "_" + a if a else p
    return p


def rank_weights(cd, known):
    """mean published weight of the 1st, 2nd, 3rd... tier from the top over the families of this base type"""
    fam, by = {}, {}
    for m in cd["pool"]:
        if m["id"] in known and m.get("side"):
            fam.setdefault((m["group"], m["side"]), []).append((m["level"], known[m["id"]]))
    for v in fam.values():
        v.sort(reverse=True)
        for r, (_, w) in enumerate(v):
            by.setdefault(min(r, 4), []).append(w)
    out = {r: round(sum(x) / len(x) / 50) * 50 for r, x in by.items()}
    return [out.get(r, 750) for r in range(5)]


def weigher(cd):
    """weight of a mod tier: the poe2db value for this base type; a tier without one (Kolr's Marksman mods,
    Abyssal lord mods) gets the mean published weight of tiers of the same rank (1st, 2nd, ... from the top)"""
    known = MODW.get(weight_page(cd["cls"], set(cd.get("tags") or [])), {})
    rw = rank_weights(cd, known)
    est = {}
    for lst in (cd["pool"], cd.get("marksmanPool") or [], cd.get("desPool") or []):
        fam = {}
        for m in lst:
            if m["id"] not in known and m.get("side"):
                fam.setdefault((m["group"], m["side"]), []).append(m)
        for ms in fam.values():
            ms.sort(key=lambda m: -m["level"])
            for k, m in enumerate(ms):
                est[m["id"]] = rw[min(k, 4)]
    return lambda m: WEIGHT_OVERRIDE.get(m["id"]) or known.get(m["id"]) or est.get(m["id"], rw[0])



def orhigher(text):
    """' (или выше)' unless the mod text already says so"""
    return "" if "или выше" in text else " (или выше)"
ABYSS_ESS = "Essence of the Abyss"
ABYSS_MARK = "__abyss_mark__"  # group of the Mark of the Abyssal Lord an Essence of the Abyss leaves (a crafted mod)
EXPENSIVE = 1.0     # div: essences / alloys at least this dear go in only when every other wanted mod is on the item
CAP = 2500          # currency uses per simulated craft before it counts as failed (spam loops are sampled at once)
FIRST_KEYS = ("first", "fget", "fmode", "facc", "keep", "restart", "cslam")  # strategy keys of the first phase
CAPK = 8            # screening: a craft that already spent CAPK x the best known strategy's price is cut off (it loses anyway)
PREF_RUNE_PRICE = {}  # filled per call


def is_rare_essence(name):
    return name.startswith("Perfect ") or name in CORRUPT_ESS


# ---------------------------------------------------------------- matching item text to mods
_NUM = re.compile(r"\((-?\d+(?:\.\d+)?)-(-?\d+(?:\.\d+)?)\)|(\d+(?:\.\d+)?)")


def _tmpl(line):
    rx, ranges, pos = "", [], 0
    for m in _NUM.finditer(line):
        rx += re.escape(line[pos:m.start()])
        if m.group(3) is not None:
            v = float(m.group(3)); ranges.append((v, v))
        else:
            ranges.append((float(m.group(1)), float(m.group(2))))
        rx += r"(-?\d+(?:\.\d+)?)"
        pos = m.end()
    rx += re.escape(line[pos:])
    return re.compile("^" + rx + "$", re.I), ranges


_tcache = {}


def tmpl(line):
    t = _tcache.get(line)
    if t is None:
        t = _tcache[line] = _tmpl(line)
    return t


def line_fit(tline, text, scale=1.0):
    """None if the text is not this template; else how far the numbers are outside the tier range (0 = inside).
    scale: catalyst quality on the item ("Quality (Caster Modifiers): +40%" shows caster mods x1.4)"""
    rx, ranges = tmpl(tline)
    m = rx.match(text)
    if not m:
        return None
    off = 0.0
    for v, (a, b) in zip((float(x) for x in m.groups()), ranges):
        lo, hi = min(a, b), max(a, b)
        if scale != 1.0:  # the game shows the scaled value rounded down (3 x 1.4 = 4.2 -> +4)
            lo, hi = math.floor(lo * scale), hi * scale
        if v < lo:
            off += (lo - v) / max(abs(lo), 1)
        elif v > hi:
            off += (v - hi) / max(abs(hi), 1)
    return off


def match_lines(lines, cands, multi_first=True, used=None):
    """lines: [{line, ...}] -> list of (cand, [line indexes]); hybrid (2-line) mods need both lines in range"""
    used = set() if used is None else used
    out = []

    def multi():
        for c in cands:
            if len(c["lines"]) < 2:
                continue
            idx, tot = [], 0.0
            for tl in c["lines"]:
                best = None
                for i, l in enumerate(lines):
                    if i in used or i in idx:
                        continue
                    f = line_fit(tl, l["line"], c.get("_scale", 1.0))
                    if f is not None and (best is None or f < best[1]):
                        best = (i, f)
                if best is None:
                    break
                idx.append(best[0]); tot += best[1]
            if len(idx) == len(c["lines"]) and tot == 0:
                used.update(idx); out.append((c, idx))

    def single(exact):
        for i, l in enumerate(lines):
            if i in used:
                continue
            best = None
            for c in cands:
                if len(c["lines"]) != 1:
                    continue
                f = line_fit(c["lines"][0], l["line"], c.get("_scale", 1.0))
                if f is None or (exact and f > 0):
                    continue
                if best is None or f < best[1]:
                    best = (c, f)
            if best:
                used.add(i); out.append((best[0], [i]))

    if multi_first:
        multi(); single(False)
    else:
        single(True); multi(); single(False)
    return out


def rebalance(nfound, cfound, lines, tiers, caps):
    """more mods on one affix type than the base allows (Absent Amulet: 2 + 2) while a mod reads the same on the other
    type (Rarity is both a prefix and a suffix on amulets): move it there"""
    n = {"P": 0, "S": 0}
    for c, _ in nfound + cfound:
        n[c["side"]] += 1
    groups = {(c["side"], c["group"]) for c, _ in nfound + cfound}
    for side, other in (("P", "S"), ("S", "P")):
        k = 0
        while n[side] > caps[side] and n[other] < caps[other] and k < len(nfound):
            c, idx = nfound[k]
            k += 1
            if c["side"] != side or len(idx) != 1 or lines[idx[0]].get("fractured") or lines[idx[0]].get("desecrated"):
                continue
            alt = [t for t in tiers if t["side"] == other and len(t["lines"]) == 1 and (other, t["group"]) not in groups
                   and line_fit(t["lines"][0], lines[idx[0]]["line"], t.get("_scale", 1.0)) == 0]
            if alt:
                a = max(alt, key=lambda t: t["level"])
                nfound[k - 1] = (a, idx)
                groups.add((other, a["group"]))
                n[side] -= 1; n[other] += 1
    return nfound


def implied_quality(cd, ilvl, targets):
    """catalyst quality a wanted mod needs: its number is above every tier of the base (+4 spell levels on an amulet,
    whose best tier is +3, only exists as +3 at 34%+ caster quality). Percent, 0 when none needs it"""
    nat, ess, alloys = catalog(cd, ilvl, False)
    fam = {(f["group"], f["side"]): f for f in nat}
    need = 0
    for t in targets:
        f = fam.get((t.get("group"), t.get("side")))
        if not f or not f["tiers"]:
            continue
        top = f["tiers"][0]["text"]
        m = tmpl(top)[0].match(t.get("text", "").replace(" или выше", ""))
        if not m:
            continue
        for v, (a, b) in zip((float(x) for x in m.groups()), tmpl(top)[1]):
            hi = max(a, b)
            if hi > 0 and v > hi:
                need = max(need, math.ceil((v / hi - 1) * 100 - 1e-9))
    return need




def expand_pseudo(cd, ilvl, targets, need_by):
    """pseudo goals -> ordinary ones. A total (resistances >= 60) becomes "any k of these mods at a tier whose floor is
    >= 60 / k", with the k that is easiest to roll (2 at 30+ or 3 at 20+...); empty affixes become open slots.
    Returns (targets, need_by, open (P, S), meta {group: {"pseudo": id, "min": x}})"""
    nat, _, _ = catalog(cd, ilvl, False)
    fam = {f["group"]: f for f in nat if not f.get("desOnly")}
    pool = Pool(cd["pool"], ilvl, weigher(cd))
    out, need, meta, op, os_ = [], dict(need_by), {}, 0, 0
    gid = max([int(g) for g in need] + [int(t.get("opt") or 0) if t.get("opt") is not True else 1 for t in targets] + [0]) + 1
    for t in targets:
        if t.get("kind") != "pseudo":
            out.append(t); continue
        pid, x = t.get("id"), float(t.get("min") or 0)
        if pid not in PSEUDO or x <= 0:
            continue
        g0 = 0 if not t.get("opt") else 1 if t.get("opt") is True else int(t["opt"])
        if g0:
            # inside a Count group the pseudo stat is one more way to fill the group: a single mod that reaches it alone
            # (total elemental resistance >= 40 -> any one resistance of 41+); a total that needs several mods would be a
            # group inside a group, which the craft does not model
            if "empty" in pid:
                raise ValueError(f"«{PSEUDO[pid][0]}» — только в блоке «И»")
            mem = [fam[g] for g in PSEUDO[pid][1] if g in fam]
            got = 0
            for f in mem:
                tiers = [tt for tt in f["tiers"] if _low(tt["text"]) >= x - 1e-9]
                if tiers:
                    tt = min(tiers, key=lambda z: z["level"])
                    out.append({"kind": "natural", "side": f["side"], "group": f["group"], "minLevel": tt["level"],
                                "text": tt["text"] + " или выше", "opt": g0, "on": True, "pseudo_of": pid, "pseudo_min": x})
                    got += 1
            if not got:
                raise ValueError(f"«{PSEUDO[pid][0]}» не ниже {x:g} одним модом не набрать, а сумма нескольких модов внутри "
                                 "группы Count пока не считается — поставьте этот псевдо-мод в блок «И»")
            continue
        if pid.endswith("empty_prefix_mods"):
            op = max(op, int(x)); continue
        if pid.endswith("empty_suffix_mods"):
            os_ = max(os_, int(x)); continue
        mem = [fam[g] for g in PSEUDO[pid][1] if g in fam]
        best = None
        for k in range(1, min(3, len(mem)) + 1):
            pick, ps = [], []
            for f in mem:
                tiers = [tt for tt in f["tiers"] if _low(tt["text"]) >= x / k - 1e-9]
                if not tiers:
                    continue
                tt = min(tiers, key=lambda z: z["level"])  # the lowest tier that is enough
                pick.append((f, tt))
                ps.append(pool.W(f["group"], f["side"], 0, tt["level"]) / max(pool.T(f["side"], 0), 1e-9))
            if len(pick) >= k and sum(ps) > 0:
                # rough difficulty: collecting k different ones (each next one has fewer left to hit), and every extra
                # mod takes an affix slot (x2 each)
                avg = sum(ps) / len(ps)
                diff = sum(1 / ((len(ps) - j) * avg) for j in range(k)) * 2 ** (k - 1)
                if best is None or diff < best[0]:
                    best = (diff, k, pick)
        if best is None:
            raise ValueError(f"«{PSEUDO[pid][0]}» не ниже {x:g}: на этой базе так не получить")
        _, k, pick = best
        for f, tt in pick:
            out.append({"kind": "natural", "side": f["side"], "group": f["group"], "minLevel": tt["level"],
                        "text": tt["text"] + " или выше", "opt": gid, "on": True})
        need[gid] = k
        meta[gid] = {"pseudo": pid, "min": x}
        gid += 1
    return out, need, (op, os_), meta


def _low(text):
    m = re.search(r"\((-?\d+(?:\.\d+)?)-", text) or re.search(r"(-?\d+(?:\.\d+)?)", text)
    return abs(float(m.group(1))) if m else 0.0


def quality_scale(item, cands):
    """mark each candidate with the catalyst quality factor that applies to it on this item"""
    tags, q = set((item or {}).get("catalystTags") or []), (item or {}).get("catalystQuality") or 0
    for c in cands:
        c["_scale"] = 1 + q / 100 if q and tags & set(c.get("tags") or []) else 1.0
    return cands


def catalog(cd, ilvl, kolr):
    """everything that can be asked for on this base: natural families (with tiers), essences, alloys"""
    fam = {}
    for m in cd["pool"] + (cd["marksmanPool"] if kolr else []):
        if not m.get("side") or m["level"] > ilvl:
            continue
        f = fam.setdefault((m["group"], m["side"]), {"kind": "natural", "group": m["group"], "side": m["side"],
                                                     "tiers": [], "marksman": bool(m.get("marksman"))})
        f["tiers"].append({"level": m["level"], "text": " / ".join(m["lines"]), "id": m["id"], "tags": m.get("tags") or []})
    for m in cd.get("desPool") or []:
        if not m.get("side") or m["level"] > ilvl:
            continue
        f = fam.setdefault((m["group"], m["side"], "d"), {"kind": "natural", "group": m["group"], "side": m["side"],
                                                          "tiers": [], "lord": m.get("lord"), "desOnly": True})
        f["tiers"].append({"level": m["level"], "text": " / ".join(m["lines"]), "id": m["id"], "tags": m.get("tags") or []})
    nat = []
    for f in fam.values():
        f["tiers"].sort(key=lambda t: -t["level"])
        f["text"] = f["tiers"][0]["text"]
        nat.append(f)
    ess = [{"kind": "essence", "name": e["name"], "side": e["side"], "group": e["group"], "level": e["level"],
            "text": " / ".join(e["lines"]), "lines": e["lines"], "magic": not is_rare_essence(e["name"]), "id": e["id"],
            "tags": e.get("tags") or []}
           for e in cd["essences"] if e.get("side")]
    amap = {a["id"]: a for a in cd["alloys"]}
    alloys = []
    for name, per in ALLOYS.items():
        a = amap.get(per.get(cd["cls"]))
        if a and a.get("side"):
            alloys.append({"kind": "alloy", "name": name, "side": a["side"], "group": a["group"], "level": a["level"],
                           "text": " / ".join(a["lines"]), "lines": a["lines"], "id": a["id"], "tags": a.get("tags") or []})
    return nat, ess, alloys


def _fit(c, lines, free, prefer):
    """line indexes for every line of an essence/alloy (all within range), preferring lines in `prefer`"""
    idx = []
    for tl in c["lines"]:
        best = None
        for i in free:
            if i in idx:
                continue
            f = line_fit(tl, lines[i]["line"], c.get("_scale", 1.0))
            if f is None or f > 0:
                continue
            k = (i not in prefer, i)
            if best is None or k < best[0]:
                best = (k, i)
        if best is None:
            return None
        idx.append(best[1])
    return idx


def _naturals(lines, tiers, free):
    """natural tiers on the free lines, one mod per group (the higher tier keeps the group)"""
    got = match_lines(lines, tiers, False, used=set(range(len(lines))) - set(free))
    got.sort(key=lambda x: -x[0]["level"])
    seen, out = set(), []
    for c, idx in got:
        if c["group"] in seen:
            continue
        seen.add(c["group"]); out.append((c, idx))
    return out


def split_lines(lines, tiers, crafts):
    """item lines -> ([(natural tier, idx)], [(essence/alloy, idx)]).
    Lines marked crafted go to essences/alloys. A plain Ctrl+C copy has no marks: then a line no natural tier explains
    (or a second line of a group already taken) must be part of an essence/alloy, which may also take a line that
    looked natural (an Alloy hybrid like Cast Speed + extra Cold). Picked so that the fewest lines stay unknown and
    sides fit 3+3 with at most 2 crafted mods."""
    n = len(lines)
    marked = {i for i, l in enumerate(lines) if l.get("crafted")}
    if marked:
        cr = match_lines(lines, crafts, True, used=set(range(n)) - marked)
        taken = {i for _, idx in cr for i in idx}
        return _naturals(lines, tiers, [i for i in range(n) if i not in taken and i not in marked]), cr
    nat0 = _naturals(lines, tiers, range(n))
    left = set(range(n)) - {i for _, idx in nat0 for i in idx}
    if not left:
        return nat0, []
    opts = []
    for c in crafts:
        idx = _fit(c, lines, range(n), left)
        if idx and left & set(idx):
            opts.append((c, idx))

    def score(cr):
        taken = {i for _, idx in cr for i in idx}
        nat = _naturals(lines, tiers, [i for i in range(n) if i not in taken])
        known = taken | {i for _, idx in nat for i in idx}
        side = {"P": 0, "S": 0}
        for c, _ in nat + cr:
            side[c["side"]] += 1
        over = max(side["P"] - 3, 0) + max(side["S"] - 4, 0) + 0.5 * max(side["S"] - 3, 0)
        return (n - len(known), over, len(cr), -sum(len(i) for _, i in cr)), nat

    best = (score([])[0], nat0, [])
    for a in range(len(opts)):
        for b in [None] + list(range(a + 1, len(opts))):
            cr = [opts[a]] + ([opts[b]] if b is not None else [])
            if b is not None and (set(opts[a][1]) & set(opts[b][1]) or opts[a][0]["group"] == opts[b][0]["group"]):
                continue
            k, nat = score(cr)
            if k < best[0]:
                best = (k, nat, cr)
    return best[1], best[2]


def targets_from_item(cd, ilvl, kolr):
    """the pasted item's explicit mods as craft targets (fractured / crafted / desecrated kept as such)"""
    nat, ess, alloys = catalog(cd, ilvl, kolr)
    lines = cd["item"]["mods"]
    tiers = [dict(t, side=f["side"], group=f["group"], lines=t["text"].split(" / "), desOnly=f.get("desOnly"),
                  lord=f.get("lord"), marksman=f.get("marksman")) for f in nat for t in f["tiers"]]
    quality_scale(cd["item"], tiers); quality_scale(cd["item"], ess + alloys)
    nfound, cfound = split_lines(lines, tiers, ess + alloys)
    ad = cd.get("affixDelta") or {}
    rebalance(nfound, cfound, lines, tiers, {"P": AFFIX_MAX["P"] + int(ad.get("P") or 0), "S": AFFIX_MAX["S"] + int(ad.get("S") or 0)})
    used, out = set(), []
    for c, idx in cfound:
        used.update(idx)
        out.append({"kind": c["kind"], "name": c["name"], "side": c["side"], "group": c["group"], "minLevel": c["level"],
                    "text": " / ".join(lines[i]["line"] for i in idx), "magic": c.get("magic", False), "lines": idx})
    for c, idx in nfound:
        used.update(idx)
        l0 = lines[idx[0]]
        out.append({"kind": "natural", "side": c["side"], "group": c["group"], "minLevel": c["level"],
                    "text": " / ".join(lines[i]["line"] for i in idx), "frac": bool(l0.get("fractured")),
                    "des": bool(l0.get("desecrated")) or bool(c.get("desOnly")), "lord": c.get("lord"),
                    "marksman": bool(c.get("marksman")), "lines": idx})
    unknown = [lines[i]["line"] for i in range(len(lines)) if i not in used]
    out.sort(key=lambda t: min(t["lines"]))
    return out, unknown


# ---------------------------------------------------------------- pool
class Pool:
    def __init__(self, mods, ilvl, wfn):
        self.g = {"P": {}, "S": {}}
        for m in mods:
            s = m.get("side")
            if s in self.g and m["level"] <= ilvl:
                self.g[s].setdefault(m["group"], []).append((m["level"], wfn(m)))
        for s in self.g:
            for g in self.g[s]:
                self.g[s][g].sort()
        self._T, self._W, self._J = {}, {}, {}
        self.tgroups = {"P": set(), "S": set()}

    def junk_level(self, s, f, rng):
        """level of a random non-target mod (needed only for Omen of Whittling)"""
        k = (s, f)
        j = self._J.get(k)
        if j is None:
            cum, lv, acc = [], [], 0.0
            for g, tiers in self.g[s].items():
                if g in self.tgroups[s]:
                    continue
                for l, w in tiers:
                    if l >= f:
                        acc += w; cum.append(acc); lv.append(l)
            j = self._J[k] = (cum, lv)
        cum, lv = j
        if not cum:
            return f
        return lv[min(len(lv) - 1, bisect.bisect_right(cum, rng.random() * cum[-1]))]

    def junk_groups(self, s, f):
        k = ("jg", s, f)
        v = self._T.get(k)
        if v is None:
            v = self._T[k] = [(g, self.W(g, s, f)) for g in self.g[s] if g not in self.tgroups[s] and self.W(g, s, f) > 0]
        return v

    def T(self, s, f):
        k = (s, f)
        if k not in self._T:
            self._T[k] = sum(w for tiers in self.g[s].values() for lv, w in tiers if lv >= f)
        return self._T[k]

    def W(self, g, s, f, lo=0):
        k = (g, s, f, lo)
        v = self._W.get(k)
        if v is None:
            v = self._W[k] = sum(w for lv, w in self.g[s].get(g, ()) if lv >= f and lv >= lo)
        return v

    def level_at(self, g, s, f, r):
        for lv, w in self.g[s].get(g, ()):
            if lv < f:
                continue
            if r < w:
                return lv
            r -= w
        return 0


# ---------------------------------------------------------------- simulation
class M:
    __slots__ = ("s", "g", "t", "fr", "cr", "de", "un", "lv")

    def __init__(self, s, g=None, t=-1, fr=0, cr=0, de=0, un=0, lv=0):
        self.s, self.g, self.t, self.fr, self.cr, self.de, self.un, self.lv = s, g, t, fr, cr, de, un, lv

    def copy(self):
        return M(self.s, self.g, self.t, self.fr, self.cr, self.de, self.un, self.lv)


class It:
    __slots__ = ("mods", "rar")

    def __init__(self, mods=None, rar=0):
        self.mods, self.rar = mods or [], rar

    def copy(self):
        return It([m.copy() for m in self.mods], self.rar)


_CTX_SEQ = itertools.count(1)
# per worker process: what is measured once per context (the kept-magic-item lottery of magic_keep2). A context comes to a
# worker as a new pickled copy with every task, so a cache inside it was rebuilt per task (seconds each); its sig is stable
_KEEP2 = {}


class Ctx:
    """everything a strategy run needs: pool, targets, prices, limits"""

    def __init__(self, pool, targets, prices, ilvl, cls, max_s, max_c, base_price, start, runes_once, runes_each,
                 dpool=None, lords=None):
        self.pool, self.targets, self.prices = pool, targets, prices
        self.sig = (os.getpid(), next(_CTX_SEQ))  # identity that survives pickling to the worker processes
        self.dpool, self.lords = dpool or pool, lords or {}
        self.lord_omen = cls not in ARMOUR  # lord omens work on weapon / jewellery desecration only
        self.ilvl, self.cls, self.max = ilvl, cls, {"P": AFFIX_MAX["P"], "S": max_s}
        self.max_c, self.base_price, self.start = max_c, base_price, start
        self.runes_once, self.runes_each = runes_once, runes_each
        self.frac = next((t for t in targets if t["frac"]), None)
        self.tg = {"P": [], "S": []}  # natural groups that can satisfy a target
        for s in "PS":
            seen = {}
            for t in targets:
                if t["side"] == s and t["src"] in ("natural", "desecrated", "essence") and pool.g[s].get(t["group"]):
                    seen.setdefault(t["group"], []).append(t)
            self.tg[s] = list(seen.items())
        self.dtg = {}
        for s in "PS":
            seen = {}
            for t in targets:
                if t["side"] == s and t["src"] in ("natural", "desecrated") and self.dpool.g[s].get(t["group"]):
                    seen.setdefault(t["group"], []).append(t)
            self.dtg[s] = list(seen.items())
        self.ltg = {l: {s: [(g, ts) for g, ts in self.dtg[s] if p.g[s].get(g)] for s in "PS"} for l, p in self.lords.items()}
        for p in [self.pool, self.dpool] + list(self.lords.values()):
            for t in targets:
                p.tgroups[t["side"]].add(t["group"])
        self.alias_of, self.alias_text, self.pool_slots = {}, {}, set()
        self.kolr = None
        self.missing_prices = set()
        self.order = []
        self.cost_cap = math.inf
        self.assign_desc = []
        # optional pool: any need_opt of the 'opt' targets will do
        self.req = [t for t in targets if not t.get("opt")]
        self.opt = [t for t in targets if t.get("opt")]
        self.need_opt = 0
        self.req_side = {s: sum(1 for t in self.req if t["side"] == s) for s in "PS"}
        # "any" first phase: every wanted mod that rolls naturally (required or from the pool)
        self.anyset = [t for t in targets if t["src"] == "natural" and not t["frac"] and pool.g[t["side"]].get(t["group"])]


    def pool_alias(self, eq, picked):
        """a pool goal broken into a concrete pick: any other mod of the pool on the same side fills a picked slot
        just as well (Cold instead of Fire resistance), so those groups count as the picked targets, not as junk.
        eq: every pool mod {side, group, minLevel, text}; picked: indices of the targets that came from the pool"""
        self.pool_slots = set(self.pool_slots) | set(picked)
        for s in "PS":
            # a slot planned for an essence or a desecration is filled just as well by another mod of its group that rolls
            slots = [t for t in self.targets if t["i"] in picked and t["side"] == s and t["src"] in ("natural", "desecrated", "essence")]
            if not slots:
                continue
            for e in eq:
                if e["side"] != s:
                    continue
                al = [dict(t, group=e["group"], minLevel=e["minLevel"], text=e["text"]) for t in slots if t["group"] != e["group"]]
                if not al:
                    continue
                self.alias_text[(s, e["group"])] = e["text"]
                for t in slots:
                    if t["group"] != e["group"]:
                        self.alias_of.setdefault(t["i"], []).append(next(a for a in al if a["i"] == t["i"]))
                for tab, pool in ((self.tg, self.pool), (self.dtg, self.dpool)):
                    if not pool.g[s].get(e["group"]):
                        continue
                    row = next((r for r in tab[s] if r[0] == e["group"]), None)
                    if row is None:
                        tab[s].append((e["group"], list(al)))
                    else:
                        row[1].extend(al)
                for p in [self.pool, self.dpool] + list(self.lords.values()):
                    p.tgroups[s].add(e["group"])
                    p._T.clear() if hasattr(p, "_T") else None
        self.ltg = {l: {s: [(g, ts) for g, ts in self.dtg[s] if p.g[s].get(g)] for s in "PS"} for l, p in self.lords.items()}

    def mtext(self, m):
        """text of a wanted mod on the item (a pool alias shows its own text)"""
        t = self.targets[m.t]
        return t["text"] if m.g is None or m.g == t["group"] else self.alias_text.get((m.s, m.g), t["text"])

    def tval(self, t):
        """rough cost to get target t back if a random removal takes it"""
        cache = self.__dict__.setdefault("_tval", {})
        if t["i"] in cache:
            return cache[t["i"]]
        if t["src"] in ("essence", "alloy"):
            v = self.price(t["name"]) + self.price(OMEN["cryst"][t["side"]])
        elif t["src"] == "desecrated" and not self.pool.g[t["side"]].get(t["group"]):
            w = self.dpool.W(t["group"], t["side"], 0, t["minLevel"]) / max(self.dpool.T(t["side"], 0), 1e-9)
            p = 1 - (1 - w) ** 6
            v = (self.price(f"Preserved {bone_kind(self.cls)}") + self.price(ECHOES) + self.price(ANNUL) + self.price(LIGHT)) / max(p, 1e-4)
        else:  # natural, or a desecration target that also rolls by Exalted (then the cheaper of the two)
            # the cheapest Exalted grade to slam it back, each miss costing an Annul too (a rough estimate: it only
            # weighs risks; Perfect alone overrated a crit chance at ~140 div and made 13-23 div side omens look cheap)
            # a pool mod is replaced by any pool mod of that side, not only by itself
            same = [o for o in self.opt if o["side"] == t["side"]] if t.get("opt") else [t]
            v = math.inf
            for q in ("", "Greater", "Perfect"):
                f = FLOOR["exalt"][q]
                p = sum(self.pool.W(o["group"], o["side"], f, o["minLevel"]) for o in same) / max(self.pool.T(t["side"], f), 1e-9)
                if p > 0:
                    v = min(v, (self.price(CUR["exalt"][q]) + self.price(ANNUL)) / p)
            if t["src"] == "desecrated":
                w = self.dpool.W(t["group"], t["side"], 0, t["minLevel"]) / max(self.dpool.T(t["side"], 0), 1e-9)
                p = 1 - (1 - w) ** 6
                v = min(v, (self.price(f"Preserved {bone_kind(self.cls)}") + self.price(ECHOES) + self.price(ANNUL) + self.price(LIGHT)) / max(p, 1e-4))
            if not math.isfinite(v):
                v = 1e4
        cache[t["i"]] = v
        return v

    def price(self, name):
        p = self.prices.get(name)
        if p is None:
            self.missing_prices.add(name)
            return 0.01
        return p


class Ask(Exception):
    """step-by-step mode: the next random outcome is the player's answer"""

    def __init__(self, q):
        super().__init__(q["text"])
        self.q = q


class Sim:
    def __init__(self, ctx, v, rng, answers=None):
        self.c, self.v, self.r = ctx, v, rng
        self.c0 = ctx  # self.c may become a per-craft copy with swapped roles (_swap); a new base starts from ctx again
        self.led = {}
        self.ops = 0
        self.cost = 0.0
        self.relaxed = False  # another wanted mod got fractured: the planned one may be an ordinary mod
        self.restarts = 0
        self.fp = 0.0  # sum of the exact success chances of the fracture attempts so far
        self.cap = getattr(ctx, "cap", CAP)  # action limit: screening runs use CAP, the exact run a higher one
        self.ia = answers is not None   # step-by-step: outcomes come from the player
        self._loop = None  # step mode: a «repeat until» waiting to be shown together with the next question
        self._att = []  # steps of the current attempt (since the last new base), in order: the guide's order
        self.astrid = False  # Astrid's Creativity bought during the craft (for a Mark of the Abyssal Lord next to another crafted mod)
        # Kolr's Hunt goes into every base of the first phase when the mod caught there needs it (each attempt or
        # missed fracture = a new base); otherwise once, on the finished base
        f = v.get("first")
        fs = ctx.anyset if f == "any" else [ctx.targets[f]] if f is not None else []
        each = bool(ctx.kolr) and any(t.get("mk") for t in fs)
        self.runes_each = [ctx.kolr] if each else []
        self.runes_once = ([ctx.kolr] if ctx.kolr and not each else []) + list(ctx.runes_once)
        self.answers, self.qi, self.trace = answers or [], 0, []

    # -- step-by-step mode
    def mlabel(self, m):
        if m.un:
            return f"нераскрытая кость ({SIDE_RU[m.s]})"
        txt = f"«{self.c.mtext(m)}»" if m.t >= 0 else f"Ненужный {SIDE_RU[m.s]}"
        tags = [x for x, on in (("фрактур", m.fr), ("крафт", m.cr), ("десекрейт", m.de)) if on]
        return txt + (f" [{', '.join(tags)}]" if tags else "")

    def view(self, it):
        if it is None:
            return None
        order = sorted(it.mods, key=lambda m: (m.s, m.t < 0))
        return {"rarity": it.rar,
                "mods": [{"side": m.s, "text": self.c.mtext(m) if m.t >= 0 else None, "want": m.t >= 0,
                          "fr": bool(m.fr), "cr": bool(m.cr), "de": bool(m.de), "un": bool(m.un)} for m in order],
                "missing": [t["text"] + (" — есть, но ещё не зафрактурен" if t["frac"] and not self.relaxed and any(m.t == t["i"] for m in it.mods) else "")
                            for t in self.missing(it)]}

    def info(self, text):
        if self.ia:
            self.trace.append({"info": text})

    def ask(self, text, opts, it, loop=None, how=None):
        """opts: [(label, value)]; replays the player's earlier answers, then stops at the first new question.
        A pending «repeat until» (ask_until) becomes part of this question: the player repeats the loop and answers
        what came in one step"""
        lp, self._loop = self._loop, None
        if lp is not None:
            if len(opts) == 1:  # nothing to choose: the loop keeps its own «Получилось», then this question
                self._loop = lp
                self._flush_loop()
            else:
                loop, how, it = lp["text"], lp["how"], lp["item"]
        if self.qi < len(self.answers):
            i = self.answers[self.qi]
            self.qi += 1
            if not 0 <= i < len(opts):
                raise ValueError("ответ не подходит к вопросу")
            self.trace.append(dict({"q": text, "a": opts[i][0]}, **({"loop": loop} if loop else {})))
            return opts[i][1]
        raise Ask({"text": text, "options": [o[0] for o in opts], "item": self.view(it), "loop": loop or False, "how": how})

    def _flush_loop(self):
        """a loop with no question after it: asked on its own, «Получилось»"""
        lp, self._loop = self._loop, None
        if lp is not None:
            o = [("Получилось", True)]
            self.ask(lp["text"], o, lp["item"], loop=lp["text"], how=lp["how"])

    def pick_mod(self, text, it, cands):
        """which of these mods a random removal / fracture took"""
        if not self.ia:
            return self.r.choice(cands)
        kinds = {}
        for m in cands:
            kinds.setdefault((m.s, m.t, m.fr, m.cr, m.de, m.un), m)
        if len(kinds) == 1:
            m = cands[0]
            if not text.startswith("Какой мод зафрактур"):  # the result is known: still its own step, so the item window keeps up
                self.ask(f"Уйдёт {self.mlabel(m)[0].lower() + self.mlabel(m)[1:]} — других вариантов нет", [("Сделал", True)], it)
            return m
        m = self.ask(text, [(self.mlabel(m), m) for m in kinds.values()], it)
        if m.t >= 0 and not m.fr and "фрактур" not in text:
            self.info(f"Снялся нужный мод «{self.c.targets[m.t]['text']}». Ничего страшного: следующие шаги вернут его.")
        return m

    def ask_until(self, goal, seq, it):
        """seq: the loop as orb names and short words, e.g. [transmute, "→", augment, "→", annul, "+", augment].
        Shown together with the next question (see ask)"""
        self._flush_loop()
        self._loop = {"text": goal, "how": seq, "item": it}

    # -- bookkeeping
    def pay(self, step, name, n=1, price=None):
        if self.ia:
            self._flush_loop()
            last = self.trace[-1] if self.trace else None
            if last is None or last.get("step") != step:
                last = {"step": step, "uses": {}}
                self.trace.append(last)
            last["uses"][name] = last["uses"].get(name, 0) + n
            return
        if name in BASES:
            self._att = []  # a new base: a new attempt (Sim._pos)
        pos = self._pos(step)
        e = self.led.get(step)
        if e is None:
            e = self.led[step] = [0.0, {}, 0, 0, pos]
            if step not in self.c.order:
                self.c.order.append(step)
        x = (self.c.price(name) if price is None else price) * n
        e[0] += x
        self.cost += x
        e[1][name] = e[1].get(name, 0) + n
        self.ops += 1
        if self.cost > self.c.cost_cap:
            self.ops = self.cap  # hopeless run: stop it

    def _pos(self, step):
        """the step's place within the current attempt: a branch first met on the 2nd base after a missed fracture still
        sorts where it happens (Regal, the 3rd mod, the bone, then the Fracturing Orb), not after the fracture"""
        if step not in self._att:
            self._att.append(step)
        return self._att.index(step)

    def tally(self, step, hit, tries=1):
        pos = self._pos(step)
        e = self.led.get(step)
        if e is None:
            e = self.led[step] = [0.0, {}, 0, 0, pos]
            if step not in self.c.order:
                self.c.order.append(step)
        e[2] += tries
        e[3] += 1 if hit else 0

    def geom(self, p):
        """attempts until the first success"""
        if p <= 0:
            return None
        if p >= 1:
            return 1
        return 1 + int(math.log(1.0 - self.r.random()) / math.log(1.0 - p))

    # -- first phase: one wanted mod ("first") or any of several ("any"); Fs = the acceptable targets
    def _fw(self, Fs, f, it=None):
        """weight of the acceptable targets per side at floor f (groups already on the item cannot roll)"""
        pool = self.c.pool
        present = {(m.s, m.g) for m in (it.mods if it else []) if m.g is not None}
        return {x: sum(pool.W(F["group"], x, f, F["minLevel"]) for F in Fs if F["side"] == x and (x, F["group"]) not in present)
                for x in "PS"}

    def _pick_f(self, Fs, side, f, it=None):
        """which acceptable target came, by weight, on that side"""
        cand = [F for F in Fs if F["side"] == side and not any(m.g == F["group"] and m.s == side for m in (it.mods if it else []))]
        ws = [self.c.pool.W(F["group"], side, f, F["minLevel"]) for F in cand]
        r = self.r.random() * sum(ws)
        for F, w in zip(cand, ws):
            if r < w:
                return F
            r -= w
        return cand[-1]

    def _ask_f(self, Fs, it):
        """step mode: which of the acceptable mods came"""
        if len(Fs) == 1:
            return Fs[0]
        return self.ask("Какой из нужных модов выпал?", [(f"«{F['text']}»", F) for F in Fs], it)

    def _p_slam(self, t):
        """chance to slam t, whatever role it has in the plan (None: it never rolls naturally)"""
        if t["src"] == "alloy" or not self.c.pool.g[t["side"]].get(t["group"]):
            return None
        f = FLOOR["exalt"]["Perfect"]
        return self.c.pool.W(t["group"], t["side"], f, t["minLevel"]) / max(self.c.pool.T(t["side"], f), 1e-9)

    def _as_hard(self, t, Fs):
        """t is no easier to slam than the planned first mod (within 25%). Mods of Fs that fill the same slot (any of
        three T1 flat damages for one pool pick) are one target: their chances add up"""
        by = {}
        for F in Fs:
            by[F["i"]] = by.get(F["i"], 0.0) + (self._p_slam(F) or 1)
        pt, pf = self._p_slam(t), min(by.values())
        return pt is not None and pt <= pf * 1.25

    def first_set(self, i, acc=None):
        """what the first phase accepts for the planned first mod i. "hard": only mods at least as hard to slam as it
        (an easier one locked by the fracture would leave the hard one to plain slams); "pool": also every other slot of
        the pool on either side, whatever its weight. The planner tries both"""
        c, P = self.c, self.c.targets[i]
        acc = acc or self.v.get("facc", "hard")
        out, seen = [P], {(P["side"], P["group"])}

        def put(F):
            if acc == "only" and F["i"] != i:
                return
            if (F["side"], F["group"]) not in seen and c.pool.g[F["side"]].get(F["group"]):
                seen.add((F["side"], F["group"]))
                out.append(F)
        for F in c.alias_of.get(i, []):
            put(F)
        if i in c.pool_slots and acc == "pool":
            for t in c.targets:
                if t["i"] in c.pool_slots and t["i"] != i and t["src"] == "natural":
                    put(t)
                    for F in c.alias_of.get(t["i"], []):
                        put(F)
        # other wanted mods at least as hard to slam: planned for slams ("hard"), or also the one planned for desecration
        # ("swap": the two then swap roles, see _swap). Mods planned for an essence are never taken instead
        roles = ("natural", "desecrated") if acc == "swap" else ("natural",)
        if acc != "only":
            for t in c.targets:
                if t["i"] != i and not t["frac"] and t["src"] in roles and self._as_hard(t, [P]):
                    put(t)
        return out

    def _to_essence(self, t, e):
        """this craft takes target t by essence e (a per-craft copy of the context, like _swap)"""
        c = copy.copy(self.c)
        c.__dict__.pop("_tval", None)
        T = list(c.targets)
        T[t["i"]] = dict(t, src="essence", name=e["name"], magic=False)
        c.targets = T
        c.req = [T[x["i"]] for x in c.req]
        c.opt = [T[x["i"]] for x in c.opt]
        c.anyset = [x for x in T if x["src"] == "natural" and not x["frac"] and c.pool.g[x["side"]].get(x["group"])]
        self.c = c
        self.info(f"«{t['text']}» дешевле поставить эссенцией ({e['name']}), чем ловить Exalted: дальше — так.")
        return True

    def _swap(self, a, b):
        """this craft caught wanted mod b where a was planned: b had the desecration role, so a takes it over (a and b are
        about as hard, and the desecration is otherwise left unused). A per-craft copy of the context"""
        c = self.c
        A, B = dict(c.targets[a]), dict(c.targets[b])
        if B["src"] != "desecrated" or B.get("lord") or A["src"] != "natural" \
                or c.dpool.W(A["group"], A["side"], 0, A["minLevel"]) <= 0:
            return False
        c = copy.copy(c)
        c.__dict__.pop("_tval", None)
        A["src"], B["src"] = "desecrated", "natural"
        A["frac"], B["frac"] = B["frac"], A["frac"]
        T = list(c.targets)
        T[a], T[b] = A, B
        c.targets = T
        c.req = [T[t["i"]] for t in c.req]
        c.opt = [T[t["i"]] for t in c.opt]
        c.frac = next((t for t in T if t["frac"]), None)
        c.anyset = [t for t in T if t["src"] == "natural" and not t["frac"] and c.pool.g[t["side"]].get(t["group"])]
        self.c = c
        return True

    def _aug_hint(self, Fs):
        """when the Augment is worth it, in plain words: it adds the other affix type"""
        sides = {F["side"] for F in Fs}
        if sides == {"P"}:
            return "Augment — только если Transmute дал суффикс: Augment добавит префикс, а нужный мод — префикс"
        if sides == {"S"}:
            return "Augment — только если Transmute дал префикс: Augment добавит суффикс, а нужный мод — суффикс"
        return "Augment — если Transmute дал ненужный мод: Augment добавит аффикс другого типа (к префиксу — суффикс, к суффиксу — префикс)"

    def _magic_got(self, Fs, f):
        """step mode: which wanted mod the magic item got, and what its second affix is (it may be wanted too)"""
        F = self._ask_f(Fs, None)
        it = It([self._m(F)], 1)
        o = {"P": "S", "S": "P"}[F["side"]]
        opts = [("Второго аффикса нет", None)]
        for t in self.missing(it):
            if t["side"] == o and t["src"] in ("natural", "essence", "desecrated") and self.c.pool.W(t["group"], o, f, t["minLevel"]) > 0:
                opts.append((f"«{t['text']}»{orhigher(t['text'])}", M(o, t["group"], t["i"], lv=t["minLevel"])))
        opts.append((f"Ненужный {SIDE_RU[o]}", M(o)))
        m = self.ask(f"А второй аффикс ({SIDE_RU[o]})?", opts, it)
        if m is not None:
            it.mods.insert(0, m)
        return it

    def _goal(self, Fs):
        return Fs[0]["text"] if len(Fs) == 1 else "любой из: " + "; ".join(F["text"] for F in Fs)

    def magic_keep2(self, step, Fs, q):
        """new white base + Transmute + Augment each attempt; the magic item is kept when it has a first-phase mod or
        two wanted mods at once (one prefix + one suffix: a magic item holds one of each)"""
        f, Fid, oth = FLOOR["transmute"][q], {F["i"] for F in Fs}, {"P": "S", "S": "P"}
        goal = self._goal(Fs) if len(Fs) <= 3 else "любой мод первого этапа"
        if self.ia:
            self.pay(step, "Белая база", price=self.c.base_price)
            self.pay(step, CUR["transmute"][q]); self.pay(step, CUR["augment"][q])
            both = self.v.get("keep") == 3
            fsides = {F["side"] for F in Fs}
            other = [t for t in self.missing(It([], 1)) if t["side"] not in fsides and t["src"] in ("natural", "essence", "desecrated")
                     and self.c.pool.W(t["group"], t["side"], f, t["minLevel"]) > 0]
            seconds = "; ".join(f"«{t['text']}»" for t in other) or "любой нужный мод"
            first_s = "; ".join(f"«{F['text']}»" for F in Fs[:4]) + ("…" if len(Fs) > 4 else "")
            self.ask_until(f"синий, на котором есть один из: {first_s} — И ещё один из: {seconds}" if both
                           else f"«{goal}» или сразу два нужных мода", ["новая база", "→", CUR["transmute"][q], "→", CUR["augment"][q]],
                           None)
            it = It([], 1)
            if both:
                F = Fs[0] if len(Fs) == 1 else self.ask(
                    f"Какой {SIDE_RU[Fs[0]['side']] if len(fsides) == 1 else 'мод'} из нужных на синем?", [(f"«{F['text']}»", F) for F in Fs], it)
            else:
                F = self.ask("Что получилось?", [(f"«{F['text']}»", F) for F in Fs] + [("Два других нужных мода", None)], it)
            if F is not None:
                it.mods.append(self._m(F))
                o2 = [t for t in other if t["side"] == oth[F["side"]]] if both else []
                if len(o2) == 1:  # the loop already said which one it must be
                    it.mods.append(self._m(o2[0]))
                elif o2:
                    it.mods.append(self._m(self.ask(f"А какой {SIDE_RU[oth[F['side']]]}?", [(f"«{t['text']}»", t) for t in o2], it)))
                else:
                    self.add(it, f, oth[F["side"]], f"А {SIDE_RU[oth[F['side']]]}?")
            else:
                self.add(it, f, "P", "Какой нужный префикс?")
                self.add(it, f, "S", "Какой нужный суффикс?")
            return it
        # every attempt starts from the same white base, so the chance of a kept magic item and what it looks like are
        # the same each time: measured once per context (and kept in it), then the count is drawn at once
        both = self.v.get("keep") == 3
        sig = getattr(self.c, "sig", None)
        if sig is None:
            cache = self.c.__dict__.setdefault("_keep2", {})
        else:
            if len(_KEEP2) > 300:
                _KEEP2.clear()  # old contexts (finished jobs): keep the worker's memory flat
            cache = _KEEP2.setdefault(sig, {})
        key = (tuple(sorted(Fid)), q, both)
        if key not in cache:
            cache[key] = self._keep2_table(Fid, f, both)
        p, cum, combos = cache[key]
        k = self.geom(p)
        if k is None or not combos or self.ops + 1 >= self.cap:
            return None
        _, x, m1, m2 = combos[min(len(combos) - 1, bisect.bisect_right(cum, self.r.random() * cum[-1]))]
        it = It([], 1)
        for side, m in ((x, m1), (oth[x], m2)):
            if m is not None:
                it.mods.append(m.copy() if m.g != "__junk__" else self._junk(side, f))
        if self.runes_each:
            step += " + " + " + ".join(self.runes_each) + " в сокет"
        self.pay(step, "Белая база", k, price=self.c.base_price)
        for name in self.runes_each:
            self.pay(step, ARTIFICER, k); self.pay(step, name, k)
        self.pay(step, CUR["transmute"][q], k)
        self.pay(step, CUR["augment"][q], k)
        self.tally(step, True, k)
        return it

    def _keep2_table(self, Fid, f, both):
        """the kept-magic-item lottery of magic_keep2, exact: a magic item is one prefix + one suffix, the Transmute
        side drawn by total weight, the Augment on the other; each side's outcome is a wanted group at a tier, or junk
        (lumped). Every pair in both orders, with what the first leaves missing deciding whether the second is wanted.
        -> (chance per base, cumulative weights, [(p, first side, first mod, second mod)]) for the kept ones"""
        pool, oth = self.c.pool, {"P": "S", "S": "P"}
        outs = {}
        for s_ in "PS":
            T = pool.T(s_, f)
            if T <= 0:
                outs[s_] = [(1.0, None)]
                continue
            lst, tw = [], 0.0
            for g, ts in self.c.tg[s_]:
                for lv, w in pool.g[s_].get(g, ()):
                    if lv >= f and w > 0:
                        lst.append((w / T, (g, lv)))
                        tw += w
            if T - tw > 1e-9 * T:
                lst.append(((T - tw) / T, "junk"))
            outs[s_] = lst

        def mk(it, s_, o):
            if o is None:
                return None
            if o == "junk":
                return M(s_, "__junk__", -1)
            g, lv = o
            miss = {t["i"] for t in self.missing(it)}
            ts = next(ts for g2, ts in self.c.tg[s_] if g2 == g)
            t = next((t for t in ts if t["i"] in miss and lv >= t["minLevel"]), None)
            return M(s_, g, t["i"] if t else -1, lv=lv)
        TP, TS = pool.T("P", f), pool.T("S", f)
        combos = []
        for x, px in (("P", TP / max(TP + TS, 1e-12)), ("S", TS / max(TP + TS, 1e-12))):
            if px <= 0:
                continue
            for pa, a in outs[x]:
                it1 = It([], 1)
                m1 = mk(it1, x, a)
                if m1 is not None:
                    it1.mods.append(m1)
                for pb, b in outs[oth[x]]:
                    m2 = mk(it1, oth[x], b)
                    ms = [m for m in (m1, m2) if m is not None]
                    got_f, got_n = any(m.t in Fid for m in ms), sum(1 for m in ms if m.t >= 0)
                    if (got_f and got_n >= 2) if both else (got_f or got_n >= 2):
                        combos.append((px * pa * pb, x, m1, m2))
        cum, acc = [], 0.0
        for c_ in combos:
            acc += c_[0]
            cum.append(acc)
        return acc, cum, combos

    def _junk(self, s, f):
        """a concrete junk mod for side s (not a group any target can use), drawn by weight"""
        tgs = {g for g, _ in self.c.tg[s]}
        for _ in range(1000):
            m = self.draw(It([], 1), s, f)
            if m is not None and m.g not in tgs:
                m.t = -1
                return m
        return M(s, lv=self.c.pool.junk_level(s, f, self.r))

    def magic_spam(self, step, Fs, q):
        """new white base + Transmute (+ Augment when the other side can still give a wanted mod) until a wanted mod
        is on the magic item; sampled in one go"""
        if self.v.get("keep") in (2, 3):
            return self.magic_keep2(step, Fs, q)
        pool, f = self.c.pool, FLOOR["transmute"][q]
        Tt = {x: pool.T(x, f) for x in "PS"}
        tot = Tt["P"] + Tt["S"]
        w = self._fw(Fs, f)
        if tot <= 0 or w["P"] + w["S"] <= 0:
            return None
        miss = {x: 1 - w[x] / Tt[x] if Tt[x] > 0 else 1.0 for x in "PS"}
        p_fail = miss["P"] * miss["S"]  # the Transmute lands on one side, the Augment (if any) on the other
        p, pa = 1 - p_fail, (w["P"] + w["S"]) / tot
        oth = {"P": "S", "S": "P"}
        aug_fail = sum(Tt[x] / tot * miss[x] * miss[oth[x]] for x in "PS" if w[oth[x]] > 0)
        if self.ia:
            self.pay(step, "Белая база", price=self.c.base_price)
            self.pay(step, CUR["transmute"][q]); self.pay(step, CUR["augment"][q])
            self.ask_until(self._goal(Fs), ["новая база", "→", CUR["transmute"][q], "→", CUR["augment"][q],
                                            f"({self._aug_hint(Fs)})"], None)
            return self._magic_got(Fs, f)
        k = self.geom(p)
        if k is None:
            return None
        via_aug = self.r.random() * p >= pa
        if self.runes_each:
            step += " + " + " + ".join(self.runes_each) + " в сокет"
        self.pay(step, "Белая база", k, price=self.c.base_price)
        for name in self.runes_each:
            self.pay(step, ARTIFICER, k); self.pay(step, name, k)
        self.pay(step, CUR["transmute"][q], k)
        aug = (k - 1) * aug_fail / max(p_fail, 1e-12) + (1 if via_aug else 0)
        if aug:
            self.pay(step, CUR["augment"][q], aug)
        self.tally(step, True, k)
        if not via_aug:
            side = "P" if self.r.random() * (w["P"] + w["S"]) < w["P"] else "S"
            it = It([self._m(self._pick_f(Fs, side, f))], 1)
            if self.v.get("fmode") in ("bone", "four"):
                # Augment anyway before a fracture: the Regal then gives the 3rd mod (no «one more Exalted» branch)
                self.pay(step, CUR["augment"][q])
                m = self.draw(it, oth[side], f)
                if m:
                    it.mods.insert(0, m)
            return it
        # the Transmute missed on side x, the Augment hit on the other side
        wy = {y: Tt[oth[y]] / tot * miss[oth[y]] * (w[y] / Tt[y] if Tt[y] > 0 else 0) for y in "PS"}
        y = "P" if self.r.random() * (wy["P"] + wy["S"]) < wy["P"] else "S"
        it = It([self._m(self._pick_f(Fs, y, f))], 1)
        m = self.draw(it, oth[y], f)
        if m:
            it.mods.insert(0, m)
        return it

    def _m(self, F):
        return M(F["side"], F["group"], F["i"], lv=F["minLevel"])

    def magic_annul(self, step, Fs, q):
        """one white base: Transmute (+ Augment); on a miss Annul (one of the two mods) + Augment into the freed side"""
        pool, f = self.c.pool, FLOOR["transmute"][q]
        Tt = {x: pool.T(x, f) for x in "PS"}
        tot = Tt["P"] + Tt["S"]
        w = self._fw(Fs, f)
        if tot <= 0 or w["P"] + w["S"] <= 0:
            return None
        oth = {"P": "S", "S": "P"}
        self.white("Новая база")  # its own guide line: once per base, not part of every try of the catch
        self.pay(step, CUR["transmute"][q])
        if self.ia:
            self.pay(step, CUR["augment"][q])
            self.pay(step, ANNUL); self.pay(step, CUR["augment"][q])
            self.ask_until(self._goal(Fs), [CUR["transmute"][q], "→", CUR["augment"][q], "→", ANNUL, "+", CUR["augment"][q]], None)
            return self._magic_got(Fs, f)
        miss = {x: 1 - w[x] / Tt[x] if Tt[x] > 0 else 1.0 for x in "PS"}
        pa = (w["P"] + w["S"]) / tot
        x = self.r.random()
        if x < pa:  # the Transmute gave it
            side = "P" if self.r.random() * (w["P"] + w["S"]) < w["P"] else "S"
            self.tally(step, True, 1)
            it = It([self._m(self._pick_f(Fs, side, f))], 1)
            if self.v.get("fmode") in ("bone", "four"):
                # Augment anyway: the Regal then adds the 3rd mod the Fracturing Orb needs — one plain route in the
                # guide instead of a «one more Exalted» branch (readability over ~0.04 div)
                self.pay(step, CUR["augment"][q])
                m = self.draw(it, oth[side], f)
                if m:
                    it.mods.insert(0, m)
            return it
        self.pay(step, CUR["augment"][q])
        if x < 1 - miss["P"] * miss["S"]:  # the Augment gave it
            hit = "P" if self.r.random() * (w["P"] / max(Tt["P"], 1e-9) + w["S"] / max(Tt["S"], 1e-9)) < w["P"] / max(Tt["P"], 1e-9) else "S"
            k = 0
        else:
            # Annul takes one of the two mods at random, the Augment rolls that side again
            pp = {y: 0.5 * w[y] / Tt[y] if Tt[y] > 0 else 0 for y in "PS"}
            k = self.geom(pp["P"] + pp["S"])
            if k is None:
                return None
            self.pay(step, ANNUL, k); self.pay(step, CUR["augment"][q], k)
            hit = "P" if self.r.random() * (pp["P"] + pp["S"]) < pp["P"] else "S"
        self.tally(step, True, 1 + k)
        it = It([self._m(self._pick_f(Fs, hit, f))], 1)
        m = self.draw(it, oth[hit], f)
        if m:
            it.mods.insert(0, m)
        return it

    def chaos_until(self, step, it, Fs, q="", lose_cr=False):
        """Chaos Orb until a wanted mod appears; the chance per orb barely changes, so the count is sampled at once.
        lose_cr: crafted wanted mods on the item are not protected — each orb takes one of the n mods, so a crafted mod
        lives through k orbs with ((n-1)/n)^(k-1) (the last orb's removal is drawn below); a lost one is junk now and
        gets crafted again later"""
        pool, f = self.c.pool, FLOOR["chaos"][q]
        rem = [m for m in it.mods if not m.fr]
        if not rem:
            return False
        w = self._fw(Fs, f, it)
        p, by_side = 0.0, {"P": 0.0, "S": 0.0}
        for m in rem:
            rooms = [x for x in "PS" if self.room(it, x) + (1 if m.s == x else 0) > 0]
            tot = sum(pool.T(x, f) for x in rooms)
            for x in rooms:
                if tot > 0:
                    by_side[x] += w[x] / tot / len(rem)
        p = by_side["P"] + by_side["S"]
        k = self.geom(p) if not self.ia else (1 if p > 0 else None)
        if k is None:
            return False
        self.pay(step, CUR["chaos"][q], k)
        self.tally(step, True, k)
        if self.ia:
            self.ask_until(self._goal(Fs), [CUR["chaos"][q]], it)
            F = self._ask_f([F for F in Fs if w[F["side"]] > 0], it)
        else:
            s = "P" if self.r.random() * p < by_side["P"] else "S"
            F = self._pick_f(Fs, s, f, it)
        if lose_cr:
            for m in [m for m in rem if m.t >= 0 and not self.precious(m)]:
                gone = self.ask(f"«{self.c.mtext(m)}» ещё на предмете?", [("Да", False), ("Нет, Chaos его снял", True)], it) \
                    if self.ia else self.r.random() >= ((len(rem) - 1) / len(rem)) ** max(k - 1, 0)
                if gone:
                    i = it.mods.index(m)
                    it.mods[i] = rem[rem.index(m)] = M(m.s)
        s = F["side"]
        cand = [m for m in rem if m.s == s] if self.room(it, s) <= 0 else rem
        if self.ia:
            it.mods.remove(self.pick_mod(f"Какой мод ушёл, когда выпал «{F['text']}»?", it, cand))
        else:
            it.mods.remove(self.r.choice(cand))
        it.mods.append(self._m(F))
        return True

    # -- item queries
    def room(self, it, s):
        n = 0
        for m in it.mods:
            if m.s == s:
                n += 1
        return (1 if it.rar == 1 else self.c.max[s]) - n

    def sat(self, it):
        out = set()
        for m in it.mods:
            if m.t >= 0 and not m.un:
                t = self.c.targets[m.t]
                if not t["frac"] or m.fr or self.relaxed:
                    out.add(m.t)
        return out

    def missing(self, it):
        s = self.sat(it)
        out = [t for t in self.c.req if t["i"] not in s]
        k = self.c.need_opt
        if k:
            have = [t for t in self.c.opt if t["i"] in s]
            if len(have) < k:
                for side in "PS":  # pool mods only where a slot is left after the required ones
                    if self.c.max[side] - self.c.req_side[side] - sum(1 for t in have if t["side"] == side) > 0:
                        out += [t for t in self.c.opt if t["side"] == side and t["i"] not in s]
        return out

    def crafted(self, it):
        return sum(1 for m in it.mods if m.cr)

    # -- random mod
    def draw(self, it, s, f, exclude=(), miss=None, pool=None, tg=None):
        pool = pool or self.c.pool
        tg = tg or self.c.tg
        present = {m.g for m in it.mods if m.g is not None and m.s == s}
        present.update(exclude)
        total = pool.T(s, f) - sum(pool.W(g, s, f) for g in present)
        if total <= 0:
            return None
        r = self.r.random() * total
        for g, ts in tg[s]:
            if g in present:
                continue
            wg = pool.W(g, s, f)
            if r < wg:
                lv = pool.level_at(g, s, f, r)
                if miss is None:  # only when a wanted group came up (most draws are junk)
                    miss = {t["i"] for t in self.missing(it)}
                t = next((t for t in ts if t["i"] in miss and lv >= t["minLevel"]), None)
                return M(s, g, t["i"] if t else -1, lv=lv)
            r -= wg
        for g, wg in pool.junk_groups(s, f):  # not a target group: still a real group that blocks itself
            if g in present:
                continue
            if r < wg:
                return M(s, g, -1, lv=pool.level_at(g, s, f, r))
            r -= wg
        return M(s, lv=pool.junk_level(s, f, self.r))

    def side_for(self, it, f, sides):
        if len(sides) == 1:
            return sides[0]
        pool = self.c.pool
        a, b = (max(pool.T(s, f) - sum(pool.W(m.g, s, f) for m in it.mods if m.s == s and m.g is not None), 1e-9)
                for s in sides)
        return sides[0] if self.r.random() * (a + b) < a else sides[1]

    def add(self, it, f, side=None, q="Что добавилось?"):
        sides = [side] if side else [s for s in "PS" if self.room(it, s) > 0]
        sides = [s for s in sides if self.room(it, s) > 0]
        if not sides:
            return None
        if self.ia:
            miss, opts = self.missing(it), []
            for s in sides:
                present = {m.g for m in it.mods if m.s == s and m.g is not None}
                for t in miss:
                    if t["side"] == s and t["src"] in ("natural", "essence", "desecrated") and t["group"] not in present \
                            and self.c.pool.W(t["group"], s, f, t["minLevel"]) > 0:
                        opts.append((f"«{t['text']}»{orhigher(t['text'])}" + (" — из пула" if t.get("opt") or t["i"] in self.c.pool_slots else ""),
                                     M(s, t["group"], t["i"], lv=t["minLevel"])))
                seen = {o[1].g for o in opts if o[1].s == s}
                for t in miss:
                    for a in self.c.alias_of.get(t["i"], []):
                        if a["side"] == s and a["group"] not in present and a["group"] not in seen \
                                and self.c.pool.W(a["group"], s, f, a["minLevel"]) > 0:
                            seen.add(a["group"])
                            opts.append((f"«{a['text']}»{orhigher(a['text'])} — из пула", M(s, a["group"], t["i"], lv=a["minLevel"])))
                opts.append((f"Ненужный {SIDE_RU[s]}", M(s)))
            m = self.ask(q, opts, it)
            it.mods.append(m)
            return m
        m = self.draw(it, self.side_for(it, f, sides), f)
        if m:
            it.mods.append(m)
        return m

    def remove(self, it, side=None, only_de=False):
        rem = [m for m in it.mods if not m.fr and (side is None or m.s == side) and (not only_de or m.de)]
        if rem:
            it.mods.remove(self.pick_mod("Какой мод снялся?", it, rem))

    # -- currency
    def again_item(self):
        """from the player's item, starting over means another item like it (a magic +4 wand), bought at the price in the
        field, not a white base"""
        self.pay("Ваш предмет", "Ваш предмет", price=self.c.base_price)
        return self.c.start.copy()

    def white(self, step):
        self._att = []  # a new base: a new attempt
        if self.runes_each:
            step += " + " + " + ".join(self.runes_each) + " в сокет"
        self.pay(step, "Белая база", price=self.c.base_price)
        for name in self.runes_each:
            self.pay(step, ARTIFICER)
            self.pay(step, name)
        return It([], 0)

    def magic_hit(self, step, it, q, goal):
        self.pay(step, CUR["transmute"][q])
        it.rar = 1
        self.add(it, FLOOR["transmute"][q])
        if goal["i"] not in self.sat(it):
            self.pay(step, CUR["augment"][q])
            self.add(it, FLOOR["augment"][q])
        hit = goal["i"] in self.sat(it)
        self.tally(step, hit)
        return hit

    def to_rare(self, it, essence=True):
        miss = self.missing(it)
        ess = None if not essence else next((t for t in miss if t["src"] == "essence" and t["magic"] and
                    sum(1 for m in it.mods if m.s == t["side"]) < self.c.max[t["side"]] and
                    not any(m.g == t["group"] for m in it.mods)), None) if self.crafted(it) < self.c.max_c else None
        if ess:
            step = f"Сделать редким: {ess['name']} → «{ess['text']}» (крафт-мод)"
            self.pay(step, ess["name"])
            it.rar = 2
            it.mods.append(M(ess["side"], ess["group"], ess["i"], cr=1, lv=ess["minLevel"]))
            if self.ia:  # its own step: the player applies it before anything else
                self.ask(f"Примените {ess['name']} к синему: он станет редким, и на нём наверняка появится «{ess['text']}».",
                         [("Сделал", True)], it)
            return
        # the planned essence is not needed any more (another mod of its group came): any cheap essence of a mod still
        # missing gives that mod for sure, where a Regal Orb gives a random one
        best = None
        # the essence's mod is a crafted mod: only when the planned crafted mods (alloys, essences) still leave a place
        pending = sum(1 for t in miss if t["src"] in ("alloy", "essence"))
        if essence and self.crafted(it) + pending < self.c.max_c:
            for t in miss:
                if t["src"] not in ("natural", "desecrated") or sum(1 for m in it.mods if m.s == t["side"]) >= self.c.max[t["side"]] \
                        or any(m.g == t["group"] for m in it.mods):
                    continue
                for e in t.get("ess_opts") or []:
                    pe = self.c.price(e["name"])
                    if e["level"] >= t["minLevel"] and pe < EXPENSIVE and (best is None or pe < best[0]):
                        best = (pe, t, e)
        if best:
            _, t, e = best
            step = f"Сделать редким: {e['name']} → «{t['text']}» (вместо Regal: нужный мод наверняка)"
            self.pay(step, e["name"])
            it.rar = 2
            it.mods.append(M(t["side"], t["group"], t["i"], cr=1, lv=e["level"]))
            if self.ia:
                self.ask(f"Примените {e['name']} к синему (вместо Regal Orb): он станет редким, и на нём наверняка появится «{t['text']}».",
                         [("Сделал", True)], it)
        else:
            # Regal, Greater or Perfect: the price against the chance it brings a wanted mod (worth what slamming it costs)
            natm = [t for t in miss if t["src"] == "natural"]
            rooms = [x for x in "PS" if self.room(it, x) > 0]
            best = None
            for q in ("", "Greater", "Perfect"):
                f = FLOOR["regal"][q]
                gain = 0.0
                for x in rooms:
                    tot = self.c.pool.T(x, f)
                    if tot > 0:
                        gain += sum(self.c.pool.W(t["group"], x, f, t["minLevel"]) / tot * self.c.tval(t) for t in natm
                                    if t["side"] == x and not any(m.g == t["group"] for m in it.mods)) / len(rooms)
                score_q = self.c.price(CUR["regal"][q]) - gain
                if q == "":
                    score_plain = score_q
                if best is None or score_q < best[0]:
                    best = (score_q, q)
            # a Greater / Perfect Regal only when it clearly pays (0.5 div+): otherwise one plain line in the guide
            q = best[1] if best and best[0] < score_plain - 0.5 else ""
            step = f"Сделать редким: {CUR['regal'][q]}"
            self.pay(step, CUR["regal"][q])
            it.rar = 2
            self.add(it, FLOOR["regal"][q])

    def exalt(self, step, it, q, side=None, greater=False):
        self.pay(step, CUR["exalt"][q])
        other = side and self.room(it, "S" if side == "P" else "P") > 0
        if side and other:
            self.pay(step, OMEN["exalt"][side])
        else:
            side = side if side and not other else None
        if greater:
            self.pay(step, GREATER)
        for k in range(2 if greater else 1):
            self.add(it, FLOOR["exalt"][q], side,
                     "Что добавилось вторым (Omen of Greater Exaltation даёт два мода)?" if k else
                     "Что добавилось первым?" if greater else "Что добавилось?")

    def chaos(self, step, it, q=""):
        self.pay(step, CUR["chaos"][q])
        self.remove(it)
        self.add(it, FLOOR["chaos"][q], None, "Что добавил этот Chaos Orb взамен?")

    def fix(self, it, s):
        rem = [m for m in it.mods if not m.fr]
        mode = self.v["fix"]
        if mode == "plain" and any(m.t >= 0 for m in rem):
            mode = "value"  # a wanted mod could go: weigh the side omen against that risk (a dear Spirit is worth one)
        if mode == "value":
            # cost per removal that frees side s: (orb + omen + expected loss of a wanted mod) / chance to hit junk on s
            best = None
            for name, R, cost in (("plain", rem, self.c.price(ANNUL)),
                                  ("omen", [m for m in rem if m.s == s], self.c.price(ANNUL) + self.c.price(OMEN["annul"][s]))):
                if not R:
                    continue
                good = sum(1 for m in R if m.s == s and m.t < 0 and not m.un) / len(R)
                if not any(m.s == s and m.t < 0 and not m.un for m in rem):
                    good = sum(1 for m in R if m.s == s) / len(R)  # no junk there at all: any mod of that side frees it
                loss = (sum(self.c.tval(self.c.targets[m.t]) for m in R if m.t >= 0) +
                        sum(self.c.price(f"{self.v['bone']} {bone_kind(self.c.cls)}") for m in R if m.un)) / len(R)
                e = (cost + loss) / good if good else math.inf
                if best is None or e < best[0]:
                    best = (e, name)
            mode = best[1] if best and math.isfinite(best[0]) else "plain"
        if all(m.t < 0 and not m.un for m in rem):
            step = "Убрать мусор: Orb of Annulment (на предмете только мусор и фрактур)"
            self.pay(step, ANNUL)
            self.remove(it)
        elif mode == "whittle":
            step = "Убрать мусор: Chaos Orb + Omen of Whittling (уходит мод с наименьшим уровнем)"
            self.pay(step, CUR["chaos"][""]); self.pay(step, WHITTLING)
            rem = [m for m in it.mods if not m.fr]
            if rem:
                lo = min(m.lv for m in rem)
                it.mods.remove(self.pick_mod("Какой мод снялся?", it, rem) if self.ia else self.r.choice([m for m in rem if m.lv == lo]))
            self.add(it, 0, None, "Что добавил этот Chaos Orb взамен?")
        elif mode == "omen":
            step = f"Убрать мусор: Orb of Annulment + {OMEN['annul'][s]} (снимает только {SIDE_RU[s]})"
            self.pay(step, ANNUL); self.pay(step, OMEN["annul"][s])
            self.remove(it, s)
        else:
            step = "Убрать мусор: Orb of Annulment (случайный мод)"
            self.pay(step, ANNUL)
            self.remove(it)

    def bone(self, step, it, q, side):
        mk = next((m for m in it.mods if m.g == ABYSS_MARK and (not side or m.s == side)), None)
        if mk is not None:
            # a Mark of the Abyssal Lord: the bone turns it into the unrevealed desecrated mod, no free slot or omen needed
            self.pay(step, f"{q} {bone_kind(self.c.cls)}")
            it.mods[it.mods.index(mk)] = M(mk.s, de=1, un=1)
            return True
        if side and self.room(it, side) <= 0:
            side = None
        sides = [side] if side else [s for s in "PS" if self.room(it, s) > 0]
        if not sides:
            return False
        self.pay(step, f"{q} {bone_kind(self.c.cls)}")
        if side and len([s for s in "PS" if self.room(it, s) > 0]) > 1:
            self.pay(step, OMEN["necro"][side])
        s = sides[0] if len(sides) == 1 else \
            self.ask("Куда легла кость?", [(SIDE_RU[x], x) for x in sides], it) if self.ia else self.r.choice(sides)
        it.mods.append(M(s, de=1, un=1))
        return True

    def dedup(self, it):
        """two mods filling the same wanted slot (a fractured Fire flat and a Lightning flat for one «any T1 flat» pick):
        the second is junk for the rest of the craft — protecting it turns cheap Chaos / plain Annul into omen loops.
        The fractured one keeps the slot, then a desecrated, a crafted, any"""
        ts = [m.t for m in it.mods if m.t >= 0]
        if len(ts) == len(set(ts)):
            return
        by = {}
        for m in it.mods:
            if m.t >= 0 and not m.un:
                by.setdefault(m.t, []).append(m)
        for ms in by.values():
            if len(ms) > 1:
                ms.sort(key=lambda m: (not m.fr, not m.de, not m.cr))
                for m in ms[1:]:
                    m.t = -1

    def precious(self, m):
        """a wanted mod worth protecting from Chaos: not fractured, not crafted, and not one the plan makes by an essence
        or Alloy anyway (rolled by itself it is cheap to craft again: Hysteria 1.2 div against Exalted + Annul loops)"""
        return m.t >= 0 and not m.fr and not m.cr and not m.un and \
            self.c.targets[m.t]["src"] not in ("essence", "alloy")

    def has_mark(self, it, side):
        return any(m.g == ABYSS_MARK and m.s == side for m in it.mods)

    def undo_des(self, it, dj):
        """a failed desecrated mod off the item. Strategy light: True = Annul + Omen of Light (only a desecrated mod goes);
        "abyss" = Essence of the Abyss + Crystallisation of its side when it is the only removable mod there: the essence
        replaces exactly it with a Mark of the Abyssal Lord, which the next bone turns into a new desecrated mod (the
        Mark is a crafted mod: next to another one it takes Astrid's Creativity, bought once); False = plain Annul"""
        light = self.v["light"]
        side_rest = [m for m in it.mods if m is not dj and m.s == dj.s and not m.fr]
        # other mods on that side only junk (no Mark yet): the essence takes one of them or the failed mod — nothing
        # wanted goes; if it took junk, the failed mod is still there and goes by Light next time (the Mark stays for the bone)
        # the Mark is a crafted mod: it needs a free crafted place, or Astrid's Creativity not used yet (bought now)
        can_astrid = not self.astrid and RUNE["astrid"] not in self.runes_once and "astrid" not in getattr(self.c, "runes_have", ())
        if light == "abyss" and all(m.t < 0 and not m.cr and not m.de for m in side_rest) and \
                not any(m.g == ABYSS_MARK for m in it.mods) and (self.crafted(it) < self.c.max_c or can_astrid):
            if self.crafted(it) >= self.c.max_c:
                step = f"Перед первым {ABYSS_ESS}: {RUNE['astrid']} + {ARTIFICER} (один раз: Mark — второй крафт-мод)"
                self.pay(step, ARTIFICER)
                self.pay(step, RUNE["astrid"])
                self.astrid = True
            om = any(m.s != dj.s and not m.fr for m in it.mods)
            step = (f"Убрать неудачный десекрейт: {ABYSS_ESS}" + (f" + {OMEN['cryst'][dj.s]}" if om else "") +
                    (f" (заменит его или мусорный {SIDE_RU[dj.s]}; Mark заменит следующая кость)" if side_rest else
                     " (Mark на его месте заменит следующая кость)"))
            self.pay(step, ABYSS_ESS)
            if om:
                self.pay(step, OMEN["cryst"][dj.s])
            gone = dj if not side_rest else \
                self.ask(f"Какой {SIDE_RU[dj.s]} заменила Essence of the Abyss?",
                         [("Неудачный десекрейт", dj)] + [(f"Ненужный {SIDE_RU[dj.s]}", m) for m in side_rest[:1]], it) \
                if self.ia else self.r.choice([dj] + side_rest)
            it.mods[it.mods.index(gone)] = M(dj.s, ABYSS_MARK, cr=1)
            return
        if light:
            step = "Убрать неудачный десекрейт: Orb of Annulment + Omen of Light"
            self.pay(step, ANNUL); self.pay(step, LIGHT)
            self.remove(it, only_de=True)
            return
        self.fix(it, dj.s)

    def reveal(self, step, it, u, floor, echoes=None, take_wanted=True):
        echoes = self.v["echoes"] if echoes is None else echoes
        miss = [t for t in self.missing(it) if t["src"] in ("natural", "desecrated") and t["side"] == u.s and not t["frac"]]
        if not take_wanted:
            miss = []
        missi = {t["i"] for t in miss}
        if not missi:
            echoes = False  # nothing wanted on this side: take any option, no reroll
        if echoes and self.v.get("putref") and self.v.get("echo") == "need":
            # a spare slot left: a junk option here still lets the set come together, so keep the Echoes
            i = it.mods.index(u)
            it.mods[i] = M(u.s)
            if self.putref_ok(it):
                echoes = False
            it.mods[i] = u
        it.mods.remove(u)
        if self.ia:
            if step and not (self.trace and self.trace[-1].get("step") == step):
                self.trace.append({"step": step, "uses": {}})  # revealing an existing bone costs nothing: still its own step
            pick = None
            if not missi:
                # nothing wanted can be among the options here: no question, just do it
                self.ask(f"Раскройте кость в Well of Souls и возьмите любой из 3 вариантов: нужных {SIDE_RU[u.s]}ов среди них "
                         "быть не может.", [("Сделал", None)], it)
                pick = M(u.s, de=1)
                it.mods.append(pick)
                return pick
            for attempt in range(2 if echoes else 1):
                if attempt:
                    self.pay(step, ECHOES)
                opts = [(f"«{self.c.targets[i]['text']}» — выбрать его", M(u.s, self.c.targets[i]["group"], i, lv=self.c.targets[i]["minLevel"]))
                        for i in sorted(missi)]
                here = {m.g for m in it.mods if m.s == u.s and m.g is not None} | {o[1].g for o in opts}
                for i in sorted(missi):  # the rest of the pool fills the same slot
                    for a in self.c.alias_of.get(i, []):
                        if a["side"] == u.s and a["group"] not in here and self.c.dpool.g[u.s].get(a["group"]):
                            here.add(a["group"])
                            opts.append((f"«{a['text']}» — выбрать его (из пула)", M(u.s, a["group"], i, lv=a["minLevel"])))
                what = f"нераскрытый {SIDE_RU[u.s]}" if self.v.get("putref") else "кость"
                pick = self.ask(f"Раскройте {what} (Well of Souls). Есть среди 3 вариантов нужный?" if not attempt else
                                "Abyssal Echoes перекинул варианты. Есть нужный?",
                                opts + [("Нет" + (" — перекинуть Abyssal Echoes" if echoes and not attempt else " — взять любой"), None)], it)
                if pick is not None:
                    break
            pick = pick or M(u.s)
            pick.de = 1
            it.mods.append(pick)
            return pick
        pick = None
        for attempt in range(2 if echoes else 1):
            if attempt:
                self.pay(step, ECHOES)
            opts, ex = [], set()
            lord = None
            if self.c.lord_omen and not attempt:
                lord = next((self.c.targets[i].get("lord") for i in missi if self.c.targets[i].get("lord")), None)
                if lord == "altered" and u is not getattr(self, "alt_u", None):
                    lord = None  # an ordinary bone: no Otherworldly option
                if lord and lord in self.c.lords:
                    if lord in LORD_OMEN:  # an Altered bone is itself what brings the Otherworldly option
                        self.pay(step, LORD_OMEN[lord])
                    o = self.draw(it, u.s, floor, exclude=ex, miss=missi, pool=self.c.lords[lord], tg=self.c.ltg[lord])
                    if o is not None:
                        opts.append(o)
                        if o.g is not None:
                            ex.add(o.g)
            while len(opts) < 3:
                o = self.draw(it, u.s, floor, exclude=ex, miss=missi, pool=self.c.dpool, tg=self.c.dtg)
                if o is None:
                    break
                opts.append(o)
                if o.g is not None:
                    ex.add(o.g)
            good = [o for o in opts if o.t >= 0]
            if good:
                pick = next((o for o in good if self.c.targets[o.t]["src"] == "desecrated"), good[0])
                break
        if pick is None:
            # no wanted option: the player takes a junk one that does not block a mod still wanted (a low tier of a
            # wanted mod would take its group and need an Annul later)
            need = {t["group"] for t in self.missing(it) if t["side"] == u.s}
            pick = next((o for o in opts if o.g is None or o.g not in need), opts[0] if opts else M(u.s))
        pick.de = 1
        it.mods.append(pick)
        if step:
            self.tally(step, pick.t >= 0)
        return pick

    # -- phases
    def first_phase(self, fresh=False):
        """get the 'first' target ("any": any acceptable wanted mod) on a magic item, and fracture it if asked;
        returns the rare item"""
        c, v = self.c, self.v
        if v["first"] == "any":
            Fs, frac = c.anyset, v["fmode"] != "none"
        else:
            Fs = self.first_set(v["first"])
            frac = Fs[0]["frac"]
        Fid = {F["i"] for F in Fs}
        goal = "любой нужный мод" if v["first"] == "any" else self._goal(Fs)
        it = (self.again_item() if fresh else c.start.copy()) if c.start else None
        fresh = False
        on = lambda x: any(m.t in Fid and not m.un for m in x.mods)
        while self.ops < self.cap:
            if it is None:
                fresh = True
                if self.ops >= self.cap:
                    return None
            if not (it is not None and on(it)):
                if v["fget"].startswith("chaos"):
                    cq = v["fget"].partition(":")[2]
                    if it is None or it.rar != 2 or fresh:
                        it = self.white("Новая база")
                        step = "Синий предмет: Transmute + Augment"
                        self.pay(step, CUR["transmute"][""]); it.rar = 1; self.add(it, 0)
                        self.pay(step, CUR["augment"][""]); self.add(it, 0)
                        self.to_rare(it, essence=False)
                        fresh = False
                        # strategy "ftrim": Annul the fresh rare down to one mod before the Chaos spam — the wanted mod
                        # then lands on an almost empty item: both sides have room, a crafted mod later replaces junk safely
                        while v.get("ftrim") and len(it.mods) > 1 and not on(it) and self.ops < self.cap:
                            self.pay("Снять моды до одного перед Chaos: Orb of Annulment", ANNUL)
                            self.remove(it)
                    if not on(it) and not self.chaos_until(f"{CUR['chaos'][cq]} до «{goal}»", it, Fs, cq):
                        return None
                elif v["fget"].startswith("annul"):
                    q = v["fget"].partition(":")[2]
                    step = (f"Поймать «{goal}» на синем, одна база: {CUR['transmute'][q]} + {CUR['augment'][q]}, "
                            f"промах — {ANNUL} + {CUR['augment'][q]}")
                    it = self.magic_annul(step, Fs, q)
                    if it is None:
                        return None
                    self.to_rare(it)
                    fresh = False
                else:
                    q = v["fget"]
                    step = (f"Поймать «{goal}» на синем, каждый раз новая база: "
                            + (f"{CUR['transmute'][q]} + {CUR['augment'][q]}; оставить, только если есть он и ещё один нужный мод"
                               if v.get("keep") == 3 else
                               f"{CUR['transmute'][q]} + {CUR['augment'][q]}; оставить, если есть он или сразу два нужных мода"
                               if v.get("keep") == 2 else f"{CUR['transmute'][q]} + {CUR['augment'][q]}"
                               if v.get("fmode") in ("bone", "four") else f"{CUR['transmute'][q]} (+ {CUR['augment'][q]}, если не попал)"))
                    it = self.magic_spam(step, Fs, q)
                    if it is None:
                        return None
                    self.to_rare(it)
                    fresh = False
            if it.rar < 2:
                self.to_rare(it)
            if not frac:
                if v["first"] != "any" and not any(m.t == v["first"] for m in it.mods):
                    got = next((m.t for m in it.mods if m.t in Fid and m.t != v["first"]), None)
                    if got is not None:
                        self._swap(v["first"], got)
                return it
            need = FRACTURE_MIN_MODS_BONE if v["fmode"] == "bone" else FRACTURE_MIN_MODS
            step = f"Добить до {need} модов: Exalted Orb (для Fracturing Orb)"
            while sum(1 for m in it.mods if not m.un) < need and self.ops < self.cap:
                self.exalt(step, it, "")
            if v["fmode"] == "bone" and not any(m.de for m in it.mods):
                dt = next((t for t in c.targets if t["src"] == "desecrated"), None)
                q = "Gnawed" if c.ilvl <= 64 else "Preserved"
                self.bone(f"Кость под фрактур ({q} {bone_kind(c.cls)}, мод не раскрывать)", it, q, dt["side"] if dt else None)
            self.pay("Fracturing Orb", FRACTURE)
            elig = [m for m in it.mods if not m.un]
            # another wanted mod at least as hard to get as the planned one (e.g. Cold instead of Fire resistance) is as good
            # a lock: the planned one is then slammed like the rest. An easier one is not: slamming the hard mod without
            # the lock costs more than a new base. "any": every wanted mod counts
            fine = lambda m: m.t in Fid or (m.t >= 0 and (v["first"] == "any" or c.targets[m.t]["src"] == "natural"
                                                          and self._as_hard(c.targets[m.t], Fs)))
            self.fp += sum(1 for m in elig if fine(m)) / len(elig)  # the exact chance of this attempt (for fscore)
            pick = self.pick_mod("Какой мод зафрактурился?", it, elig)
            pick.fr = 1
            good = fine(pick)
            self.tally("Fracturing Orb", good)
            if good:
                if v["first"] != "any" and pick.t != v["first"] and self._swap(v["first"], pick.t):
                    self.info(f"Зафрактурился «{c.targets[pick.t]['text']}» вместо «{c.targets[v['first']]['text']}» — это тоже "
                              f"хорошо: они меняются ролями, «{c.targets[v['first']]['text']}» получим десекрейтом.")
                elif v["first"] == "any" and pick.t not in Fid or v["first"] != "any" and pick.t != v["first"]:
                    self.relaxed = True
                    self.info(f"Зафрактурился другой нужный мод — это тоже хорошо: он закреплён, а «{self._goal(Fs)}» "
                              "добавим дальше обычным способом.")
                return it if self.ops < self.cap else None
            self.info("Fracturing Orb закрепил не тот мод: закреплённый мусор не снять, предмет для цели не годится. " +
                      ("Берём ещё один такой же предмет." if c.start else "Начинаем с новой белой базы."))
            it = self.again_item() if c.start else None  # wrong mod locked: start over
        return None

    def begin(self, again=False):
        """the rare base the main phase works on (again: a new one after giving up the last)"""
        if again:
            self.c = self.c0
        c, v = self.c, self.v
        if c.start and not again and not self.ia:
            # the player's own item is spent on the craft too: its price (the base price field) is part of the total
            self.pay("Ваш предмет", "Ваш предмет", price=c.base_price)
        if again:
            self.restarts += 1
            self.relaxed = False
            self.info(("Берём ещё один такой же предмет" if c.start else "Начинаем с новой базы") +
                      ": так в среднем дешевле, чем вычищать мусор с этой.")
        if v.get("first") is not None:
            it = self.first_phase(fresh=again)
            if it is None:
                return None
            self.quality(it)
        else:
            it = (self.again_item() if again else c.start.copy()) if c.start else self.white("Новая база")
            if it.rar == 0:
                step = "Синий предмет: Transmute + Augment"
                self.pay(step, CUR["transmute"][""]); it.rar = 1; self.add(it, 0)
                self.pay(step, CUR["augment"][""]); self.add(it, 0)
            if it.rar == 1:
                self.to_rare(it)
        if self.missing(it):
            free = getattr(c, "free_sockets", 0) if c.start and not again else 0
            for name in self.runes_once:
                if free > 0:
                    free -= 1  # an empty socket on the player's item: no Artificer's Orb needed
                else:
                    self.pay("Руны в сокет (один раз)", ARTIFICER)
                self.pay("Руны в сокет (один раз)", name)
        return it

    def quality(self, it):
        """the goal needs catalyst quality above 20% (+3 spell levels show +4 at 40%): Essence of the Breach (+20% to
        maximum quality) replaces a junk prefix (Sinistral Crystallisation) or, with a free prefix slot, a junk suffix
        (Dextral); then catalysts. The Breach mod is junk for the rest of the craft (the quality stays). Tried again on
        every step until it fits"""
        c = self.c
        q = getattr(c, "quality", 0)
        if getattr(self, "qdone", False) or q <= 20 or c.cls not in JEWELLERY or it.rar != 2:
            return
        jp = [m for m in it.mods if m.s == "P" and m.t < 0 and not m.fr and not m.un]
        js = [m for m in it.mods if m.s == "S" and m.t < 0 and not m.fr and not m.un]
        wanted_p = any(m.t >= 0 and m.s == "P" and not m.fr for m in it.mods)
        risky = False
        if jp and not wanted_p:
            r, rem = "P", jp
        elif self.room(it, "P") > 0 and js and not any(m.t >= 0 and m.s == "S" and not m.fr for m in it.mods):
            r, rem = "S", js
        elif jp:
            # no safe way and later there will be even less room: now, on a prefix, a wanted one may go
            r, rem, risky = "P", [m for m in it.mods if m.s == "P" and not m.fr and not m.un], True
        else:
            return
        self.qdone = True
        other = "S" if r == "P" else "P"
        need_om = any(m.s == other and not m.fr for m in it.mods)
        step = (f"Качество до {q}% (как у цели), 1/2: Essence of the Breach" + (f" + {OMEN['cryst'][r]}" if need_om else "") +
                f" на {'случайный' if risky else 'мусорный'} {SIDE_RU[r]}" + (" (может снять нужный: безопасного места нет, позже его не "
                "будет совсем)" if risky else "") + " — +20% к максимуму качества; Breach-мод дальше уходит как обычный мусор")
        cat = getattr(c, "catalyst", None) or "Sibilant Catalyst"
        step2 = f"Качество до {q}%, 2/2: {cat} ×{q} (по +1% за штуку — допущение) — усиливает моды этого типа"
        self.pay(step, "Essence of the Breach")
        if need_om:
            self.pay(step, OMEN["cryst"][r])
        it.mods.remove(self.pick_mod(f"Какой {SIDE_RU[r]} заменила Essence of the Breach?", it, rem) if self.ia else
                       (self.r.choice(rem) if risky else rem[0]))
        it.mods.append(M("P", lv=1))
        self.pay(step2, cat, q)

    def fix_or_restart(self, it, s):
        """junk blocks side s: clean it, or (strategy 'restart', few wanted mods on the item yet) start a new base.
        Step mode: the player may still choose to clean"""
        k = self.v.get("restart") or 0
        wanted = sum(1 for m in it.mods if m.t >= 0)
        if k and wanted < k and not self.c.start:
            go = True
            if self.ia:
                go = self.ask(f"Ненужный {SIDE_RU[s]} занимает место, нужных модов на предмете {wanted}. Что делаем?",
                              [("Новая база (в среднем дешевле)", True), ("Продолжить: убрать мусор", False)], it)
            if go:
                return self.begin(again=True)
        self.fix(it, s)
        return it

    def putref_ok(self, it):
        """the wanted set can still be completed from the unrevealed mods left"""
        miss = self.missing(it)
        un = {x: sum(1 for m in it.mods if m.un and m.s == x) for x in "PS"}
        req = {x: sum(1 for t in miss if t["side"] == x and t in self.c.req) for x in "PS"}
        if any(req[x] > un[x] for x in "PS"):
            return False
        if self.c.need_opt:
            have = len(self.sat(it) & {t["i"] for t in self.c.opt})
            return req["P"] + req["S"] + max(0, self.c.need_opt - have) <= un["P"] + un["S"]
        return True

    def putref(self, once=False):
        """Omen of Putrefaction: a bone on a rare base replaces every mod with 6 unrevealed ones (3 + 3) and corrupts
        the item; each is revealed as 1 of 3 options (Abyssal Echoes rerolls them once). A miss = a new base"""
        c, v = self.c, self.v
        bq = v["bone"]
        floor = ANCIENT_BONE_FLOOR if bq == "Ancient" else 0
        bone = f"{bq} {bone_kind(c.cls)}"
        tar = [CUR["transmute"][""], CUR["augment"][""], CUR["regal"][""]]
        alch = c.prices.get(ALCHEMY) is not None and c.price(ALCHEMY) <= sum(c.price(x) for x in tar)
        step1 = f"Новая база → редкая: {ALCHEMY}" if alch else "Новая база → редкая: " + " + ".join(tar)
        step2 = f"Осквернить: {PUTREF} + {bone} (все моды → 6 нераскрытых, предмет станет corrupted)"
        step3 = ("Раскрыть моды в Well of Souls: из 3 вариантов брать нужный" +
                 ("" if not v["echoes"] else
                  f"; нет нужного — {ECHOES} (только если без этого мода набор не собрать)" if v.get("echo") == "need" else
                  f"; нет нужного — {ECHOES} (перекинуть 3 варианта)"))
        while self.ops < self.cap:
            if self.restarts:
                self.info("Набор уже не собрать — эта база не годится. Новая база.")
            self.pay(step1, "Белая база", price=c.base_price)
            for x in ([ALCHEMY] if alch else tar):
                self.pay(step1, x)
            self.pay(step2, PUTREF)
            self.pay(step2, bone)
            it = It([M(x, de=1, un=1) for x in "PPPSSS"], 2)
            self.last = it
            for u in list(it.mods):
                if not self.missing(it):
                    self.info("Всё нужное уже есть: остальные моды раскройте как угодно, Echoes не нужны.")
                    break
                if not self.putref_ok(it):
                    break
                self.reveal(step3, it, u, floor)
            self.last = it
            ok = not self.missing(it)
            self.tally(step2, ok)
            if ok or once:
                return ok
            self.restarts += 1
        return False

    def run(self, only_first=False):
        c, v = self.c, self.v
        if v.get("putref"):
            return self.putref(once=only_first)
        if only_first:
            return self.first_phase() is not None
        it = self.begin()
        if it is None:
            return False
        q, light = v["slam"], v["light"]
        while self.ops < self.cap:
            c = self.c  # roles may have been swapped in the first phase (also after a restart)
            self.quality(it)
            self.last = it
            self.dedup(it)
            miss = self.missing(it)
            if not miss:
                break
            u = next((m for m in it.mods if m.un), None)
            if u is not None and any(t["src"] == "desecrated" for t in miss) and \
                    not any(t["src"] == "desecrated" and t["side"] == u.s for t in miss):
                # the bone sits on the other affix type, and only one desecrated mod is allowed: reveal it as junk,
                # Omen of Light + Annul take it off, then the bone for the wanted desecrated mod
                self.reveal("Раскрыть кость в Well of Souls: взять любой вариант (десекрейт-мод нужен под другой мод)",
                            it, u, 0, echoes=False, take_wanted=False)
                continue
            if u is not None and not any(t["src"] == "desecrated" and t["side"] == u.s for t in miss):
                can = any(t["side"] == u.s and t["src"] in ("natural", "desecrated") and not t["frac"] for t in miss)
                self.reveal("Раскрыть кость в Well of Souls: " + ("взять нужный мод, если он есть среди 3, иначе любой" if can else
                                                                  "взять любой вариант"), it, u, 0, echoes=False)
                continue
            # 1. crafted mods that remove a random mod: on an empty side first (nothing is removed), else on a side of junk.
            # Order: a crafted mod that lands where another one can still remove safely (an empty side or only junk) goes
            # last — e.g. on a wand with +4 (suffix): the suffix alloy + Sinistral Crystallisation first, then the prefix alloy
            crt = [x for x in miss if x["src"] == "alloy" or (x["src"] == "essence" and not x["magic"])]
            # Chaos is still hunting the next natural mod: a crafted mod now would be at risk from every Chaos Orb
            hold = ("cslam" in v and not any(self.precious(m) for m in it.mods) or
                    v.get("crepair") and not any(self.precious(m) for m in it.mods)) and \
                any(x["src"] == "natural" and c.pool.g[x["side"]].get(x["group"]) for x in miss)
            if v.get("cw") == "pre":
                hold = False
            if any(m.g == ABYSS_MARK for m in it.mods):
                hold = True  # the Mark holds a crafted place until the next bone turns it into a desecrated mod  # crafted mods before anything else: later a crafted mod would replace a found wanted mod

            def blocks(x):
                # x's side holds only junk (or nothing): another crafted mod can remove from it safely, but not once x is there
                return any(y is not x and all(m.t < 0 for m in it.mods if m.s == x["side"] and not m.fr)
                           and self.room(it, y["side"]) > 0 for y in crt if y["side"] != x["side"])
            done = False
            for t in sorted(miss, key=blocks):
                if t["src"] not in ("essence", "alloy") or (t["src"] == "essence" and t["magic"]) or hold:
                    continue
                if v.get("cw") == "abyss" and any(x["side"] == t["side"] and x["src"] == "natural" for x in miss):
                    # strategy "abyss": this side's natural mod first, by bone with the Essence of the Abyss cycle
                    # (the Mark needs the side free of crafted mods); this crafted mod comes after it
                    continue
                if self.crafted(it) >= c.max_c:
                    return False
                opts = []
                for r in "PS":
                    # an alloy / this essence always replaces a mod: Crystallisation needs a mod of its type to remove
                    # (the game does not let it go on a side without one)
                    rem = [m for m in it.mods if m.s == r and not m.fr]
                    if not rem:
                        continue
                    after = self.room(it, t["side"]) + (1 if r == t["side"] else 0)
                    if after <= 0:
                        continue
                    junk = sum(1 for m in rem if m.t < 0)
                    kind = "safe" if junk == len(rem) else "mixed"
                    opts.append((kind, junk / len(rem), r, rem))
                nat_left = any(x["src"] == "natural" or (x["src"] == "essence" and x["magic"]) for x in miss)
                cw = v.get("cw", "first")
                if cw != "pre" and c.price(t["name"]) >= EXPENSIVE and any(x["i"] != t["i"] and x["src"] not in ("essence", "alloy") for x in miss):
                    continue  # an expensive essence / alloy only when everything else is already there
                if cw == "last" and any(x["side"] == t["side"] and x["src"] in ("natural", "desecrated") for x in miss):
                    continue  # later cleanups on this side would take the crafted mod off again
                # a side of junk only: the crafted mod replaces junk and never a wanted mod
                best = next((o for o in opts if o[0] == "safe"), None)
                if best is None and nat_left and cw in ("junk", "last"):
                    continue  # keep the crafted mod as the eraser of a missed slam / for the end
                if best is None:
                    # nothing to replace safely: first put a mod where only junk is (or nothing), then the crafted mod
                    # replaces it. If a wanted mod can roll there it is the plan's Exalted (a hit stays); otherwise the
                    # cheapest one, it is only there to be replaced
                    gs = [r for r in "PS" if self.room(it, r) > 0 and all(m.t < 0 for m in it.mods if m.s == r and not m.fr)
                          and (r == t["side"] or self.room(it, t["side"]) > 0)]
                    if not gs and cw == "abyss" and self.room(it, t["side"]) > 0 and \
                            not any(m.s == t["side"] and m.t < 0 and not m.fr for m in it.mods) and \
                            all(m.de for m in it.mods if m.s == t["side"] and m.t >= 0 and not m.fr):
                        # only next to a mod from a bone (cheap to get again by the cycle), never next to the +4 and the like
                        gs = [t["side"]]  # one junk next to the wanted mod: the crafted mod risks it 1 time in 2; a miss
                        # still leaves a free slot for the next bone (two junk would need a dear side Annul then)
                    if gs:
                        r = max(gs, key=lambda x: (x != t["side"], self.room(it, x)))
                        other = "S" if r == "P" else "P"
                        om = self.room(it, other) > 0
                        hit = any(x["side"] == r and x["src"] in ("natural",) and c.pool.g[r].get(x["group"]) for x in miss)
                        # strategy "pbone": the placeholder by a bone (1 of 3, Echoes 3 more) instead of an Exalted: a wanted
                        # option stays, any other is replaced by the crafted mod anyway (no Omen of Light needed). One
                        # desecrated mod per item: only while there is none
                        dhit = [x for x in miss if x["side"] == r and x["src"] == "natural" and
                                (c.dpool.g[r].get(x["group"]) or any(c.dpool.g[a["side"]].get(a["group"])
                                                                    for a in c.alias_of.get(x["i"], [])))]
                        # (a wanted option kept there leaves the crafted mod nothing safe to replace on this side, so
                        # only where the other side still has junk for it)
                        alt_safe = any(rr != r and self.room(it, t["side"]) + (1 if rr == t["side"] else 0) > 0 and
                                       [m for m in it.mods if m.s == rr and not m.fr] and
                                       all(m.t < 0 for m in it.mods if m.s == rr and not m.fr) for rr in "PS")
                        if v.get("pbone") and dhit and alt_safe and not any(m.de for m in it.mods):
                            bq, bk = v["bone"], bone_kind(c.cls)
                            step = (f"Добавить {SIDE_RU[r]} под замену: {bq} {bk}" +
                                    (f" + {OMEN['necro'][r]}" if self.room(it, other) > 0 else "") +
                                    (f" + {ECHOES}" if v["echoes"] else "") +
                                    f"; нужный среди вариантов — взять, иначе любой: его заменит {t['name']} + {OMEN['cryst'][r]}")
                            before = {x["i"] for x in miss}
                            self.bone(step, it, bq, r)
                            u = next((m for m in it.mods if m.un), None)
                            self.reveal(step, it, u, ANCIENT_BONE_FLOOR if bq == "Ancient" else 0)
                            self.tally(step, bool(before & self.sat(it)))
                            done = True
                            break
                        if cw in ("pre", "abyss"):
                            hit = False  # a pure placeholder: a wanted mod there would be replaced by the crafted mod later
                        qq = q if hit else ""
                        how = f" + {OMEN['exalt'][r]}" if om else f" ({SIDE_RU[other]}ы заполнены — ляжет в {SIDE_RU[r]})"
                        step = (f"Добавить {SIDE_RU[r]} под замену: {CUR['exalt'][qq]}{how}; " +
                                ("выпал нужный — остаётся, иначе его " if hit else "его ") +
                                f"заменит {t['name']} + {OMEN['cryst'][r]}")
                        before = {x["i"] for x in miss}
                        self.exalt(step, it, qq, r, False)
                        if hit:  # a chance only where a wanted mod can come; a pure placeholder has none to show
                            self.tally(step, bool(before & self.sat(it)))
                        done = True
                        break
                if best is None:
                    # a wanted mod may have to go: the one cheapest to get back (a crafted mod rather than a +4 spell level)
                    loss = lambda o: sum(c.tval(c.targets[m.t]) for m in o[3] if m.t >= 0) / len(o[3])
                    best = min((o for o in opts if o[0] == "mixed"), key=loss, default=None)
                    # strategy "wfix": first free a slot with Chaos + Omen of Whittling (it takes the lowest-level mod, junk
                    # here) instead of the gamble; whether that pays is for the planner to find out on whole crafts
                    rem_all = [m for m in it.mods if not m.fr and not m.un]
                    lo = min((m.lv for m in rem_all), default=None)
                    # only with room on the other side: the Chaos puts a new mod back, and it frees the slot only by
                    # landing there (with the other side full it lands on the same side every time: an endless loop)
                    if v.get("wfix") and best is not None and lo is not None and all(m.t < 0 for m in rem_all if m.lv == lo) \
                            and self.room(it, "S" if best[2] == "P" else "P") > 0:
                        rs_ = best[2]
                        nw = sum(1 for m in best[3] if m.t >= 0)
                        step = (f"Освободить {SIDE_RU[rs_]} под «{t['name']}»: Chaos Orb + Omen of Whittling (снимет мусор с "
                                "наименьшим уровнем)")
                        self.pay(step, CUR["chaos"][""]); self.pay(step, WHITTLING)
                        low = [m for m in rem_all if m.lv == lo]
                        it.mods.remove(self.pick_mod("Какой мод снялся?", it, rem_all) if self.ia else self.r.choice(low))
                        self.add(it, 0, None, "Что добавил этот Chaos Orb взамен?")
                        done = True
                        break
                if best is None:
                    s = t["side"]
                    if any(not m.fr for m in it.mods if m.s == s):
                        it = self.fix_or_restart(it, s)
                        if it is None:
                            return False
                        done = True
                        break
                    return False
                kind, _, r, rem = best
                # every removable mod is of this type: the crafted mod removes from it anyway, no omen needed
                need_om = any(m.s != r and not m.fr for m in it.mods)
                other = "S" if r == "P" else "P"
                step = f"Крафт-мод «{t['text']}»: {t['name']}" + (f" + {OMEN['cryst'][r]}" if need_om else "") + \
                    {"safe": f" (заменит мусорный {SIDE_RU[r]})",
                     "mixed": f" (заменит случайный {SIDE_RU[r]}: может снять нужный)"}[kind]
                if kind == "safe" and r == "P" and need_om and c.cls in JEWELLERY and len(miss) == 1 \
                        and not getattr(self, "qopt", False) and getattr(c, "quality", 0) <= 20:
                    # optional, adds value: the last crafted prefix replaces junk anyway, so first put Essence of the Breach
                    # (+20% maximum quality) there, raise quality to 40% with catalysts, then this step replaces it
                    self.qopt = True
                    qs = (f"По желанию, качество до 40%: перед шагом «{t['name']}» поставить Essence of the Breach + "
                          f"{OMEN['cryst']['P']} (заменит мусорный префикс, даст +20% к максимуму качества) → "
                          f"{getattr(c, 'catalyst', None) or 'каталисты'} до 40% → потом {t['name']} заменит Breach-мод, "
                          "качество останется")
                    if self.ia:
                        self.info(qs)
                    else:
                        self.pay(qs, "Essence of the Breach"); self.pay(qs, OMEN["cryst"]["P"])
                self.pay(step, t["name"])
                if need_om:
                    self.pay(step, OMEN["cryst"][r])
                if rem:
                    it.mods.remove(self.pick_mod(f"Какой {SIDE_RU[r]} заменил {t['name']}?", it, rem))
                it.mods.append(M(t["side"], t["group"], t["i"], cr=1, lv=t["minLevel"]))
                done = True
                break
            if done:
                continue
            # a magic-only essence that no longer fits: roll the same mod naturally
            # one desecrated mod per item: while a wanted one is on it (a bone already gave another mod of the set), a mod
            # planned for desecration is slammed instead
            des_full = any(m.de and not m.un and m.t >= 0 for m in it.mods)
            nat = [t for t in miss if t["src"] == "natural" or (t["src"] == "essence" and t["magic"]) or
                   (des_full and t["src"] == "desecrated" and c.pool.g[t["side"]].get(t["group"]))]
            # a mod planned for slams that a rare (Perfect / corrupted) essence also gives: when the slams (with their misses
            # to clean) cost more, the essence + Crystallisation gives it for sure — it becomes a crafted mod of this craft
            if nat and not v.get("nodyn") and \
                    self.crafted(it) + sum(1 for x in miss if x["src"] in ("essence", "alloy") and not x.get("magic")) < c.max_c:
                best = None
                for t in nat:
                    if t["src"] != "natural":
                        continue
                    for e in t.get("ess_all") or []:
                        if e.get("magic"):
                            continue
                        cost = c.price(e["name"]) + c.price(OMEN["cryst"][t["side"]])
                        if cost < 0.7 * c.tval(t) and (best is None or cost < best[0]):
                            best = (cost, t, e)
                if best and self._to_essence(best[1], best[2]):
                    continue
            if nat and "cslam" in v and not any(self.precious(m) for m in it.mods) and any(not m.fr for m in it.mods):
                # strategy "cslam": while only fractured wanted mods are on the item, a Chaos Orb cannot take anything of
                # value, and it removes junk and adds a mod in one go: Chaos until any missing mod comes
                Fs = [x for x in nat if x["src"] == "natural" and c.pool.g[x["side"]].get(x["group"])]
                cq = v["cslam"]
                # "ctrim": first plain Annuls down to one junk mod (safe: only junk and the fracture are on it; the quality
                # stays when the Breach mod goes). The Chaos then always replaces that one, and once it hits, the next
                # mods have free slots: no removal with a wanted mod at risk
                while Fs and v.get("ctrim") and sum(1 for m in it.mods if not m.fr) > 1 and self.ops < self.cap:
                    self.pay("Снять мусор до одного мода перед Chaos: Orb of Annulment (кроме фрактура нужного нет)", ANNUL)
                    self.remove(it)
                if Fs and v.get("chard"):
                    # only the rarest one: the easier mods come after it by Exalted, with fewer misses to clean up
                    Fs = [min(Fs, key=lambda x: self._p_slam(x) or 1)]
                if Fs and self.chaos_until(f"{CUR['chaos'][cq]} до «{self._goal(Fs)}»", it, Fs, cq, lose_cr=True):
                    continue
            if nat and v.get("crepair") and not any(self.precious(m) for m in it.mods) and \
                    any(not m.fr for m in it.mods):
                # strategy "crepair": a natural mod is missing and the only wanted mods on the item are fractured or
                # crafted (cheap to craft again): Chaos spam for it instead of Exalted + Annul cycles, the crafted mod
                # goes back on afterwards
                Fs = [x for x in nat if x["src"] == "natural" and c.pool.g[x["side"]].get(x["group"])]
                crs = any(m.cr and m.t >= 0 for m in it.mods)
                if Fs and self.chaos_until(f"{CUR['chaos']['']} до «{self._goal(Fs)}»" +
                                           (" (крафт-мод может слететь — поставить заново)" if crs else ""),
                                           it, Fs, "", lose_cr=True):
                    continue
            if nat and v.get("dslam") and not any(x["src"] == "desecrated" for x in miss) and \
                    not any(m.de and m.t >= 0 for m in it.mods):
                # strategy "dslam": no mod is planned for desecration, so the one desecrated slot of the item adds a
                # missing natural mod: a bone + Necromancy omen on its side reveals 1 of 3 (Echoes: 3 more) instead of
                # a blind Exalted. A miss: Annul + Omen of Light takes only it off and the bone goes again ("light"),
                # or the rest comes by Exalted ("once")
                dj = next((m for m in it.mods if m.de and not m.un and m.t < 0), None)
                if dj is not None and v["dslam"] == "light":
                    if v["light"] != "abyss":
                        step = "Убрать неудачный десекрейт: Orb of Annulment + Omen of Light"
                        self.pay(step, ANNUL); self.pay(step, LIGHT)
                        self.remove(it, only_de=True)
                    else:
                        self.undo_des(it, dj)
                    continue
                def des_ok(x):
                    return bool(c.dpool.g[x["side"]].get(x["group"])) or \
                        any(c.dpool.g[a["side"]].get(a["group"]) for a in c.alias_of.get(x["i"], []))
                ds = [x for x in nat if x["src"] == "natural" and (self.room(it, x["side"]) > 0 or self.has_mark(it, x["side"])) and des_ok(x)] \
                    if dj is None and not any(m.un for m in it.mods) else []
                if ds:
                    t = min(ds, key=lambda x: self._p_slam(x) or 1)
                    bq, bk = v["bone"], bone_kind(c.cls)
                    what = f"мод из пула ({SIDE_RU[t['side']]})" if t.get("opt") else f"«{t['text']}»"
                    step = f"Добавить {what}: {bq} {bk}" + \
                        (f" + {OMEN['necro'][t['side']]}" if self.room(it, 'S' if t['side'] == 'P' else 'P') > 0 and not self.has_mark(it, t['side']) else "") + \
                        (f" + {ECHOES}" if v["echoes"] else "")
                    self.bone(step, it, bq, t["side"])
                    u = next((m for m in it.mods if m.un), None)
                    self.reveal(step, it, u, ANCIENT_BONE_FLOOR if bq == "Ancient" else 0)
                    continue
            if nat:
                t = next((t for t in nat if self.room(it, t["side"]) > 0), nat[0])
                s = t["side"]
                if self.room(it, s) > 0:
                    need = {x["side"] for x in nat if self.room(it, x["side"]) > 0}
                    if len(need) == 2:
                        # wanted mods missing on both sides: a plain Exalted can hit either, no side omen
                        k = len(nat)
                        g = v["greater"] and self.room(it, "P") + self.room(it, "S") >= 2 and k >= 2
                        step = f"Добавить нужные моды: {CUR['exalt'][q]}" + (" + Greater Exaltation" if g else "")
                        side = None
                    else:
                        k = sum(1 for x in nat if x["side"] == s)
                        g = v["greater"] and self.room(it, s) >= 2 and k >= 2
                        om = self.room(it, "S" if s == "P" else "P") > 0
                        what = f"мод из пула ({SIDE_RU[s]})" if t.get("opt") else f"«{t['text']}»"
                        step = f"Добавить {what}: {CUR['exalt'][q]}" + (" + Greater Exaltation" if g else "") + \
                            (f" + {OMEN['exalt'][s]}" if om else "")
                        side = s
                    before = {x["i"] for x in nat} - self.sat(it)
                    self.exalt(step, it, q, side, g)
                    self.tally(step, bool(before & self.sat(it)))
                else:
                    if not any(not m.fr for m in it.mods if m.s == s):
                        return False
                    it = self.fix_or_restart(it, s)
                    if it is None:
                        return False
                continue
            t = next((t for t in miss if t["src"] == "desecrated"), None)
            if t is None:
                return False
            u = next((m for m in it.mods if m.un), None)
            dj = next((m for m in it.mods if m.de and not m.un and m.t < 0), None)
            if dj is not None:
                self.undo_des(it, dj)
                continue
            alt = t.get("lord") == "altered"  # an Otherworldly mod comes only from an Altered bone
            bq = "Altered" if alt else v["bone"]
            step = f"Десекрейт «{t['text']}»: {bq} {bone_kind(c.cls)}" + (" + Abyssal Echoes" if v["echoes"] else "")
            if u is None:
                if self.room(it, t["side"]) <= 0 and not self.has_mark(it, t["side"]):
                    it = self.fix_or_restart(it, t["side"])
                    if it is None:
                        return False
                    continue
                self.bone(step, it, bq, t["side"])
                u = next((m for m in it.mods if m.un), None)
                self.alt_u = u if alt else None
                floor = ANCIENT_BONE_FLOOR if bq == "Ancient" else 0
            else:
                floor = 0
            self.reveal(step, it, u, floor)
        else:
            return False
        if not self.missing(it):
            # an optional finish that adds value: a bone in a free slot is a choice of 1 of 3 (with Abyssal Echoes, of 6)
            # instead of a blind Exalted; only one desecrated mod per item, so only if there is none yet
            sides = [x for x in "PS" if self.room(it, x) > 0]
            if sides and not any(m.de for m in it.mods):
                bk = bone_kind(c.cls)
                step = f"По желанию: Preserved {bk} + {ECHOES} в свободный аффикс (взять лучший из вариантов)"
                if self.ia:
                    self.info(step)
                else:
                    self.pay(step, f"Preserved {bk}"); self.pay(step, ECHOES)
                    it.mods.append(M(sides[0], de=1))
            if v.get("fill"):
                step = "По желанию: заполнить свободные слоты (Perfect Exalted + Greater Exaltation)"
                while self.room(it, "P") + self.room(it, "S") > 0 and self.ops < self.cap + 50:
                    self.exalt(step, it, "Perfect", None, self.room(it, "P") + self.room(it, "S") >= 2)
            return True
        return False


def simulate(ctx, v, n, seed=1, only_first=False, early=True):
    rng = random.Random(seed)
    costs, agg, ok = [], {}, 0
    done, fp = 0, 0.0
    ref = getattr(ctx, "stop_ref", math.inf) if early else math.inf
    fin_costs = []
    for _ in range(n):
        if early and done == 40 and ok < 4:
            break  # hopeless variant: do not spend time on it
        if early and done == 10 and ok <= 3:
            break  # 3 of 10 finished: the (0.9/ok)^2 penalty alone puts it far behind; its crafts run to the action limit
        if early and done in (10, 20, 30) and len(fin_costs) >= 5 and \
                sorted(fin_costs)[len(fin_costs) // 2] > 2.5 * ref:
            break  # its median craft costs over 2.5x the best known strategy's mean: it loses (the median, so that one
            #        unlucky craft does not throw a good variant out)
        done += 1
        s = Sim(ctx, v, rng)
        fin = s.run(only_first)
        cut = not fin and s.ops >= s.cap
        if cut:
            # cut off by the action limit, not impossible: it still costs at least this much (keeps the mean honest)
            costs.append(s.cost)
        if fin:
            ok += 1
            costs.append(sum(e[0] for k, e in s.led.items() if not k.startswith("По желанию")))
            fin_costs.append(costs[-1])
        if fin or cut:  # steps and purchases over the same crafts as the price
            fp += s.fp
            for pos, (k, e) in enumerate(s.led.items()):  # place within its attempt (Sim._pos), else order of first use
                a = agg.setdefault(k, [0.0, {}, 0, 0, 0.0, 0])
                a[0] += e[0]; a[2] += e[2]; a[3] += e[3]; a[4] += e[4] if len(e) > 4 else pos; a[5] += 1
                for nm, x in e[1].items():
                    a[1][nm] = a[1].get(nm, 0) + x
    costs.sort()
    mean = sum(costs) / len(costs) if costs else math.inf
    return {"v": v, "ok": ok / max(done, 1), "mean": mean, "costs": costs, "agg": agg, "n": ok, "done": done, "fp": fp}


def merge(rs):
    costs, agg, ok, done, fp = [], {}, 0, 0, 0.0
    for r in rs:
        costs += r["costs"]; ok += r["n"]; done += r["done"]; fp += r.get("fp", 0.0)
        for k, a in r["agg"].items():
            b = agg.setdefault(k, [0.0, {}, 0, 0, 0.0, 0])
            for i in (0, 2, 3, 4, 5):
                b[i] += a[i]
            for nm, x in a[1].items():
                b[1][nm] = b[1].get(nm, 0) + x
    costs.sort()
    return {"v": rs[0]["v"], "ok": ok / max(done, 1), "mean": sum(costs) / len(costs) if costs else math.inf,
            "costs": costs, "agg": agg, "n": ok, "done": done, "fp": fp}


def rel_ci(r):
    """95% interval of the mean as a share of the mean"""
    x = r["costs"]
    if len(x) < 30 or not r["mean"] or not math.isfinite(r["mean"]):
        return math.inf
    m = r["mean"]
    sd = math.sqrt(sum((c - m) ** 2 for c in x) / (len(x) - 1))
    return 1.96 * sd / math.sqrt(len(x)) / m


def sim_final(ctx, v, n, nmin=8000, target=0.02, progress=None):
    """the chosen strategy on all cores: at least nmin crafts, more (up to n) until the mean is known to ±target"""
    W, rs, seed = _workers(), [], 1000
    ctx.cap = CAP * 3  # long crafts are not cut off in the exact run

    def batch(m):
        nonlocal seed
        args = [(ctx, v, m // W + (1 if j < m % W else 0), seed + j, False, False) for j in range(W)]
        seed += W
        rs.extend(_pmap(_task, args, (lambda i, n_: progress("Точный прогон лучшей стратегии")) if progress else None))

    batch(min(nmin, n))
    r = merge(rs)
    while r["done"] < n and rel_ci(r) > target:
        batch(min(nmin, n - r["done"]))
        r = merge(rs)
    ctx.cap = CAP
    ctx.missing_prices |= {nm for a in r["agg"].values() for nm in a[1] if nm not in BASES and nm not in ctx.prices}
    return r


_EX = None


def _executor():
    global _EX
    if _EX is None:
        _EX = ProcessPoolExecutor(max_workers=_workers())
    return _EX


def _workers():
    return max(2, min(12, (os.cpu_count() or 3) - 1))


def _task(a):
    return simulate(*a)


def _pmap(fn, args, tick=None, every=1):
    """results of fn over args from the process pool, in order; tick(done, total) is called as results come in and may
    raise (cancel): then every task not started yet is dropped instead of running on in the background"""
    try:
        ex = _executor()
        futs = [ex.submit(fn, a) for a in args]
    except Exception:
        out = []
        for i, a in enumerate(args):
            out.append(fn(a))
            if tick and (i + 1) % every == 0:
                tick(i + 1, len(args))
        return out
    out = []
    try:
        for i, f in enumerate(futs):
            out.append(f.result())
            if tick and ((i + 1) % every == 0 or i + 1 == len(futs)):
                tick(i + 1, len(futs))
    except BaseException:
        for f in futs:
            f.cancel()
        raise
    return out


def sim_many(ctx, vs, n, seed, only_first=False, progress=None, label="", cap=None):
    """two passes when there are several variants: half of the runs for all, the rest only for those within 1.8x of
    the best (clearly losing variants stop early; the survivors get the full sample). Costs are heavy-tailed: a
    smaller first pass or a tighter cut drops the true best now and then.
    ctx.ref_best (the best whole-craft score so far, set by _plan): crafts that spend over CAPK x it are cut off — a
    hopeless variant then costs a few crafts instead of running every craft to the action limit"""
    ref = getattr(ctx, "ref_best", math.inf)
    ctx.cost_cap, ctx.stop_ref = (CAPK * ref if math.isfinite(ref) else math.inf), ref
    cap0 = getattr(ctx, "cap", CAP)
    ctx.cap = cap or cap0
    try:
        rs = _sim_many2(ctx, vs, n, seed, only_first, progress, label)
    finally:
        ctx.cost_cap, ctx.stop_ref = math.inf, math.inf
        ctx.cap = cap0
    if not only_first and rs:
        ctx.ref_best = min([ref] + [score(r) for r in rs])
    if rs and max(r["ok"] for r in rs) < 0.5 and (cap or cap0) < CAP * 3:
        # almost no craft gets to the end within the action limit (a tight base, long loops): the comparison is noise
        # (crafts stop after 40 tries). Again with the exact run's limit
        cap, ctx.cap = getattr(ctx, "cap", CAP), CAP * 3
        try:
            rs = _sim_many2(ctx, vs, min(n, 200), seed, only_first, progress, label)  # long crafts: fewer of them
        finally:
            ctx.cap = cap
    return rs


def _sim_many2(ctx, vs, n, seed, only_first=False, progress=None, label=""):
    if len(vs) < 8 or n < 300:  # a few variants: all of them in full (cheap, and no lucky cut of the best)
        return _sim_many(ctx, vs, n, seed, only_first, progress, label)
    n1 = n // 2
    rs = _sim_many(ctx, vs, n1, seed, only_first, progress, label)
    key = fscore if only_first else score
    best = min(key(r) for r in rs)
    keep = [i for i, r in enumerate(rs) if math.isfinite(key(r)) and key(r) <= 1.8 * best]
    more = _sim_many(ctx, [vs[i] for i in keep], n - n1, seed + 99991, only_first, progress, label) if keep else []
    for i, r in zip(keep, more):
        rs[i] = merge([rs[i], r])
    return rs


def _sim_many(ctx, vs, n, seed, only_first=False, progress=None, label=""):
    """each variant split into chunks so that all cores work even for a few variants (same seeds across variants)"""
    k = max(1, min(_workers() * 2 // max(len(vs), 1), n // 150))
    args = [(ctx, v, n // k + (1 if j < n % k else 0), seed + 7919 * j, only_first, True) for v in vs for j in range(k)]
    rs = _pmap(_task, args, (lambda i, n_: progress(f"{label}: {i // k}/{len(vs)}")) if progress else None, every=k)
    return [merge(rs[i:i + k]) if k > 1 else rs[i] for i in range(0, len(rs), k)]


def fscore(r):
    """first phase up to a good fracture: all that was spent / the expected number of good fractures (the exact chance
    of every attempt summed). Unlike the plain mean it does not depend on how lucky the Fracturing Orbs were, so
    the ways of getting the first mod (Transmute grades, Annul, Chaos) are compared by what really differs"""
    if r.get("fp", 0) <= 0 or r["ok"] < 0.9:
        return score(r)
    return sum(a[0] for a in r["agg"].values()) / r["fp"]


def score(r):
    # crafts cut off by the action limit are already in the mean at what they spent; /ok adds the rest they would need
    # a strategy that rarely finishes is underestimated even so (cut-off crafts are lower bounds): penalise it hard
    ok = max(r["ok"], 1e-6)
    return r["mean"] / ok * (1 if ok >= 0.9 else (0.9 / ok) ** 2) if r["ok"] > 0 else math.inf


def describe(v, ctx):
    t = ctx.targets
    if v.get("putref"):
        return (f"Omen of Putrefaction + {v['bone']} {bone_kind(ctx.cls)} на редкой базе: все моды заменяются 6 нераскрытыми, "
                "из каждых 3 вариантов взять нужный" +
                ("" if not v["echoes"] else ", нет нужного — Abyssal Echoes, только если без этого мода набор не собрать"
                 if v.get("echo") == "need" else ", нет нужного — Abyssal Echoes") +
                "; не собралось — новая база. Предмет станет corrupted: дальше его не изменить")
    out = list(ctx.assign_desc)
    if v.get("first") == "any":
        fq = v["fget"].partition(":")[2]
        how = f"{CUR['chaos'][fq]} на редком" if v["fget"].startswith("chaos") \
            else f"{CUR['transmute'][fq]} на одной базе, промахи — Annul + Augment" if v["fget"].startswith("annul") \
            else f"{CUR['transmute'][v['fget']]} на новых белых базах"
        if ctx.start:
            how = "на вашем предмете, промах — ещё один такой же"
        names = ", ".join(f"«{x['text']}»" for x in ctx.anyset)
        out.insert(0, f"сначала любой нужный мод ({names}): {how}" +
                   (", затем 3 мода + кость и фрактур — годится любой нужный мод" if v["fmode"] == "bone" else
                    ", затем 4 мода и фрактур — годится любой нужный мод" if v["fmode"] == "four" else ""))
    elif v.get("first") is not None:
        F = t[v["first"]]
        fq = v["fget"].partition(":")[2]
        how = f"{CUR['chaos'][fq]} на редком" if v["fget"].startswith("chaos") \
            else f"{CUR['transmute'][fq]} на одной базе, промахи — Annul + Augment" if v["fget"].startswith("annul") \
            else f"{CUR['transmute'][v['fget']]} на новых белых базах"
        if ctx.start:
            how = "на вашем предмете, промах — ещё один такой же"
        names = [x["text"] for x in Sim(ctx, v, random.Random(0)).first_set(v["first"])]
        acc = "годится только он" if len(names) <= 1 else \
            "годится любой из: " + ", ".join(f"«{x}»" for x in names) + " — выпал другой, роли меняются"
        if v.get("keep") == 2:
            how += "; синий сразу с двумя нужными модами тоже оставляем"
        elif v.get("keep") == 3:
            how += "; синий оставляем, только если на нём он и ещё один нужный мод"
        if F["frac"]:
            out.insert(0, f"фрактур «{F['text']}»: {how}, затем " +
                       (f"3 мода + кость (шанс 1/3; {acc})" if v["fmode"] == "bone" else f"4 мода (шанс 1/4; {acc})"))
        else:
            out.insert(0, f"сначала «{F['text']}» ({acc}): {how}")
    have = {m.t for m in ctx.start.mods} if ctx.start else set()
    if any(x["src"] == "natural" and x["i"] != v.get("first") and x["i"] not in have for x in t) or v.get("first") == "any":
        out.append(f"слэм {CUR['exalt'][v['slam']]}" + (" + Greater Exaltation" if v["greater"] else ""))
        out.append({"omen": "чистка Annul + омен на префикс/суффикс", "plain": "чистка обычным Annul",
                    "value": "чистка: Annul без омена или с оменом на префикс/суффикс — что дешевле с учётом риска снять нужный мод",
                    "whittle": "чистка Chaos + Omen of Whittling"}[v["fix"]])
    if v.get("restart"):
        out.append(f"мусор, пока нужных модов меньше {v['restart']}, — новая база вместо чистки")
    if v.get("pbone"):
        out.append("мод под замену крафт-модом — костью: нужный вариант остаётся")
    if v.get("ftrim"):
        out.append("перед Chaos-спамом первого мода — Annul редкого до одного мода")
    if v.get("crepair"):
        out.append("недостающий мод, когда из нужного на предмете только крафт-моды, — Chaos Orb (крафт-мод потом заново)")
    if "cslam" in v:
        out.append(f"после фрактура, пока других нужных модов нет, — " + ("Annul до одного мусорного мода, потом " if v.get("ctrim") else "") +
                   f"{CUR['chaos'][v['cslam']]} до " +
                   ("самого редкого из недостающих (остальные потом Exalted)" if v.get("chard") else "первого из недостающих"))
    if v.get("dslam"):
        out.append(f"самый редкий недостающий мод — {v['bone']} костью (выбор 1 из 3" + (", с Echoes ещё 3" if v["echoes"] else "") +
                   ("; мимо — Annul + Omen of Light и кость снова)" if v["dslam"] == "light" else "; мимо — дальше Exalted)"))
    if v.get("wfix"):
        out.append("крафт-моду негде встать без риска — сначала Chaos + Omen of Whittling снимает мусор")
    if any(x["src"] in ("essence", "alloy") for x in t):
        out.append({"first": "крафт-моды сразу", "junk": "крафт-мод вместо промаха слэма",
                    "gamble": "Exalted туда (префикс или суффикс), где нужных модов нет; промах заменить крафт-модом",
                    "last": "крафт-моды в конце",
                    "pre": "сначала крафт-моды на место дешёвого мусора, потом остальное",
                    "abyss": "крафт-мод стороны с нужным модом — после него (цикл Essence of the Abyss), перед ним один мусорный под замену"}[v.get("cw", "first")])
    if any(x["src"] == "desecrated" for x in t):
        out.append(f"{v['bone']} кость" + (" + Echoes" if v["echoes"] else "") + (", неудачу снимать Essence of the Abyss + Crystallisation (Mark заменит следующая кость)" if v["light"] == "abyss" else ", неудачу снимать Omen of Light" if v["light"] else ""))
        lord = next((x.get("lord") for x in t if x["src"] == "desecrated" and x.get("lord")), None)
        if lord == "altered":
            out.append("Altered Collarbone: одна из 3 опций — Otherworldly (допущение)")
        elif lord and ctx.lord_omen:
            out.append(LORD_OMEN[lord])
    return "; ".join(out)


def faccs(ctx, fi):
    """first-phase acceptance variants worth trying for first mod fi: only it, mods at least as hard, any pool slot
    (variants that give the same set are tried once)"""
    if fi is None or fi == "any":
        return ("hard",)
    s = Sim(ctx, {"first": fi, "facc": "hard"}, random.Random(0))
    out, seen = [], set()
    for acc in ("only", "hard", "swap", "pool"):
        key = frozenset((F["side"], F["group"]) for F in s.first_set(fi, acc))
        if key not in seen:
            seen.add(key); out.append(acc)
    return tuple(out)


def plan(ctx, budget=1.0, fill=False, progress=None, do_final=True, template=None, ref=math.inf):
    """coordinate search over strategy parameters; returns best result + alternatives. template: the quick screen's
    best strategy for this split: the early stages use its later part (slams, cleanup) instead of a fixed preset that may
    hardly ever finish on a tight base (then their comparison is noise and needs a slow rerun)"""
    try:
        return _plan(ctx, budget, fill, progress, do_final, template, ref)
    finally:
        ctx.cap = CAP
        ctx.cost_cap, ctx.ref_best = math.inf, math.inf  # exact runs and the step-by-step mode: no cut-off


# -- rules that drop variants known to lose before simulating them (speed at the cost of a few % of price)
def _echo_opts(ctx):
    """Abyssal Echoes only adds options (3 more) for a small price: with a cheap omen it is always on"""
    return (True,) if ctx.price(ECHOES) < 0.5 else (False, True)


def _abyss_ok(ctx):
    """the Essence of the Abyss cycle needs a free crafted place for the Mark (or Astrid's Creativity not used yet)"""
    ncr = sum(1 for t in ctx.targets if t["src"] in ("essence", "alloy"))
    return ncr < ctx.max_c or (RUNE["astrid"] not in ctx.runes_once and "astrid" not in getattr(ctx, "runes_have", ()))


def _cost_ok(costs, k=2.5):
    """the options whose expected cost per hit is within k x the cheapest (all of them when none can be estimated)"""
    fin = [c for c in costs.values() if math.isfinite(c)]
    if not fin:
        return tuple(costs)
    lo = min(fin)
    return tuple(q for q, c in costs.items() if c <= k * lo)


def _slam_grades(ctx):
    """Exalted grades worth simulating: price per hit of the hardest natural mod at that grade's tier floor"""
    nat = [t for t in ctx.targets if t["src"] == "natural" and ctx.pool.g[t["side"]].get(t["group"])]
    if not nat:
        return ("", "Greater", "Perfect")
    costs = {}
    for q in ("", "Greater", "Perfect"):
        f = FLOOR["exalt"][q]
        p = min(ctx.pool.W(t["group"], t["side"], f, t["minLevel"]) / max(ctx.pool.T(t["side"], f), 1e-9) for t in nat)
        costs[q] = ctx.price(CUR["exalt"][q]) / p if p > 0 else math.inf
    return _cost_ok(costs)


def _chaos_grades(ctx, allowed=("", "Greater", "Perfect")):
    """Chaos Orb grades worth simulating, as for Exalted (price per hit of the hardest natural mod)"""
    nat = [t for t in ctx.targets if t["src"] == "natural" and ctx.pool.g[t["side"]].get(t["group"])]
    if not nat:
        return allowed
    costs = {}
    for q in allowed:
        f = FLOOR["chaos"][q]
        p = min(ctx.pool.W(t["group"], t["side"], f, t["minLevel"]) / max(ctx.pool.T(t["side"], f), 1e-9) for t in nat)
        costs[q] = ctx.price(CUR["chaos"][q]) / p if p > 0 else math.inf
    return _cost_ok(costs)


def _bone_grades(ctx, ts):
    """bone qualities worth simulating for these mods: (bone + Echoes) per reveal and a miss taken off by Annul + Light"""
    ts = [t for t in ts if ctx.dpool.g[t["side"]].get(t["group"])]
    if not ts:
        return ("Preserved", "Ancient")
    undo = ctx.price(ANNUL) + ctx.price(LIGHT)
    costs = {}
    for b in ("Preserved", "Ancient"):
        f = ANCIENT_BONE_FLOOR if b == "Ancient" else 0
        p = min(1 - (1 - ctx.dpool.W(t["group"], t["side"], f, t["minLevel"]) / max(ctx.dpool.T(t["side"], f), 1e-9)) ** 6
                for t in ts)
        c = ctx.price(f"{b} {bone_kind(ctx.cls)}") + ctx.price(ECHOES)
        costs[b] = (c + (1 / p - 1) * (c + undo)) if p > 0 else math.inf
    return _cost_ok(costs)


def _plan(ctx, budget=1.0, fill=False, progress=None, do_final=True, template=None, ref=math.inf):
    n1 = n2 = max(250, int(700 * budget))    # screening runs per variant (250 instead of 350: same picks on the bench)
    ctx.ref_best = ref  # the quick screen's score of this split: the screening cut-off starts from it
    nr, nf = 2000, 20000                        # the best few again, then the chosen one
    has_des = any(t["src"] == "desecrated" for t in ctx.targets)
    base = {"first": None, "fget": "", "fmode": "none", "slam": "Perfect", "greater": True, "fix": "whittle", "cw": "last",
            "echoes": True, "bone": "Preserved", "light": True, "fill": False}
    if template:
        base.update({k: template[k] for k in ("slam", "greater", "fix", "cw", "echoes", "bone", "light") if k in template})
    firsts = []
    if ctx.frac is not None and not (ctx.start and any(m.t == ctx.frac["i"] and m.fr for m in ctx.start.mods)):
        firsts = [ctx.frac["i"]]
    elif not ctx.start or ctx.start.rar < 2:
        # hardest natural target on the magic stage (like buying a magic +5 base)
        have = {m.t for m in ctx.start.mods} if ctx.start else set()
        nat = [t for t in ctx.targets if t["src"] == "natural" and ctx.pool.g[t["side"]].get(t["group"]) and t["i"] not in have
               and not t.get("opt")]
        if nat:
            def p_slam(t):
                f = FLOOR["exalt"]["Perfect"]
                return ctx.pool.W(t["group"], t["side"], f, t["minLevel"]) / max(ctx.pool.T(t["side"], f), 1e-9)
            firsts = [min(nat, key=p_slam)["i"], None]
            req_nat = [t for t in nat if t["i"] not in ctx.pool_slots]
            if req_nat and min(req_nat, key=p_slam)["i"] != firsts[0]:
                firsts.insert(1, min(req_nat, key=p_slam)["i"])  # the hardest required mod (not one of the pool)
    if ctx.frac is None and len(ctx.anyset) >= 2 and (not ctx.start or ctx.start.rar < 2):
        firsts = (firsts or [None]) + ["any"]  # wait for any wanted mod (e.g. any T1 resistance) instead of one particular
    tried = []
    first_alts = []  # the runner-up ways to get the first mod: re-checked at the end with the leader's later settings
    # stage A: how to get the first target (and fracture it)
    best_first = {}
    for fi in firsts:
        if fi is None:
            continue
        Fs = ctx.anyset if fi == "any" else [ctx.targets[fi]]
        vs = []
        for fget in ("", "Greater", "Perfect", "annul", "annul:Greater", "annul:Perfect", "chaos", "chaos:Greater", "chaos:Perfect"):
            fl = FLOOR["chaos"][fget[6:]] if fget.startswith("chaos") else FLOOR["transmute"][fget.partition(":")[2] if fget.startswith("annul") else fget]
            if sum(ctx.pool.W(F["group"], F["side"], fl, F["minLevel"]) for F in Fs) <= 0:
                continue
            for fmode in (("bone", "four", "none") if fi == "any" else ("bone", "four") if Fs[0]["frac"] else ("none",)):
                for facc in faccs(ctx, fi):
                    # keep a magic item with two wanted mods (new bases only, not on the Annul / Chaos routes)
                    for keep in ((1, 2, 3) if fi != "any" and fget in ("", "Greater", "Perfect") else (1,)):
                        vs.append(dict(base, first=fi, fget=fget, fmode=fmode, facc=facc, keep=keep))
        opts = sim_many(ctx, vs, n1, 7, True, progress, "Как получить первый мод")
        # the cheapest way per acceptance rule; the rules themselves differ in what they leave for later, so they
        # are compared on the whole craft below, not on the first phase alone
        per = {}
        for r in opts:
            k = (r["v"].get("facc"), r["v"].get("keep"))
            if k not in per or fscore(r) < fscore(per[k]):
                per[k] = r
        if per:
            best_first[fi] = [r["v"] for r in per.values()]
    post_frac = any(t["src"] == "natural" and t["i"] != (ctx.frac or {}).get("i") for t in ctx.targets) and \
        (ctx.frac is not None or bool(ctx.start and any(m.fr for m in ctx.start.mods)))
    if any(len(x) > 1 for x in best_first.values()):
        # each rule with and without Chaos after the fracture: on a tight base the craft barely finishes without it,
        # and a comparison of crafts that do not finish is a lottery
        # and a magic item with two wanted mods (no junk) is worth a new base on the first miss rather than a cleanup
        # that risks them ("restart"): checked here together, else the rule only ever meets the routes chosen without it
        vs = [dict(v, **ex) for x in best_first.values() for v in x
              for ex in ([{}, {"cslam": ""}] if post_frac else [{}]) +
              ([{"restart": 2}, {"restart": 3}] if v.get("fmode") == "none" and v.get("keep") in (2, 3) and not ctx.start else [])]
        rs = sim_many(ctx, vs, n2, 11, False, progress, "Что принимать на первом шаге")
        first_alts = [{k: r["v"][k] for k in FIRST_KEYS if k in r["v"]} for r in sorted(rs, key=score)[:3]
                      if math.isfinite(score(r))]
        for fi in best_first:
            mine = [r for r in rs if r["v"]["first"] == fi]
            best_first[fi] = min(mine, key=score)["v"] if mine else best_first[fi][0]
    else:
        best_first = {fi: x[0] for fi, x in best_first.items()}
    if post_frac:
        # Chaos after the fracture decided early: it changes what the rest of the craft looks like
        for fi in list(best_first):
            bv = best_first[fi]
            rs = sim_many(ctx, [{k: x for k, x in bv.items() if k != "cslam"}] +
                          [dict(bv, cslam=cq, fix=fx, ctrim=tr) for cq in _chaos_grades(ctx, ("", "Greater")) for fx in ("plain", "value")
                           for tr in (False, True)],
                          n2, 11, False, progress, "Chaos после фрактура")
            best_first[fi] = min(rs, key=score)["v"]
    # stage B: slam / fix choices with the full craft
    cws = ("first", "junk", "gamble", "last", "pre") if any(t["src"] in ("essence", "alloy") for t in ctx.targets) else ("first",)
    vs = []
    for fi in (firsts or [None]):
        bv = best_first.get(fi, base) if fi is not None else dict(base, first=None)
        for slam in _slam_grades(ctx):
            for greater in (False, True):
                vs.append(dict(bv, slam=slam, greater=greater, fix=bv.get("fix", "whittle") if "cslam" in bv or template else "whittle",
                               cw=bv.get("cw", "last") if template else "last"))
    res = sim_many(ctx, vs, n2, 11, False, progress, "Какой Exalted")
    res.sort(key=score)
    b1 = res[0]["v"]
    vs = [dict(b1, fix=fix, cw=cw) for fix in ("plain", "omen", "value", "whittle") for cw in cws
          if not (fix == b1["fix"] and cw == b1["cw"])]
    res += sim_many(ctx, vs, n2, 11, False, progress, "Чистка и крафт-моды")
    res.sort(key=score)
    b2 = res[0]["v"]
    if post_frac:
        # after the fracture: Chaos spam for the next wanted mod instead of Exalted + Annul cycles
        # with the cleanup that fits it: a plain Annul on a tight item keeps taking the mod the Chaos found
        vs = [dict(b2, cslam=cq, fix=fx, chard=hd, ctrim=tr) for cq in _chaos_grades(ctx) for fx in ("plain", "value")
              for hd in (False, True) for tr in (False, True)]
        res += sim_many(ctx, vs, n2, 11, False, progress, "Chaos после фрактура")
        res.sort(key=score)
        b2 = res[0]["v"]
    if any(e for t in ctx.targets for e in t.get("ess_all") or [] if not e.get("magic")):
        # slam a mod an essence also gives, or switch to the essence when slamming gets dear: both on whole crafts
        res += sim_many(ctx, [dict(b2, nodyn=True)], n2, 11, False, progress, "Эссенция вместо Exalted")
        res.sort(key=score)
        b2 = res[0]["v"]
    if any(t["src"] in ("essence", "alloy") for t in ctx.targets):
        # a crafted mod with nowhere safe to go: gamble, or free a slot with Chaos + Omen of Whittling first
        res += sim_many(ctx, [dict(b2, wfix=True)], n2, 11, False, progress, "Эссенции: риск или Whittling")
        res.sort(key=score)
        b2 = res[0]["v"]
    if any(t["src"] == "natural" for t in ctx.targets):
        # a natural mod lost again (an essence, a cleanup took it) while only crafted mods are left: Chaos spam for it
        res += sim_many(ctx, [dict(b2, crepair=True)], n2, 11, False, progress, "Chaos вместо Annul + Exalted")
        res.sort(key=score)
        b2 = res[0]["v"]
    if any(t["src"] in ("essence", "alloy") for t in ctx.targets) and any(t["src"] == "natural" for t in ctx.targets):
        # the slot a crafted mod will replace: a bone there (a wanted option stays, junk is replaced anyway)
        res += sim_many(ctx, [dict(b2, pbone=not b2.get("pbone"))], n2, 11, False, progress, "Кость под замену")
        res.sort(key=score)
        b2 = res[0]["v"]
    if str(b2.get("fget", "")).startswith("chaos") and not ctx.start:
        # Chaos spam for the first mod on a rare Annulled down to one mod: it lands on an almost empty item
        res += sim_many(ctx, [dict(b2, ftrim=True)], n2, 11, False, progress, "Chaos на почти пустом редком")
        res.sort(key=score)
        b2 = res[0]["v"]
    if not ctx.start:
        # junk early on: give the base up instead of cleaning it, while it carries fewer than k wanted mods
        vs = [dict(b2, restart=k) for k in range(1, min(len(ctx.targets), 5) + 1)]
        res += sim_many(ctx, vs, n2, 11, False, progress, "Бросать базу или чистить")
        res.sort(key=score)
        b2 = res[0]["v"]
    if (b2["fix"], b2["cw"]) != (b1["fix"], b1["cw"]):  # the best Exalt may differ with the new cleanup
        vs = [dict(b2, slam=slam, greater=greater) for slam in ("", "Greater", "Perfect") for greater in (False, True)
              if (slam, greater) != (b2["slam"], b2["greater"])]
        res += sim_many(ctx, vs, n2, 11, False, progress, "Какой Exalted (уточнение)")
        res.sort(key=score)
    best = res[0]["v"]
    # stage C: desecration choices
    if has_des:
        vs = [dict(best, bone=bone, echoes=echoes, light=light)
              for bone in _bone_grades(ctx, [t for t in ctx.targets if t["src"] == "desecrated"])
              for echoes in _echo_opts(ctx) for light in ((False, True, "abyss") if _abyss_ok(ctx) else (False, True))]
        res2 = sim_many(ctx, vs, n2, 13, False, progress, "Десекрейт")
        res2.sort(key=score)
        best = res2[0]["v"]
        res = res2 + res
    if any(t["src"] == "natural" for t in ctx.targets):
        # no mod planned for desecration: the free desecrated slot may add a missing natural mod (1 of 3) instead of Exalted
        crafted_ = any(t["src"] in ("essence", "alloy") for t in ctx.targets)
        vs = [dict(best, dslam=d, bone=bone, echoes=echoes, light=lt, **({"cw": cw} if cw else {}))
              for d in ("once", "light") for bone in _bone_grades(ctx, [t for t in ctx.targets if t["src"] == "natural"])
              for echoes in _echo_opts(ctx)
              for cw in ((None,) + tuple(x for x in ("pre", "abyss") if x != best.get("cw")) if crafted_ else (None,))
              for lt in ((True, "abyss") if d == "light" and _abyss_ok(ctx) else
                         (best["light"] if best["light"] == "abyss" else True,))]
        res2 = sim_many(ctx, vs, n2, 13, False, progress, "Кость вместо Exalted")
        res = sorted(res2 + res, key=score)
        best = res[0]["v"]
    # the few best candidates again on a bigger sample: screening runs are small and costs are heavy-tailed. The best
    # score so far stays the cut-off reference (a variant whose median craft costs 2.5x it stops early), and only
    # variants within 2x of the leader go there at all
    ctx.cost_cap = math.inf
    top, seen_d = [], set()
    lead = min((score(r) for r in res), default=math.inf)
    for r in sorted(res, key=score):
        d = describe(r["v"], ctx)
        if d in seen_d or not math.isfinite(r["mean"]) or score(r) > 2 * lead:
            continue
        seen_d.add(d); top.append(r["v"])
        if len(top) >= 5:
            break
    if best not in top:
        top.insert(0, best)
    # the first-phase choice was made on a small sample, before the later stages tuned the rest (Chaos repair, Annul to
    # one mod...), which can suit a runner-up better: the leader with each runner-up's first phase goes to the big sample
    for fa in first_alts:
        x = {k: y for k, y in best.items() if k not in FIRST_KEYS}
        x.update(fa)
        if x not in top:
            top.append(x)
    # cheap safety switches decided on small samples early on: the leader with them flipped goes to the big sample too
    for k, ok in (("ftrim", str(best.get("fget", "")).startswith("chaos") and not ctx.start),
                  ("crepair", any(t["src"] == "natural" for t in ctx.targets)),
                  ("pbone", any(t["src"] in ("essence", "alloy") for t in ctx.targets))):
        x = dict(best, **{k: not best.get(k)})
        if ok and x not in top:
            top.append(x)
    if len(top) > 1:
        # two passes: a third of the runs for all, the rest only for those within 2x of the best (costs are heavy-tailed:
        # a tighter cut after fewer runs dropped the true best now and then)
        res = sim_many(ctx, top, nr // 3, 17, False, progress, "Уточняю лучшие", cap=CAP * 3)
        lead = min(score(r) for r in res)
        keep = [i for i, r in enumerate(res) if math.isfinite(score(r)) and score(r) <= 2 * lead]
        if len(keep) > 1:
            more = sim_many(ctx, [res[i]["v"] for i in keep], nr - nr // 3, 18, False, progress, "Уточняю лучшие", cap=CAP * 3)
            for i, r in zip(keep, more):
                res[i] = merge([res[i], r])
    ctx.cap = CAP
    res.sort(key=score)
    best = res[0]["v"]
    if do_final:
        if progress:
            progress("Точный прогон лучшей стратегии")
        final = sim_final(ctx, dict(best, fill=fill), nf, progress=progress)
    else:
        final = dict(res[0], v=dict(best, fill=fill))
    seen, alts = {describe(best, ctx)}, []
    seen_num = set()
    for r in res:
        d = describe(r["v"], ctx)
        num = (round(r["mean"], 6), round(r["ok"], 6))
        if d in seen or r["v"] == best or num in seen_num or not math.isfinite(r["mean"]):
            continue
        seen_num.add(num)
        seen.add(d)
        alts.append({"desc": d, "mean": score(r), "ok": r["ok"]})
        if len(alts) >= 6:
            break
    return final, alts


_CRAFT_STEP = re.compile(r"^(Крафт-мод «.*?»: .+?)( \+ Omen of \S+ Crystallisation)?( \(.*\))$")


def merge_variants(agg, n):
    """one crafted mod is one action in the guide: its variants (with or without an omen, junk or a random mod replaced)
    become one step — the usual one first, the rarer ones as 'in ~N% of crafts: ...'. Step mode still says which case it is"""
    groups = {}
    for k in agg:
        m = _CRAFT_STEP.match(k)
        if m:
            groups.setdefault(m.group(1), []).append((k, m.group(2) or "", m.group(3)))
    out = dict(agg)
    for base, ks in groups.items():
        if len(ks) < 2:
            continue
        ks.sort(key=lambda x: -agg[x[0]][5])
        main = ks[0][0]
        extra = []
        for k, om, note in ks[1:]:
            share = max(1, round(100 * agg[k][5] / max(n, 1)))
            extra.append(f"в ≈{share}% крафтов: " + (om.strip(" +") if om else "без омена") + ", " + note.strip(" ()"))
        merged = [0.0, {}, 0, 0, 0.0, 0]
        for k, _, _ in ks:
            a = out.pop(k)
            for i in (0, 2, 3, 4, 5):
                merged[i] += a[i]
            for nm, x in a[1].items():
                merged[1][nm] = merged[1].get(nm, 0) + x
        out[main + "; " + "; ".join(extra)] = merged
    return out


def summarize(final, ctx):
    n = max(len(final["costs"]), 1)
    final = dict(final, agg=merge_variants(final["agg"], n))
    steps = []
    for k in sorted(final["agg"], key=lambda k: (k.startswith("По желанию"), final["agg"][k][4] / max(final["agg"][k][5], 1))):
        a = final["agg"][k]
        if a[5] < 0.05 * n:
            continue  # a branch seen in under 5% of crafts (still counted in the totals)
        steps.append({"step": k, "cost": a[0] / n, "tries": a[2] / n, "rate": (a[3] / a[2]) if a[2] else None,
                      "uses": sorted(({"name": nm, "n": x / n, "price": ctx.price(nm) if nm not in BASES else ctx.base_price}
                                      for nm, x in a[1].items()), key=lambda u: -u["n"] * u["price"]),
                      "optional": k.startswith("По желанию"), "share": min(1.0, a[5] / n)})
    buy = {}
    for k, a in final["agg"].items():
        if k.startswith("По желанию"):
            continue
        for nm, x in a[1].items():
            b = buy.setdefault(nm, {"name": nm, "n": 0.0, "price": ctx.price(nm) if nm not in BASES else ctx.base_price})
            b["n"] += x / n
    c = final["costs"]
    pct = lambda q: c[min(len(c) - 1, int(q * len(c)))] if c else None
    se = None
    if len(c) > 1:
        m = sum(c) / len(c)
        se = 1.96 * math.sqrt(sum((x - m) ** 2 for x in c) / (len(c) - 1) / len(c))
    return {"mean": final["mean"] if c else None, "median": pct(0.5), "p90": pct(0.9), "ok": final["ok"], "ci": se, "runs": len(c),
            "pct": [pct(i / 20) for i in range(20)] + ([c[-1]] if c else []),  # cost at 0%, 5% ... 100% of crafts
            "desc": describe(final["v"], ctx), "steps": steps, "alias": guide_alias(ctx),
            "buy": sorted(buy.values(), key=lambda b: -b["n"] * b["price"])}


def guide_alias(c):
    """a pool goal is planned as a concrete mod, but any other mod of the pool on that side fills its slot just as well
    (pool_alias): the guide lists them next to the planned one. nat = can roll from Exalted/Chaos, des = from a bone"""
    out = {}
    for t in c.targets:
        al = c.alias_of.get(t["i"]) or []
        if not al:
            continue
        uniq = lambda xs: list(dict.fromkeys(xs))
        out[t["text"]] = {"nat": uniq(a["text"] for a in al if c.pool.g[a["side"]].get(a["group"])),
                          "des": uniq(a["text"] for a in al if c.dpool.g[a["side"]].get(a["group"]))}
    return out


class Problem:
    """the wanted mods on a base; which of them is fractured, desecrated or made by an essence is decided by plan_auto"""

    def __init__(self, cd, targets, prices, ilvl, base_price=0.02, start_item=None, runes_have=(), need_opt=0, open_affix=(0, 0)):
        self.cd, self.prices, self.ilvl, self.base_price = cd, prices, ilvl, base_price
        # groups like the trade site's "count" filters: opt = group number (True = group 1); need = {group: how many}
        gid = lambda o: 0 if not o else 1 if o is True else int(o)
        groups = sorted({gid(t.get("opt")) for t in targets} - {0})
        if isinstance(need_opt, dict):
            self.need_by = {g: int(need_opt.get(str(g), need_opt.get(g, 0)) or 0) for g in groups}
        else:
            self.need_by = {g: int(need_opt or 0) for g in groups}
        self.multi = len(groups) > 1
        self.need_opt = sum(self.need_by.values()) if groups else 0
        self.raw, self.start_item, self.pool_pick = targets, start_item, None
        ad = cd.get("affixDelta") or {}  # "-1 Prefix Modifier allowed" (Absent Amulet: 2 + 2)
        # "at least N empty prefixes / suffixes" (the trade site's Empty Prefix Modifiers): fewer slots for the craft to use
        self.open = (max(0, int(open_affix[0] or 0)), max(0, int(open_affix[1] or 0)))
        self.capP, self.capS = 3 + int(ad.get("P") or 0) - self.open[0], 3 + int(ad.get("S") or 0) - self.open[1]
        self.runes_have = set(runes_have or ())
        self.runes = {k: True for k in self.runes_have}
        self.notes = []
        if not targets:
            raise ValueError("Не выбрано ни одного мода")
        wfn = weigher(cd)
        plain = Pool(cd["pool"], ilvl, wfn)
        mk = cd.get("marksmanPool") or []
        withm = Pool(cd["pool"] + mk, ilvl, wfn)
        T, errs = [], []
        for i, t in enumerate(targets):
            x = {"i": i, "side": t["side"], "group": t["group"], "minLevel": int(t.get("minLevel") or 0),
                 "text": t.get("text") or t["group"], "fixed": None, "name": None, "magic": False, "lord": t.get("lord"), "ess": [],
                 "opt": bool(t.get("opt")), "grp": gid(t.get("opt"))}
            if x["minLevel"] > ilvl:
                errs.append(f"«{x['text']}» требует уровень предмета {x['minLevel']}, а у базы {ilvl}")
            if t["kind"] == "alloy":
                x.update(fixed="alloy", name=t["name"])
            elif t.get("desOnly") or t.get("lord"):
                x.update(fixed="desecrated")
            elif t["kind"] == "essence" and withm.W(x["group"], x["side"], 0, x["minLevel"]) <= 0:
                x.update(fixed="essence", name=t["name"], magic=bool(t.get("magic")))
            if x["fixed"] is None:
                if plain.W(x["group"], x["side"], 0, x["minLevel"]) <= 0 and withm.W(x["group"], x["side"], 0, x["minLevel"]) > 0:
                    self.runes["kolr"] = True
                    x["mk"] = True  # rolls only with Kolr's Hunt socketed
                for e in cd["essences"]:
                    if e.get("side") == x["side"] and e["group"] == x["group"] and e["level"] >= x["minLevel"] and e["level"] <= ilvl:
                        x["ess"].append({"name": e["name"], "magic": not is_rare_essence(e["name"]), "level": e["level"]})
                x["ess"].sort(key=lambda e: e["level"])
            T.append(x)
        req = [t for t in T if not t["opt"]]
        nP = sum(1 for t in req if t["side"] == "P")
        nS = sum(1 for t in req if t["side"] == "S")
        pool = [t for t in T if t["opt"]]
        if pool:
            if any(t["fixed"] in ("alloy", "essence") or t.get("lord") for t in pool):
                errs.append("В пул можно класть только моды, которые выпадают сами (слэмом или десекрейтом), без Alloy и эссенций")
            bad = [g for g in self.need_by if not 1 <= self.need_by[g] <= sum(1 for t in pool if t["grp"] == g)]
            if bad:
                errs.append("; ".join(f"Группа {g}: нужно от 1 до {sum(1 for t in pool if t['grp'] == g)} модов" for g in bad))
            elif (self.capP - nP) + (self.capS + 1 if nS > self.capS else self.capS) - nS < self.need_opt:
                errs.append(f"Обязательные моды занимают почти все слоты: для {self.need_opt} модов из пула места нет")
        if nP > self.capP:
            errs.append(f"Префиксов {nP}, а на этой базе их максимум {self.capP}")
        if nS > self.capS + 1:
            errs.append(f"Суффиксов {nS}: даже с Serle's Triumph на этой базе максимум {self.capS + 1}")
        if sum(1 for t in T if t["fixed"] in ("alloy", "essence")) > 2:
            errs.append("Крафт-модов больше двух: максимум 2 (второй даёт Astrid's Creativity)")
        if sum(1 for t in T if t["fixed"] == "desecrated") > 1:
            names = ", ".join(f"«{t['text']}»" for t in T if t["fixed"] == "desecrated")
            errs.append(f"Модов только из десекрейта больше одного ({names}), а десекрейт-мод на предмете один. "
                        "Если такой мод есть у эссенции (в списке: «Perfect Essence of …: мод»), возьмите его вариант эссенции")
        if errs:
            raise ValueError("; ".join(errs))
        if nS > self.capS:
            self.runes["serle"] = True
        mods = cd["pool"] + (mk if self.runes.get("kolr") else [])
        self.pool = Pool(mods, ilvl, wfn)
        lord_mods = cd.get("desPool") or []
        self.dpool = Pool(mods + lord_mods, ilvl, wfn)
        self.lords = {l: Pool([m for m in lord_mods if m.get("lord") == l], ilvl, wfn) for l in list(LORD_OMEN) + ["altered"]}
        for t in T:
            t["slam"] = t["fixed"] is None and self.pool.W(t["group"], t["side"], 0, t["minLevel"]) > 0
            t["des"] = t["fixed"] in (None, "desecrated") and self.dpool.W(t["group"], t["side"], 0, t["minLevel"]) > 0
            if t["fixed"] is None and not t["slam"] and not t["des"] and not t["ess"]:
                raise ValueError(f"«{t['text']}» не выпадает на этой базе")
            if t["opt"] and not t["slam"]:
                raise ValueError(f"«{t['text']}» не выпадает от Exalted — в пул нельзя, сделайте его обязательным")
            if t["fixed"] is None and not t["slam"] and not t["ess"]:
                t["fixed"] = "desecrated"
            if t["fixed"] == "desecrated" and not t["des"]:
                raise ValueError(f"«{t['text']}» не выпадает при десекрейте на этой базе")
        self.T = T
        self.start = None
        self.start_frac = False
        self.free_sockets = 0
        if start_item and start_item.get("spec"):
            self._start_spec(start_item)
        elif start_item:
            self._start(start_item)
            self.free_sockets = max(0, int(start_item.get("sockets") or 0) - len(start_item.get("runes") or []))

    def _start_spec(self, spec):
        """the player's item entered by hand: rarity, sockets, mods (a known mod with its tier, or plain junk)"""
        rar = {"NORMAL": 0, "MAGIC": 1, "RARE": 2}.get(spec.get("rarity"), 2)
        self.start = It([], rar)
        taken = set()
        for x in spec.get("mods") or []:
            s = x.get("side")
            if s not in ("P", "S"):
                continue
            how = x.get("how") or ""
            cr = x.get("kind") in ("alloy", "essence") or how == "cr"
            m = M(s, None if x.get("junk") else x.get("group"), -1, fr=1 if how == "fr" else 0, cr=1 if cr else 0,
                  de=1 if how == "de" else 0, lv=int(x.get("level") or 0))
            if m.g is not None:
                for t in self.T:
                    if t["i"] in taken or t["side"] != s or t["group"] != m.g or m.lv < t["minLevel"]:
                        continue
                    if t["fixed"] == "alloy" and not m.cr:
                        continue
                    m.t = t["i"]; taken.add(t["i"])
                    break
            self.start.mods.append(m)
        for x in "PS":
            cap = {0: 0, 1: 1, 2: self.capP if x == "P" else self.capS}[rar]
            n = sum(1 for m in self.start.mods if m.s == x)
            if n > cap + (1 if x == "S" and rar == 2 and "serle" in self.runes_have else 0):
                raise ValueError(f"На {['обычном', 'магическом', 'редком'][rar]} предмете не может быть {n} {SIDE_RU[x]}ов")
        if sum(1 for m in self.start.mods if m.fr) > 1:
            raise ValueError("Фрактур-мод на предмете может быть только один")
        if sum(1 for m in self.start.mods if m.de) > 1:
            raise ValueError("Десекрейт-мод на предмете может быть только один")
        self.start_frac = any(m.fr for m in self.start.mods)
        self.have = taken
        self.free_sockets = max(0, int(spec.get("sockets") or 0) - len(spec.get("runes") or []))

    def _start(self, start_item):
        cd, ilvl = self.cd, self.ilvl
        rar = {"NORMAL": 0, "MAGIC": 1, "RARE": 2}.get(start_item.get("rarity"), 2)
        self.start = It([], rar)
        nat, ess, alloys = catalog(cd, ilvl, self.runes.get("kolr"))
        lines = start_item["mods"]
        tiers = [dict(x, side=f["side"], group=f["group"], lines=x["text"].split(" / ")) for f in nat for x in f["tiers"]]
        quality_scale(start_item, tiers); quality_scale(start_item, ess + alloys)
        nfound, cfound = split_lines(lines, tiers, ess + alloys)
        rebalance(nfound, cfound, lines, tiers, {"P": self.capP, "S": self.capS})
        taken = set()
        for c, idx in cfound + nfound:
            l0 = lines[idx[0]]
            m = M(c["side"], c["group"], -1, fr=1 if l0.get("fractured") else 0, cr=1 if c.get("kind", "natural") != "natural" else 0,
                  de=1 if l0.get("desecrated") else 0, lv=c["level"])
            for t in self.T:
                if t["i"] in taken or t["side"] != m.s or t["group"] != m.g or c["level"] < t["minLevel"]:
                    continue
                if t["fixed"] == "alloy" and not m.cr:
                    continue
                m.t = t["i"]; taken.add(t["i"])
                break
            self.start.mods.append(m)
        self.start_frac = any(m.fr for m in self.start.mods)
        self.have = taken

    def p_slam(self, t):
        f = FLOOR["exalt"]["Perfect"]
        return self.pool.W(t["group"], t["side"], f, t["minLevel"]) / max(self.pool.T(t["side"], f), 1e-9)

    def p_reveal(self, t):
        w = self.dpool.W(t["group"], t["side"], 0, t["minLevel"]) / max(self.dpool.T(t["side"], 0), 1e-9)
        return 1 - (1 - w) ** 6  # 3 options, Abyssal Echoes rerolls all 3

    def subsets(self, limit=2):
        """a pool goal is met by any concrete pick from it: the easiest picks become ordinary goals, so they get
        fracture / desecration / essence options too and the pool can never come out dearer than a good pick"""
        pool = [t for t in self.T if t["opt"]]
        if not pool or not self.need_opt:
            return []
        if self.multi:
            return self._subsets_multi(pool, limit)
        req = [t for t in self.T if not t["opt"]]
        cap = {"P": self.capP - sum(1 for t in req if t["side"] == "P"),
               "S": (self.capS + 1 if self.runes.get("serle") else self.capS) - sum(1 for t in req if t["side"] == "S")}

        def ease(t):
            return max(self.p_slam(t) if t["slam"] else 0, self.p_reveal(t) if t["des"] else 0, 1e-9)
        # the rest of the pool on the same side fills a picked slot anyway (pool_alias), so what really tells picks apart
        # is how many prefixes and suffixes they take: the easiest pick of every split (2 + 2, 1 + 3, ...) is tried.
        # Rounded keys: equal weights (Fire / Cold / Lightning) must not be ordered by float noise
        best = {}
        for comb in itertools.combinations(pool, self.need_opt):
            if any(sum(1 for t in comb if t["side"] == s) > cap[s] for s in "PS"):
                continue
            key = (round(-sum(math.log(ease(t)) for t in comb), 6), tuple(t["i"] for t in comb))
            split = sum(1 for t in comb if t["side"] == "P")
            if split not in best or key < best[split][0]:
                best[split] = (key, comb)
        combos = sorted(best.values(), key=lambda x: x[0])
        out = []
        for _, comb in combos[:max(limit, 3)]:
            try:
                p = Problem(self.cd, [dict(self.raw[t["i"]], opt=False) for t in req + list(comb)], self.prices, self.ilvl,
                            self.base_price, self.start_item, self.runes_have, open_affix=self.open)
            except ValueError:
                continue
            p.pool_pick = [t["text"] for t in comb]
            p.pool_eq = [{k: t[k] for k in ("side", "group", "minLevel", "text")} for t in pool]
            p.pool_picked = set(range(len(req), len(req) + len(comb)))
            out.append(p)
        return out

    def _subsets_multi(self, pool, limit):
        """several groups ("any 2 of the resistances" and "crit or rarity"): the easiest picks of every group, combined;
        within a group the rest of it stands in for a pick (pool_alias per group)"""
        req = [t for t in self.T if not t["opt"]]
        cap = {"P": self.capP - sum(1 for t in req if t["side"] == "P"),
               "S": (self.capS + 1 if self.runes.get("serle") else self.capS) - sum(1 for t in req if t["side"] == "S")}

        def ease(t):
            return max(self.p_slam(t) if t["slam"] else 0, self.p_reveal(t) if t["des"] else 0, 1e-9)
        per = []
        for g, k in sorted(self.need_by.items()):
            mem = [t for t in pool if t["grp"] == g]
            best = {}
            for comb in itertools.combinations(mem, k):
                key = (round(-sum(math.log(ease(t)) for t in comb), 6), tuple(t["i"] for t in comb))
                split = sum(1 for t in comb if t["side"] == "P")
                if split not in best or key < best[split][0]:
                    best[split] = (key, comb)
            per.append((g, sorted(best.values(), key=lambda x: x[0])[:3]))
        prods = []
        for pick in itertools.product(*[c for _, c in per]):
            comb = [t for _, cm in pick for t in cm]
            if any(sum(1 for t in comb if t["side"] == s) > cap[s] for s in "PS"):
                continue
            prods.append((sum(k[0] for k, _ in pick), pick))
        prods.sort(key=lambda x: x[0])
        out = []
        for _, pick in prods[:max(limit, 4)]:
            comb = [t for _, cm in pick for t in cm]
            try:
                p = Problem(self.cd, [dict(self.raw[t["i"]], opt=False) for t in req + comb], self.prices, self.ilvl,
                            self.base_price, self.start_item, self.runes_have, open_affix=self.open)
            except ValueError:
                continue
            p.pool_pick = [t["text"] for t in comb]
            p.pool_groups, at = [], len(req)
            for (g, _), (_, cm) in zip(per, pick):
                eq = [{k: t[k] for k in ("side", "group", "minLevel", "text")} for t in pool if t["grp"] == g]
                p.pool_groups.append((eq, set(range(at, at + len(cm)))))
                at += len(cm)
            out.append(p)
        return out

    def assignments(self):
        """candidate splits: which mod is desecrated, which fractured, which made by an essence"""
        have = getattr(self, "have", set())
        free = [t for t in self.T if t["fixed"] is None and t["i"] not in have and not t["opt"]]
        fixed_des = any(t["fixed"] == "desecrated" for t in self.T)
        des_c = [None]
        if not fixed_des:
            cand = [t for t in free if t["des"]]
            cand.sort(key=lambda t: (self.p_slam(t) if t["slam"] else 0) / max(self.p_reveal(t), 1e-12))
            des_c += [t["i"] for t in cand[:4]]
        frac_c = [None]
        if not self.start_frac and not (self.start and self.start.rar == 2):
            # the hardest ones, but a third too: a mod that is easy to slam may be cheap to catch on a magic base
            # (Perfect Transmute: T1 flat damage) and then is the cheapest lock, with an essence or a bone for the rest
            cand = sorted([t for t in free if t["slam"]], key=self.p_slam)
            frac_c += [t["i"] for t in cand[:3]]
        fixed_cr = sum(1 for t in self.T if t["fixed"] in ("alloy", "essence"))
        ess_t = [t for t in free if t["ess"]]
        ess_c = [{}]
        if ess_t and fixed_cr < 2:
            ess_c.append({t["i"]: t["ess"][0] for t in sorted(ess_t, key=self.p_slam)[:2 - fixed_cr]})
        out = []
        for d in des_c:
            for f in frac_c:
                if f is not None and f == d:
                    continue
                for e in ess_c:
                    e2 = {i: x for i, x in e.items() if i not in (d, f)}
                    if e and not e2:
                        continue
                    ok = all(t["fixed"] is not None or t["slam"] or t["i"] in e2 or t["i"] == d or t["i"] in have or t["opt"] for t in self.T)
                    if ok:
                        out.append({"des": d, "frac": f, "ess": e2})
        return out

    def ctx(self, a):
        tg = []
        for t in self.T:
            src, name, magic = t["fixed"] or "natural", t["name"], t["magic"]
            if t["fixed"] is None:
                if t["i"] == a["des"]:
                    src = "desecrated"
                elif t["i"] in a["ess"]:
                    e = a["ess"][t["i"]]
                    src, name, magic = "essence", e["name"], e["magic"]
            minl = t["minLevel"]
            if src == "essence" and t["fixed"] is None:
                minl = a["ess"][t["i"]]["level"]
            tg.append({"i": t["i"], "src": src, "side": t["side"], "group": t["group"], "minLevel": minl, "text": t["text"],
                       "name": name, "frac": t["i"] == a["frac"], "magic": magic, "marksman": False,
                       "lord": t["lord"] if src == "desecrated" else None, "opt": t["opt"], "mk": bool(t.get("mk")),
                       "ess_opts": [e for e in t.get("ess") or [] if e.get("magic")], "ess_all": list(t.get("ess") or [])})
        runes = dict(self.runes)
        n_cr = sum(1 for t in tg if t["src"] in ("essence", "alloy"))
        if n_cr > 1:
            runes["astrid"] = True
        # Kolr's goes into every base only when the fractured mod itself needs it (a missed fracture = new base);
        # otherwise it is socketed once, after the fracture
        runes_once = [RUNE[k] for k in ("serle", "astrid") if runes.get(k) and k not in self.runes_have]
        c = Ctx(self.pool, tg, self.prices, self.ilvl, self.cd["cls"], self.capS + 1 if runes.get("serle") else self.capS,
                CRAFTED_MAX + 1 if runes.get("astrid") else CRAFTED_MAX, self.base_price, self.start, runes_once, [], self.dpool, self.lords)
        c.need_opt = self.need_opt
        c.free_sockets = self.free_sockets
        c.quality = getattr(self, "quality", 0)
        # the catalyst that scales most of the wanted mods (by their tags)
        tagmap = {}
        for m in self.cd["pool"] + (self.cd.get("desPool") or []) + self.cd["essences"] + self.cd["alloys"]:
            tagmap.setdefault(m.get("group"), set()).update(m.get("tags") or [])
        votes = {}
        for t in self.T:
            for tag, name in CATALYST:
                if tag in tagmap.get(t["group"], ()):
                    votes[name] = votes.get(name, 0) + 1
        c.catalyst = max(votes, key=votes.get) if votes else None
        c.max["P"] = self.capP
        if getattr(self, "pool_eq", None):
            c.pool_alias(self.pool_eq, self.pool_picked)
        for eq, picked in getattr(self, "pool_groups", None) or []:
            c.pool_alias(eq, picked)
        c.kolr = RUNE["kolr"] if runes.get("kolr") and "kolr" not in self.runes_have else None
        c.runes_have = set(self.runes_have)
        d = [f"из пула ловим: {', '.join('«' + x + '»' for x in self.pool_pick)}"] if self.pool_pick else []
        if a["des"] is not None:
            d.append(f"десекрейтом «{self.T[a['des']]['text']}»")
        for i, e in a["ess"].items():
            d.append(f"эссенцией «{self.T[i]['text']}» ({e['name']})")
        c.assign_desc = d
        c.runes_used = {k: bool(runes.get(k)) for k in RUNE}
        return c


def _qtask(a):
    ctx, v, n = a[:3]
    return simulate(ctx, v, n, seed=a[3] if len(a) > 3 else 5)


def plan_auto(prob, budget=0.4, fill=False, progress=None):
    """try the fracture / desecration / essence splits with a few typical strategies, then fully plan the best ones"""
    subs = prob.subsets()
    pairs = [(p, a) for p in ([] if prob.multi else [prob]) + subs for a in p.assignments()]
    if not pairs:
        raise ValueError(f"Цель не помещается в аффиксы базы: доступно {prob.capP} префикса и {prob.capS} суффикса"
                         + (f" (с учётом пустых: {prob.open[0]} префикс., {prob.open[1]} суфф.)" if any(prob.open) else "")
                         + " — уменьшите группы или число пустых аффиксов")
    asg = [a for _, a in pairs]
    base = {"first": None, "fget": "", "fmode": "none", "slam": "Perfect", "greater": True, "fix": "value", "cw": "gamble",
            "echoes": True, "bone": "Preserved", "light": True, "fill": False}
    presets = [dict(base), dict(base, slam="Greater", greater=False, fix="omen", cw="junk"),
               dict(base, slam="", greater=False, fix="plain", cw="first")]
    jobs, owner = [], []
    ctxs = [p.ctx(a) for p, a in pairs]
    for k, (a, c) in enumerate(zip(asg, ctxs)):
        for pv in presets:
            if a["frac"] is not None:
                F = pairs[k][0].T[a["frac"]]
                qs = [q for q in ("Perfect", "Greater", "") if c.pool.W(F["group"], F["side"], FLOOR["transmute"][q], F["minLevel"]) > 0][:1]
                for fget in ["chaos"] + qs + ["annul:" + q for q in qs]:  # new bases each time, or one base + Annul
                    # acceptance rules (facc / keep) are tried later, in the full plan of the finalists
                    jobs.append((c, dict(pv, first=a["frac"], fget=fget, fmode="bone"), 150)); owner.append(k)
            else:
                jobs.append((c, pv, 150)); owner.append(k)
                # no fracture: the hardest required mod first on new magic bases, alone or keeping two wanted mods
                nat_ = [t for t in c.targets if t["src"] == "natural" and not t.get("opt") and c.pool.g[t["side"]].get(t["group"])]
                # the hardest required mod; when every mod comes from a Count group, the hardest of the picked ones
                req = [t for t in nat_ if t["i"] not in c.pool_slots] or nat_
                if req and pv is presets[0] and not (c.start and c.start.rar == 2):
                    F = min(req, key=lambda t: c.pool.W(t["group"], t["side"], 0, t["minLevel"]) / max(c.pool.T(t["side"], 0), 1e-9))
                    for keep, rs in ((1, None), (2, None), (3, None), (2, 3), (3, 2), (3, 3)):
                        jobs.append((c, dict(pv, first=F["i"], fget="Greater", fmode="none", facc="pool" if keep > 1 else "only",
                                             keep=keep, **({"restart": rs} if rs else {})), 150))
                        owner.append(k)


    if progress:
        progress(f"Что фрактурить, что десекрейтить: {len(asg)} вариантов")
    # two passes: 50 crafts per variant, then 100 more only for those within 2x of the best (a variant that is 2x
    # dearer after 50 crafts is not going to win; the survivors get the full sample)
    n_all = len(jobs)
    tick = (lambda i, n_: progress(f"Что фрактурить, что десекрейтить: {i}/{n_all} проверок")) if progress else None
    # 10 crafts each without a cut-off give a reference price; the other 40 cut off crafts that spend over 10x it
    # (hopeless variants used to run every craft to the action limit: ~3/4 of the screen's time)
    def capped(k, fn):
        for c_ in ctxs:
            c_.cost_cap, c_.stop_ref = k, k / 10
        try:
            return fn()
        finally:
            for c_ in ctxs:
                c_.cost_cap, c_.stop_ref = math.inf, math.inf
    # first 3 crafts each; the other 7 only where at least one of the 3 finished — unless no variant finishes at all
    # (a hard goal: then all of them). A variant whose crafts all run to the action limit costs 3 crafts, not 10
    res = _pmap(_qtask, [(c, v, 3, 5) for c, v, _ in jobs], tick, every=10)
    some = any(r["n"] >= 2 for r in res)
    go = [i for i, r in enumerate(res) if r["n"] >= 1 or not some]
    for i, r in zip(go, _pmap(_qtask, [(jobs[i][0], jobs[i][1], 7, 15) for i in go], tick, every=10)):
        res[i] = merge([res[i], r])
    top = min((score(r) for r in res), default=math.inf)
    lim = 10 * top if math.isfinite(top) else math.inf
    # the second 40 only for variants not already out by the early-stop rules (simulate) on their first 10
    def alive(r):
        fc = r["costs"]
        return r["n"] > 3 and not (math.isfinite(top) and r["n"] >= 5 and sorted(fc)[len(fc) // 2] > 2.5 * top)
    go = [i for i, r in enumerate(res) if alive(r)]
    rest = capped(lim, lambda: _pmap(_qtask, [(jobs[i][0], jobs[i][1], 40, 4) for i in go], tick, every=10))
    for i, r in zip(go, rest):
        res[i] = merge([res[i], r])
    top = min((score(r) for r in res), default=math.inf)
    again = [i for i, r in enumerate(res) if math.isfinite(score(r)) and score(r) <= 2 * top]
    # only one strategy per split goes on (best_of below): the two best of each split are enough for the last 100
    per = {}
    for i in sorted(again, key=lambda i: score(res[i])):
        per.setdefault(owner[i], []).append(i)
    again = sorted(i for v_ in per.values() for i in v_[:2])
    tick2 = (lambda i, n_: progress(f"Что фрактурить, что десекрейтить: уточняю {i}/{n_}")) if progress else None
    lim = 10 * top if math.isfinite(top) else math.inf
    more = capped(lim, lambda: _pmap(_qtask, [(jobs[i][0], jobs[i][1], 100, 6) for i in again], tick2, every=10))
    for i, r in zip(again, more):
        res[i] = merge([res[i], r])
    best_of = {}
    for k, r in zip(owner, res):
        if k not in best_of or score(r) < score(best_of[k]):
            best_of[k] = r
    order = sorted(best_of, key=lambda k: score(best_of[k]))
    if not order or not math.isfinite(score(best_of[order[0]])):
        order = order[:1] or [0]
    pr = putref_screen(subs[0] if prob.multi and subs else prob, progress) if not getattr(prob, "no_putref", False) else None
    # the quick screen's estimate of the ordinary craft can be several times its tuned price (6 -> 1.24 div seen), so the
    # best ordinary route is always planned and compared with Putrefaction on exact runs; only extra finalists are dropped
    putref_far = bool(pr and order and pr[0] < 0.4 * score(best_of[order[0]]))
    if False:
        # Putrefaction is far cheaper than any ordinary route even before they are refined (refining wins well under
        # 2.5x): the ordinary ones stay rough alternatives
        pc = pr[2]
        pc.cap = CAP * 3
        final = sim_final(pc, pr[1], 20000, nmin=4000, target=0.03, progress=progress)
        alts, seen = [], set()
        for k in order[:4]:
            r = best_of[k]
            d = describe(r["v"], ctxs[k])
            if d not in seen and math.isfinite(r["mean"]):
                seen.add(d)
                alts.append({"desc": d + " (без Putrefaction, грубая оценка)", "mean": score(r), "ok": r["ok"], "rough": True})
        return final, alts, pc
    # finalists: the leader, plus the best of every other pool split (2 + 2, 1 + 3...) within 1.6x of it: the quick
    # screen runs fixed presets, and a split that loses there can win once its own strategy is tuned
    # a family = the pool split + which mods an essence / Alloy makes: a crafted mod instead of a bone is another way
    # of crafting, and the quick screen's presets rank it poorly (Hysteria 258 vs a bone 244, tuned: 88 vs 96)
    # and with or without a fracture: the screen's short runs rank a fracture route and a plain one with the same
    # essence by noise (gloves: Chaos on a one-mod rare + Hysteria, 60 div, lost the screen to a fracture route, 103)
    fam = lambda k: (id(pairs[k][0]), tuple(sorted(pairs[k][1]["ess"])), pairs[k][1]["frac"] is None)
    full, fams = order[:1], {fam(order[0])}
    top_s = score(best_of[order[0]])
    for k in order[1:]:
        if score(best_of[k]) >= 1.6 * top_s or len(full) >= 4:
            break
        if fam(k) not in fams or (len(full) < 2 and score(best_of[k]) < 1.3 * top_s):
            fams.add(fam(k))
            full.append(k)
    results = []
    glob = math.inf  # the best finalist so far: later ones stop their clearly losing variants against it
    for n, k in enumerate(full):
        lbl = f"Вариант {n + 1}/{len(full)}"
        final, alts = plan(ctxs[k], budget, fill, (lambda m, lbl=lbl: progress(f"{lbl}: {m}")) if progress else None,
                           do_final=False, template=best_of[k]["v"], ref=min(score(best_of[k]), glob))
        glob = min(glob, score(final))
        results.append((final, alts, ctxs[k]))
    if progress:
        progress("Точный прогон лучшей стратегии")
    # every finalist gets its exact run and the cheapest by that wins: a refine-stage estimate (2000 heavy-tailed crafts)
    # now and then ranks a finalist that is in fact twice as dear first
    # (a quick exact run for each, then the precise one only for the leader and those within 15% of it)
    results = [(sim_final(c_, f_["v"], 3000, nmin=3000, target=1.0, progress=progress), a_, c_) for f_, a_, c_ in results]
    lead = min(score(x[0]) for x in results)
    results = [(sim_final(c_, f_["v"], 20000, progress=progress) if score(f_) <= 1.15 * lead else f_, a_, c_)
               for f_, a_, c_ in results]
    results.sort(key=lambda x: score(x[0]))
    final, alts, ctx = results[0]
    final, ctx, alts = putref_vs(pr, final, ctx, alts, progress)
    for f2, a2, c2 in results[1:]:
        alts = [{"desc": describe(f2["v"], c2), "mean": score(f2), "ok": f2["ok"]}] + alts
    seen = {describe(final["v"], ctx)} | {a["desc"] for a in alts}
    for k in order[len(full):len(full) + 3]:
        r = best_of[k]
        d = describe(r["v"], ctxs[k])
        if d not in seen and math.isfinite(r["mean"]):
            alts.append({"desc": d, "mean": score(r), "ok": r["ok"], "rough": True})
    return final, alts, ctx


def putref_ctx(prob):
    """the goal as a Putrefaction lottery: every wanted mod must come from a reveal (no essence, Alloy, Kolr's Hunt)"""
    if PUTREF not in prob.prices:
        return None
    c = prob.ctx({"des": None, "frac": None, "ess": {}})
    for t in c.targets:
        if t["src"] != "natural" or t.get("mk") or c.dpool.W(t["group"], t["side"], 0, t["minLevel"]) <= 0:
            return None
    if c.req_side["P"] > 3 or c.req_side["S"] > 3:
        return None
    c.assign_desc = []
    return c


def putref_screen(prob, progress=None):
    """Omen of Putrefaction: a few bone / Echoes variants per attempt (mean = cost / chance) -> (estimate, v, ctx)"""
    pc = putref_ctx(prob)
    if pc is None:
        return None
    if progress:
        progress("Проверка Omen of Putrefaction")
    best = None
    for bone in ("Preserved", "Ancient"):
        for ech, pol in ((True, "always"), (True, "need")) + (((False, None),) if False in _echo_opts(pc) else ()):
            v = {"putref": True, "bone": bone, "echoes": ech, "echo": pol, "first": None, "light": False}
            r = simulate(pc, v, 3000, seed=11, only_first=True, early=False)
            if r["ok"] > 0 and math.isfinite(r["mean"]):
                est = r["mean"] / r["ok"]
                if best is None or est < best[0]:
                    best = (est, v)
    return (best[0], best[1], pc) if best else None


def putref_vs(pr, final, ctx, alts, progress=None):
    """the screened Putrefaction variant against the planned craft: simulated in full if it can win"""
    if pr is None:
        return final, ctx, alts
    best, pc = pr[:2], pr[2]
    if best[0] >= 1.2 * score(final):
        return final, ctx, alts + [{"desc": describe(best[1], pc), "mean": best[0], "ok": 1.0, "rough": True}]
    pc.cap = CAP * 3
    pf = sim_final(pc, best[1], 20000, nmin=4000, target=0.03, progress=progress)
    if score(pf) < score(final):
        return pf, pc, [{"desc": describe(final["v"], ctx) + " (без Putrefaction, предмет не corrupted)",
                         "mean": score(final), "ok": final["ok"]}] + alts
    return final, ctx, [{"desc": describe(best[1], pc), "mean": score(pf), "ok": pf["ok"]}] + alts


def chances(ctx, v=None):
    """per target: how it is obtained and the chance of one attempt (Perfect / Greater / plain Exalt + side omen)"""
    out = []
    for t in ctx.targets:
        e = {"text": t["text"], "side": t["side"], "src": t["src"], "frac": t["frac"], "opt": bool(t.get("opt"))}
        e["how"] = {"natural": "слэм", "desecrated": "десекрейт", "essence": t["name"], "alloy": t["name"]}[t["src"]]
        if (v or {}).get("putref"):
            f = ANCIENT_BONE_FLOOR if v["bone"] == "Ancient" else 0
            w = ctx.dpool.W(t["group"], t["side"], f, t["minLevel"]) / max(ctx.dpool.T(t["side"], f), 1e-9)
            e["how"], e["reveal"] = "раскрытие (Putrefaction)", 1 - (1 - w) ** (3 * (2 if v["echoes"] else 1))
            out.append(e)
            continue
        if t["src"] in ("natural", "essence") and ctx.pool.g[t["side"]].get(t["group"]) and not (t["src"] == "essence" and not t["magic"]):
            e["slam"] = {q or "Exalted": ctx.pool.W(t["group"], t["side"], FLOOR["exalt"][q], t["minLevel"]) /
                         max(ctx.pool.T(t["side"], FLOOR["exalt"][q]), 1e-9) for q in ("", "Greater", "Perfect")}
        if t["src"] == "desecrated":
            w = ctx.dpool.W(t["group"], t["side"], 0, t["minLevel"]) / max(ctx.dpool.T(t["side"], 0), 1e-9)
            looks = 2 if (v or {}).get("echoes") else 1  # 3 options; Abyssal Echoes rerolls all 3 once
            e["reveal"] = 1 - (1 - w) ** (3 * looks)
            if t.get("lord") and ctx.lord_omen:
                lp = ctx.lords[t["lord"]]
                e["reveal"] = 1 - (1 - w) ** (3 * looks - 1) * (1 - lp.W(t["group"], t["side"], 0, t["minLevel"]) / max(lp.T(t["side"], 0), 1e-9))
        if t["src"] in ("alloy",) or (t["src"] == "essence" and not t["magic"]):
            e["sure"] = t["name"]
        if t["src"] == "essence" and t["magic"]:
            e["sure"] = t["name"] + " (на синем предмете)"
        out.append(e)
    return out


def step_mode(ctx, v, answers):
    """replay the chosen strategy with the player's answers; stop at the next question"""
    s = Sim(ctx, v, random.Random(0), list(answers))
    try:
        ok = s.run()
    except Ask as a:
        return {"done": False, "ask": a.q, "trace": s.trace}
    return {"done": True, "ok": bool(ok), "trace": s.trace, "item": s.view(getattr(s, "last", None))}
