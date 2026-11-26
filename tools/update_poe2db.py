#!/usr/bin/env python3
"""Rebuild engine/mod_weights.json and engine/alloys.json from poe2db.tw (run after a PoE2 patch).

  python3 tools/update_poe2db.py            # both files; writes *.new.json next to the old ones and prints a diff
  python3 tools/update_poe2db.py --apply    # replace the old files (a copy of each goes to *.bak.json)
  python3 tools/update_poe2db.py --only weights|alloys

mod_weights.json: {poe2db page: {PoB mod id: spawn weight}}. One page per item class (armour pieces per attribute
type: Helmets_int, Gloves_str_dex...). Source: the page's ModsView JSON, list "normal": DropChance per mod; the mod
id is in its "hover" link (Data/Mods/<id>). Checked on 22.11.2026: reproduces the old file exactly (Amulets 203/203).

alloys.json: {alloy name: {PoB item class: PoB mod id}}. Source: each alloy's poe2db page, table "Class / Modifier /
Pre/Suf / Required Level"; the mod text is matched to an Alloy* mod in Path of Building's ModItem.lua by its lines.
New alloys in a patch: add their names to ALLOYS below (the page name is the name with "_" for spaces).

Needs only the Python standard library. poe2db is polite to slow clients: one request per second.
"""
import argparse, html, json, os, re, shutil, sys, time, urllib.parse, urllib.request

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ENGINE = os.path.join(HERE, "engine")
sys.path.insert(0, HERE)
from gamedata import ATTR, PAGE  # noqa: E402

UA = "Mozilla/5.0 (compatible; poe2-optimizer-local)"
BASE = "https://poe2db.tw/us/"
ALLOYS = ["Adaptive Alloy", "Celestial Alloy", "Cyclonic Alloy", "Expansive Alloy", "Mystic Alloy", "Prismatic Alloy",
          "Protective Alloy", "Runic Alloy", "Sovereign Alloy", "Swift Alloy", "The Runebinder's Alloy",
          "The Runefather's Alloy", "Transcendent Alloy"]
# armour pages are split by attribute type, as craft.weight_page() looks them up
SPLIT = {"Gloves": "armour", "Helmets": "armour", "Body_Armours": "armour", "Boots": "armour", "Shields": "shield"}
SHIELD_ATTR = ("str", "str_dex", "str_int")


def fetch(page):
    req = urllib.request.Request(BASE + urllib.parse.quote(page, safe="'"), headers={"User-Agent": UA})
    for attempt in range(3):
        try:
            time.sleep(1.0)
            return urllib.request.urlopen(req, timeout=60).read().decode("utf-8", "replace")
        except Exception as ex:  # noqa: BLE001
            print(f"  {page}: {ex} (retry)", file=sys.stderr)
            time.sleep(5)
    return None


def weight_pages():
    out = []
    for cls, p in PAGE.items():
        if p in SPLIT:
            out += [f"{p}_{a}" for a in (SHIELD_ATTR if p == "Shields" else ATTR)]
        else:
            out.append(p)
    return sorted(set(out))


def mods_view(text):
    i = text.find("ModsView({")
    if i < 0:
        return None
    obj, _ = json.JSONDecoder().raw_decode(text[i + len("ModsView("):])
    return obj


def build_weights(old):
    out = {}
    for page in weight_pages():
        text = fetch(page)
        obj = mods_view(text) if text else None
        if not obj:
            if page in old:
                print(f"  {page}: no data on poe2db — keeping the old weights")
                out[page] = old[page]
            continue
        w = {}
        for m in obj.get("normal") or []:
            x = re.search(r"Mods/([A-Za-z0-9_]+)", urllib.parse.unquote(m.get("hover") or ""))
            try:
                dc = int(m.get("DropChance") or 0)
            except ValueError:
                dc = 0
            if x and dc > 1:  # DropChance 1 = placeholder for mods that do not roll normally
                w[x.group(1)] = dc
        if w:
            out[page] = w
        print(f"  {page}: {len(w)} mods")
    return out


def pob_verisium_mods():
    """Alloy mods of Path of Building (ids start with "Alloy"): id -> normalised text lines"""
    src = open(os.path.join(ENGINE, "pob2", "src", "Data", "ModItem.lua"), encoding="utf-8").read()
    out = {}
    for m in re.finditer(r'\["(Alloy\w+)"\] = \{ type = "(?:Prefix|Suffix)", affix = "[^"]*", (.*?), statOrder', src):
        out[m.group(1)] = tuple(norm(x) for x in re.findall(r'"([^"]*)"', m.group(2)))
    return out


def norm(line):
    line = html.unescape(re.sub(r"<[^>]+>", "", line)).replace("—", "-").replace("–", "-")
    return re.sub(r"\s+", " ", line).strip()


def singular(page_cls):
    rev = {v.replace("_", " "): k for k, v in PAGE.items()}
    return rev.get(page_cls, page_cls)


def build_alloys(old):
    pob = pob_verisium_mods()
    by_text = {}
    for mid, lines in pob.items():
        by_text.setdefault(tuple(sorted(lines)), mid)
    out = {}
    for name in ALLOYS:
        text = fetch(name.replace(" ", "_"))
        if not text:
            print(f"  {name}: page not loaded — keeping the old mapping")
            if name in old:
                out[name] = old[name]
            continue
        per, miss = {}, []
        for row in re.finditer(r'<tr><td><a class="ItemClasses[^"]*"[^>]*>([^<]+)</a></td><td>(.*?)</td><td>(Prefix|Suffix)</td>', text):
            cls = singular(row.group(1).strip())
            lines = tuple(sorted(norm(x) for x in re.split(r"<br\s*/?>", row.group(2))))
            mid = by_text.get(lines)
            if mid:
                per[cls] = mid
            else:
                miss.append(f"{cls}: {' / '.join(lines)}")
        out[name] = per
        print(f"  {name}: {len(per)} classes" + (f"; not found in PoB: {miss[:3]}" if miss else ""))
    return out


def diff(old, new, what):
    added = sorted(set(new) - set(old)); gone = sorted(set(old) - set(new))
    changed = 0
    for k in set(old) & set(new):
        a, b = old[k], new[k]
        changed += sum(1 for x in set(a) | set(b) if a.get(x) != b.get(x))
    print(f"{what}: {len(new)} entries, +{len(added)} −{len(gone)}, changed values: {changed}")
    if added:
        print("  new:", ", ".join(added[:20]))
    if gone:
        print("  gone:", ", ".join(gone[:20]))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="replace the files (old ones -> *.bak.json)")
    ap.add_argument("--only", choices=("weights", "alloys"))
    a = ap.parse_args()
    jobs = [("weights", "mod_weights.json", build_weights), ("alloys", "alloys.json", build_alloys)]
    for key, fn, build in jobs:
        if a.only and a.only != key:
            continue
        path = os.path.join(ENGINE, fn)
        old = json.load(open(path)) if os.path.exists(path) else {}
        print(f"== {fn}")
        new = build(old)
        diff(old, new, fn)
        if a.apply:
            if os.path.exists(path):
                shutil.copy(path, path.replace(".json", ".bak.json"))
            json.dump(new, open(path, "w"), ensure_ascii=False, indent=0)
            print(f"  written {path}")
        else:
            out = path.replace(".json", ".new.json")
            json.dump(new, open(out, "w"), ensure_ascii=False, indent=0)
            print(f"  preview {out} (run with --apply to replace)")


if __name__ == "__main__":
    main()
