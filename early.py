"""Budget («early») craft: what a few divines buy, not the cheapest route to one exact goal.

The player picks a base, optionally the main mod(s), and which mods are useful at all. Each recipe (Transmute + Augment
+ Regal, Alchemy, a magic essence, ... then cheap Exalted Orbs) is rolled thousands of times from a fresh white base and
every result is graded:
- by tiers: how many useful mods it got from the top 4 tiers this item level can roll;
- with the build (PoB): how much better than the item worn in that slot it is, in % of the chosen metrics (each mod
  tier measured in PoB on its own, the item = the bare base + the sum of its mods).
For each recipe the strictest goal the budget reaches in most cases, and what reaching it usually costs."""
import math
import random
import re

from gamedata import CUR, FLOOR, ALCHEMY, OMEN, ANNUL
import craft as CRAFT

TV = (1.0, 0.8, 0.6, 0.4, 0.25, 0.15, 0.1)  # value of a tier by its rank among the tiers this item level can roll
GOOD = 0.4      # a useful mod counts from this value on: the top 4 tiers available
N = 3000        # simulated tries per recipe
SURE = 0.75     # the goal offered is the strictest one the budget reaches with at least this chance
SLAMS = 3       # Exalted Orbs after the rare is made (to 6 mods with Alchemy's 4 + 2)
POB_MIN = 0.3   # with the build: a mod family is useful from this gain (% of the metrics) of its best tier on

# the slot a base goes to (the first of a pair)
SLOT = {"Helmet": "Helmet", "Gloves": "Gloves", "Boots": "Boots", "Body Armour": "Body Armour", "Belt": "Belt",
        "Amulet": "Amulet", "Ring": "Ring 1", "Shield": "Weapon 2", "Buckler": "Weapon 2", "Focus": "Weapon 2",
        "Quiver": "Weapon 2"}


def slot_of(cls):
    return SLOT.get(cls, "Weapon 1")


class Roller:
    """random mods of one base: a family (side, group) by its total weight among those that fit, then its tier"""

    def __init__(self, cd, ilvl, wfn):
        # (side, group) -> [(level, weight, game tier, rank at this ilvl, text, lines)] best first
        self.fam = {}
        for m in cd["pool"]:
            s = m.get("side")
            if s in ("P", "S"):
                self.fam.setdefault((s, m["group"]), []).append((m["level"], wfn(m), m["lines"]))
        for k, ts in list(self.fam.items()):
            ts.sort(key=lambda t: -t[0])
            ok = [t for t in ts if t[0] <= ilvl]
            if not ok:
                del self.fam[k]
                continue
            off = len(ts) - len(ok)
            self.fam[k] = [(lv, w, off + r + 1, r, " / ".join(ls), ls) for r, (lv, w, ls) in enumerate(ok)]
        self._tab = {}
        # essence-only mods (Perfect Essence of Sorcery: +5 to Level of all Spell Skills, its own group): a family that
        # never rolls, counted as the natural family with the same text, at the tier its value reaches
        self.alias = {}
        shape = lambda ls: tuple(CRAFT.tmpl(l)[0].pattern for l in ls)
        by_shape = {}
        for k, ts in self.fam.items():
            by_shape.setdefault((k[0], shape(ts[0][5])), k)
        for e in cd.get("essences") or []:
            k = (e.get("side"), e["group"])
            if k[0] not in ("P", "S") or k in self.fam or e["level"] > ilvl:
                continue
            nat = by_shape.get((k[0], shape(e["lines"])))
            if not nat:
                continue
            v = _num(e["lines"][0])
            r = next((t for t in self.fam[nat] if _num(t[5][0]) <= v), self.fam[nat][-1])
            self.fam[k] = [(e["level"], 0, r[2], r[3], " / ".join(e["lines"]), e["lines"])]
            self.alias[k] = nat

    def table(self, floor):
        t = self._tab.get(floor)
        if t is None:
            t = []
            for k, ts in self.fam.items():
                # a Greater / Perfect orb's minimum level, but a family whose best tier is lower still rolls that tier
                tiers = [x for x in ts if x[0] >= min(floor, ts[0][0]) and x[1] > 0]
                if tiers:
                    t.append((k, sum(x[1] for x in tiers), tiers))
            self._tab[floor] = t
        return t

    def roll(self, item, room, floor, rng):
        """add one random mod (a family not on the item yet, on a side with room); False when nothing fits"""
        have = {m[0] for m in item}
        cand = [c for c in self.table(floor) if c[0] not in have and room[c[0][0]] > 0]
        if not cand:
            return False
        r = rng.random() * sum(c[1] for c in cand)
        for k, w, tiers in cand:
            if r < w:
                break
            r -= w
        r = rng.random() * w
        for t in tiers:
            if r < t[1]:
                break
            r -= t[1]
        item.append((k, t))
        room[k[0]] -= 1
        return True

    def tier_of(self, key, level):
        """the tier (as rolled) an essence's mod of this level matches"""
        ts = self.fam.get(key) or []
        return next((t for t in ts if t[0] <= level), ts[-1] if ts else None)


def _num(line):
    """the first number of a mod line (a range: its low end)"""
    m = re.search(r"-?\d+(?:\.\d+)?", line)
    return float(m.group()) if m else 0.0


_RANGE = re.compile(r"\((-?\d+(?:\.\d+)?)-(-?\d+(?:\.\d+)?)\)")


def _mid(line):
    def f(m):
        a, b = float(m.group(1)), float(m.group(2))
        v = (a + b) / 2
        return str(int(round(v))) if a == int(a) and b == int(b) else f"{v:.1f}"
    return _RANGE.sub(f, line)


def pob_lines(cd, ilvl):
    """every tier this item level can roll, as mod lines with the middle roll: [((side, group), rank, text)]"""
    ro = Roller(cd, ilvl, lambda m: 1)
    return [(k, t[3], "\n".join(_mid(l) for l in t[5])) for k, ts in ro.fam.items() for t in ts]


def recipes(ess, useful):
    """(name, steps, essence) for every cheap recipe; steps: (kind, currency, floor or the essence) in order"""
    out = []
    for tq in ("", "Greater", "Perfect"):
        for eq in ("", "Greater", "Perfect"):
            name = f"{CUR['transmute'][tq]} + {CUR['augment'][tq]} + {CUR['regal'][eq]}, затем {CUR['exalt'][eq]} ×{SLAMS}"
            st = [("transmute", CUR["transmute"][tq], FLOOR["transmute"][tq]), ("augment", CUR["augment"][tq], FLOOR["augment"][tq]),
                  ("regal", CUR["regal"][eq], FLOOR["regal"][eq])] + [("exalt", CUR["exalt"][eq], FLOOR["exalt"][eq])] * SLAMS
            out.append((name, st, None))
    for eq in ("", "Greater", "Perfect"):
        out.append((f"{ALCHEMY}, затем {CUR['exalt'][eq]} ×2",
                    [("alchemy", ALCHEMY, 0)] + [("exalt", CUR["exalt"][eq], FLOOR["exalt"][eq])] * 2, None))
    for e in ess:
        if (e["side"], e["group"]) not in useful:
            continue
        if e["rare"]:
            # Perfect / corrupted: on a rare it replaces a mod. Right after the Regal (3 mods, little to lose, its side has
            # room: no omen needed), or with its side's Crystallisation omen, or last on the full item with the omen
            mk = [("transmute", CUR["transmute"][""], 0), ("augment", CUR["augment"][""], 0), ("regal", CUR["regal"][""], 0)]
            om = ("omen", OMEN["cryst"][e["side"]], None)
            for q in ("", "Perfect"):
                ex = [("exalt", CUR["exalt"][q], FLOOR["exalt"][q])] * SLAMS
                out.append((f"Regal, {e['name']}, затем {CUR['exalt'][q]}", mk + [("pess0", e["name"], e)] + ex, e))
                out.append((f"Regal, {e['name']} + омен, затем {CUR['exalt'][q]}", mk + [("pess", e["name"], e), om] + ex, e))
                out.append((f"Regal + {CUR['exalt'][q]}, затем {e['name']} + омен", mk + ex + [("pess", e["name"], e), om], e))
        else:  # magic: turns the magic item rare with its mod
            for q in ("", "Perfect"):
                out.append((f"{CUR['transmute']['']} + {CUR['augment']['']} + {e['name']}, затем {CUR['exalt'][q]} ×{SLAMS}",
                            [("transmute", CUR["transmute"][""], 0), ("augment", CUR["augment"][""], 0), ("essence", e["name"], e)]
                            + [("exalt", CUR["exalt"][q], FLOOR["exalt"][q])] * SLAMS, e))
    return out


def on_item(cur, rar, ess, useful):
    """one-shot recipes on the player's own item: (name, steps, essence)"""
    out = []
    have = {k for k, _ in cur}
    nP, nS = (sum(1 for k, _ in cur if k[0] == s) for s in "PS")
    if rar == "MAGIC":
        out.append((f"{CUR['regal']['']} + {CUR['exalt']['']} ×{SLAMS}",
                    [("regal", CUR["regal"][""], 0)] + [("exalt", CUR["exalt"][""], 0)] * SLAMS, None))
        for e in ess:
            k = (e["side"], e["group"])
            if not e["rare"] and k in useful and k not in have:
                out.append((f"{e['name']} + {CUR['exalt']['']} ×{SLAMS}", [("essence", e["name"], e)] + [("exalt", CUR["exalt"][""], 0)] * SLAMS, e))
    elif rar == "RARE":
        free = max(0, 3 - nP) + max(0, 3 - nS)
        for q in ("", "Greater", "Perfect"):
            if free > 0:
                out.append((f"{CUR['exalt'][q]} ×{free}", [("exalt", CUR["exalt"][q], FLOOR["exalt"][q])] * free, None))
        for e in ess:
            k = (e["side"], e["group"])
            if e["rare"] and k in useful and k not in have:
                out.append((f"{e['name']}", [("pess0", e["name"], e)], e))
                if nP if k[0] == "P" else nS:
                    out.append((f"{e['name']} + {OMEN['cryst'][k[0]]}", [("pess", e["name"], e), ("omen", OMEN["cryst"][k[0]], None)], e))
        # the full rework: Chaos Orbs (or Annul + Exalted) until the item is better than now, while the budget lasts
        for q in ("", "Greater", "Perfect"):
            out.append((f"{CUR['chaos'][q]} до улучшения", [("chaosloop", CUR["chaos"][q], FLOOR["chaos"][q])], None))
        out.append((f"{ANNUL} + {CUR['exalt']['']} до улучшения", [("annulloop", ANNUL, 0), ("annulloop", CUR["exalt"][""], 0)], None))
    return out


CALIB = 160    # with the build: crafted items measured in PoB whole, to correct the sum of single mods


def item_text(item):
    return "\n".join(_mid(l) for _, t in item for l in t[5])


def calibrate(runs, measure, rng):
    """the sum of single-mod values misses how mods multiply (spell levels x gain as extra x spell damage: -26% on a
    staff that is really 0%); a sample of items (the best of each recipe + random ones) measured whole in PoB gives a
    linear correction a + b * sum"""
    pick = []
    per = max(4, CALIB // max(1, len(runs)))
    for r in runs:
        order = sorted(range(len(r["items"])), key=lambda i: -r["outs"][i][2])
        idx = order[:per // 2] + rng.sample(order, min(len(order), per - per // 2))
        pick += [(r, i) for i in dict.fromkeys(idx)]
    pick = pick[:CALIB]
    if len(pick) < 5:
        return 0.0, 1.0
    exact = measure([item_text(r["items"][i]) for r, i in pick])
    xs = [r["outs"][i][2] for r, i in pick]
    mx, my = sum(xs) / len(xs), sum(exact) / len(exact)
    vx = sum((x - mx) ** 2 for x in xs)
    b = sum((x - mx) * (y - my) for x, y in zip(xs, exact)) / vx if vx > 1e-9 else 1.0
    b = min(3.0, max(0.5, b))
    return my - b * mx, b


_GRADE = re.compile(r"\b(Greater|Perfect) (?!Essence)")


def one_per_grade(cs):
    """recipes that differ only in the orbs' grade (plain / Greater / Perfect) are one row: the first (best) stays"""
    seen, out = set(), []
    for c in cs:
        k = _GRADE.sub("", c["name"])
        if k not in seen:
            seen.add(k)
            out.append(c)
    return out


def undominated(cs, good, cost):
    """among the variants of one essence (with or without the omen, early or last): drop one another beats — at least
    as good in every way and no dearer (an omen that changes nothing). Different recipes all stay: they are the
    alternatives"""
    grp = lambda c: c.get("gives") and c["gives"]["text"]

    def beats(a, b):
        return all(x >= y - 1e-9 for x, y in zip(good(a), good(b))) and cost(a) <= cost(b) + 1e-9
    return [c for i, c in enumerate(cs) if not grp(c) or not any(
        j != i and grp(o) == grp(c) and beats(o, c) and (cost(o) < cost(c) - 1e-9 or j < i) for j, o in enumerate(cs))]


def plan_early(cd, ilvl, prices, base_price, budget, main, useful, pob=None, mine=None, measure=None, seed=7):
    """main, useful: sets of (side, group); pob: {"blank": %, "val": {((side, group), rank): %}} or None;
    mine: the player's item {"rarity", "mods": [(side, group, level)], "text"} or None; measure(texts) -> % of each
    item against the worn one (PoB, whole items). Returns recipe cards, best first"""
    ro = Roller(cd, ilvl, CRAFT.weigher(cd))
    useful = set(useful) | set(main)
    useful |= {k for k, nat in ro.alias.items() if nat in useful}
    # each main mod: its natural family or an essence-only mod with the same text
    need = [{mk} | {k for k, nat in ro.alias.items() if nat == mk} for mk in main]
    main = set().union(*need) if need else set()

    def grade_(item):
        return grade(item, need, main, useful, pob)
    ess = [{"name": e["name"], "side": e["side"], "group": e["group"], "level": e["level"], "rare": CRAFT.is_rare_essence(e["name"])}
           for e in cd["essences"] if e.get("side") and e["level"] <= ilvl and (e["side"], e["group"]) in ro.fam]
    cur, ref = [], None
    if mine:
        for side, group, level in mine["mods"]:
            t = ro.tier_of((side, group), level)
            cur.append(((side, group), t or (level, 0, 0, 9, group, [])))
        ref = grade_(cur)
    missing = set()

    def price(steps):
        cost = 0.0
        for kind, c, _ in steps:
            p = prices.get(c)
            if p is None:
                missing.add(c)
                return None
            cost += p
        return cost

    def gives(c, e):
        if e:  # what the essence is for: the mod it always adds
            t = ro.tier_of((e["side"], e["group"]), e["level"])
            c["gives"] = {"text": t[4] if t else e["name"], "side": e["side"], "key": (e["side"], e["group"])}
        return c

    runs, own_runs = [], []
    for name, steps, e in recipes(ess, useful):
        cost = price(steps)
        if cost is None or cost + base_price > budget:
            continue
        if any(isinstance(f, int) and f > ilvl for _, _, f in steps):
            continue  # currency with a minimum modifier level cannot be used below that item level
        rng = random.Random(seed)
        items = [roll_once(ro, steps, rng) for _ in range(N)]
        runs.append({"name": name, "steps": steps, "e": e, "cost": cost + base_price, "items": items,
                     "outs": [grade_(it) for it in items]})
    if mine:
        for name, steps, e in on_item(cur, mine["rarity"], ess, useful):
            cost = price(steps)
            if cost is None or cost > budget or any(isinstance(f, int) and f > ilvl for _, _, f in steps):
                continue
            rng = random.Random(seed)
            if steps[0][0] in ("chaosloop", "annulloop"):
                eps0 = 0.05 if pob else 0.01
                runs_ = [rework(ro, cur, steps[0][2], rng, cost, budget, lambda it: grade_(it)[2] > ref[2] + eps0)
                         for _ in range(N_LOOP)]
                items, spent = [r_[0] for r_ in runs_], [r_[1] for r_ in runs_]
            else:
                items = [roll_once(ro, steps, rng, cur, mine["rarity"]) for _ in range(N)]
                spent = [cost] * N
            own_runs.append({"name": name, "steps": steps, "e": e, "cost": cost, "items": items, "spent": spent,
                             "outs": [grade_(it) for it in items]})
    if pob and measure and (runs or own_runs):
        a, b = calibrate(runs + own_runs, measure, random.Random(seed))
        for r in runs + own_runs:
            r["outs"] = [(g, m, a + b * v) for g, m, v in r["outs"]]
        if mine:  # the player's own item measured whole, with its real rolls
            ref = (ref[0], ref[1], measure([mine["text"]])[0])
    cards = []
    for r in runs:
        c = card(r["name"], r["steps"], prices, r["cost"], budget, r["outs"], main, pob, ref)
        if c:
            cards.append(gives(c, r["e"]))
    if pob:  # the expected gain: the chance to beat the item times the mean gain when it does
        cards.sort(key=lambda c: (-c["plan"]["pimp"] * c["plan"]["gmean"], c["plan"]["spend"]))
    else:
        cards.sort(key=lambda c: (-c["plan"]["k"], c["plan"]["spend"], -c["value"]))  # the same goal: the cheapest first
    cards = undominated(cards, lambda c: (c["plan"]["gain"] if pob else c["plan"]["k"], round(c["plan"]["sure"] * 20) / 20), lambda c: c["plan"]["usual"])
    cards = one_per_grade(cards)
    top = cards[:5]
    # an essence that guarantees a main mod, or a Perfect one (a top tier for sure), stays in the list even when the
    # count of useful mods ranks it lower: it is the obvious way to that mod
    for c in cards[5:]:
        g = c.get("gives")
        if g and len(top) < 7 and ("Perfect" in c["name"] or g["key"] in main) \
                and not any(x.get("gives") and x["gives"]["text"] == g["text"] for x in top):
            top.append(c)
    own = []
    eps = 0.05 if pob else 0.01
    for r in own_runs:  # on the player's item: how often it ends better, by how much on average, what it costs
        n = len(r["outs"])
        d = [o[2] - ref[2] for o in r["outs"]]
        cnt = {}
        for _, c_, _ in r["steps"]:
            cnt[c_] = cnt.get(c_, 0) + 1
        sp = sorted(r["spent"])
        loop = r["steps"][0][0] in ("chaosloop", "annulloop")
        own.append(gives({"name": r["name"], "cost": sp[n // 2], "upto": sp[int(n * 0.9)], "loop": loop,
                          "better": sum(1 for x in d if x > eps) / n,
                          "worse": sum(1 for x in d if x < -eps) / n, "delta": sum(d) / n,
                          "goodDelta": sum(o[0] for o in r["outs"]) / n - ref[0],
                          "steps": [{"name": c_, "n": n, "price": prices.get(c_)} for c_, n in cnt.items()]}, r["e"]))
    own = undominated(own, lambda c: (c["delta"], c["better"]), lambda c: c["cost"])
    own.sort(key=lambda c: -c["delta"])
    own = one_per_grade(own)
    own = [c for c in own if c["delta"] > 0][:3]  # only what improves the item on average
    return {"cards": top, "mine": own, "missing": sorted(missing),
            "current": {"good": ref[0], "value": ref[2]} if ref else None}


N_LOOP = 600   # simulated reworks of the player's item (each a loop of up to budget / price steps)


def rework(ro, start, floor, rng, cost, budget, better):
    """Chaos Orb (or Annul + Exalted) on the player's rare: a random mod out, a random one in, until better() or the
    budget is spent; returns (the item, divines spent)"""
    item, spent = list(start), 0.0
    while spent + cost <= budget:
        spent += cost
        if item:
            item.remove(rng.choice(item))
        room = {s: 3 - sum(1 for m in item if m[0][0] == s) for s in "PS"}
        ro.roll(item, room, floor, rng)
        if better(item):
            break
    return item, spent


def roll_once(ro, steps, rng, start=(), rar=None):
    item = list(start)
    if rar == "RARE":
        room = {s: 3 - sum(1 for m in item if m[0][0] == s) for s in "PS"}
    else:
        room = {s: 1 - sum(1 for m in item if m[0][0] == s) for s in "PS"}
    for kind, cur, f in steps:
        if kind in ("transmute", "augment"):
            ro.roll(item, room, f, rng)
        elif kind == "alchemy":
            room = {"P": 3, "S": 3}
            for _ in range(4):
                ro.roll(item, room, 0, rng)
        elif kind in ("regal", "essence"):
            room = {s: 3 - sum(1 for m in item if m[0][0] == s) for s in "PS"}
            if kind == "regal":
                ro.roll(item, room, f, rng)
                continue
            key = (f["side"], f["group"])
            for m in [m for m in item if m[0] == key]:  # the same family from the Transmute: the essence's mod replaces it
                item.remove(m)
                room[key[0]] += 1
            t = ro.tier_of(key, f["level"])
            if t and room[key[0]] > 0:
                item.append((key, t))
                room[key[0]] -= 1
        elif kind in ("pess", "pess0"):
            # a Perfect essence replaces a mod: with its side's Crystallisation omen a random mod of that side, without
            # it any random mod (one of its side when that side is full); the same family goes first
            key = (f["side"], f["group"])
            same = [m for m in item if m[0] == key]
            side = [m for m in item if m[0][0] == key[0]]
            pool = side if kind == "pess" or room[key[0]] <= 0 else item
            gone = same[0] if same else rng.choice(pool) if pool else None
            if gone:
                item.remove(gone)
                room[key[0]] += 1
            t = ro.tier_of(key, f["level"])
            if t and room[key[0]] > 0:
                item.append((key, t))
                room[key[0]] -= 1
        elif kind == "exalt":
            ro.roll(item, room, f, rng)
    return item


def grade(item, need, main, useful, pob):
    """(useful mods of a good tier besides the main ones, main mods all there, value): the value is the % against the
    worn item with the build, else the tier sum"""
    good = sum(1 for k, t in item if k in useful and k not in main and TV[min(t[3], 6)] >= GOOD)
    mok = all(any(k in ks and TV[min(t[3], 6)] >= GOOD for k, t in item) for ks in need)
    if pob:
        val = pob["blank"] + sum(max(0.0, pob["val"].get((k, t[3]), 0.0)) for k, t in item if k in useful)
    else:
        val = sum(TV[min(t[3], 6)] * (2 if k in main else 1) for k, t in item if k in useful)
    return good, mok, val


def _tries(p, q, n_max):
    """tries until the first hit with chance p, at quantile q (capped by what the budget pays for)"""
    if p >= 1:
        return 1
    return min(n_max, max(1, math.ceil(math.log(1 - q) / math.log(1 - p))))


def card(name, steps, prices, cost, budget, outs, main, pob, ref=None):
    n_max = int(budget // cost) if cost > 0 else 1000
    if n_max < 1:
        return None
    need = 1 - (1 - SURE) ** (1 / n_max)  # the chance per try the budget needs to reach a goal with SURE certainty
    ok_main = [o for o in outs if o[1]]
    if not ok_main:
        return None
    if pob:
        # the best gain over the worn item that the budget reaches with SURE certainty
        r0 = ref[2] if ref else 0.0  # against the player's item when given, else against the worn one
        vals = sorted((o[2] - r0 for o in ok_main), reverse=True)
        k = max(0, min(len(vals) - 1, math.ceil(need * len(outs)) - 1))
        goal = vals[k]
        hit = [v for v in vals if v >= goal]
        p = len(hit) / len(outs)
        best = {"gain": goal, "mean": sum(hit) / len(hit)}
        # crafting until the item is better than the worn one (or the player's), within the budget: the chance it
        # happens and the mean gain of the item it stops on
        up = [v for v in vals if v > 0.05]
        p0 = len(up) / len(outs)
        best.update(pimp=1 - (1 - p0) ** n_max if p0 > 0 else 0.0, gmean=sum(up) / len(up) if up else 0.0,
                    uimp=_tries(p0, 0.5, n_max) * cost if p0 > 0 else 0.0)  # what it usually costs to get there
        value = best["mean"]
    else:
        best = None
        for k in range(0, 7):
            p = sum(1 for o in ok_main if o[0] >= k) / len(outs)
            if p <= 0 or (best is not None and p < need):
                break
            best = {"k": k}
        k = best["k"]
        p = sum(1 for o in ok_main if o[0] >= k) / len(outs)
        value = sum(o[2] for o in ok_main if o[0] >= k) / max(1, sum(1 for o in ok_main if o[0] >= k))
    best.update(main=bool(main), p=p, sure=1 - (1 - p) ** n_max, spend=(1 - (1 - p) ** n_max) / p * cost,
                usual=_tries(p, 0.5, n_max) * cost, upto=_tries(p, 0.9, n_max) * cost)
    cnt = {}
    for _, cur, _ in steps:
        cnt[cur] = cnt.get(cur, 0) + 1
    return {"name": name, "cost": cost, "plan": best, "value": value,
            "steps": [{"name": c, "n": n, "price": prices.get(c)} for c, n in cnt.items()]}
