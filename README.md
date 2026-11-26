# poe2-craft-planner

A local web app for Path of Exile 2: a crafting planner plus gear search for your build, with every number computed by
the real [Path of Building (PoE2)](https://github.com/PathOfBuildingCommunity/PathOfBuilding-PoE2) running in the
background.

Русская версия: [README.ru.md](README.ru.md) (the full description of how everything is calculated).

## What it does
- **Craft planner.** Paste an item (the goal or the item you have now) or pick a base and the mods you want, with
  tiers and "any N of these" groups like the trade site. The planner simulates thousands of crafts per strategy
  (Exalted / Chaos / Annulment with omens, essences and alloys, desecration bones, fracturing, Essence of the Abyss
  cycles…) and picks the sequence of currency that is cheapest on average at live poe.ninja prices. You get a guide
  with chances and costs per step, a shopping list, and a step-by-step mode that follows your actual rolls.
- **Gear search.** Load a build (PoB code or pobb.in link); mod weights come from PoB itself, the official trade site
  is searched with them, and every candidate is re-evaluated in PoB against what you wear.
- Also: the build's gems and PoB configuration, jewel search, export of the build with the edited config.

## Install (macOS or Linux)
Requirements: Python 3.9+, git, make and a C compiler
(macOS: `xcode-select --install`; Debian/Ubuntu: `sudo apt install build-essential git`).
Windows is not supported yet.

```bash
git clone https://github.com/SeikoPoETool/poe2-craft-planner.git
cd poe2-craft-planner
./install.sh        # once: Path of Building (git submodule), builds LuaJIT and lua-utf8
python3 server.py   # then open http://127.0.0.1:8765
```
On macOS you can also double-click `start.command`.

Only the Python standard library is used. Optional: put any image as `static/bg.webp` for a page background. The server listens on 127.0.0.1 only (another port: `PORT=8800 python3 server.py`).

## Trade search and POESESSID
Searching the official trade site needs your `POESESSID` cookie, entered in the page. It is kept in the server's
memory only and sent only to pathofexile.com (the "remember" box keeps it in your own browser). The craft planner
does not need it.

## Updating
- The app: `git pull`.
- Path of Building: `cd engine/pob2 && git pull` and restart the server.
- Mod weights and alloys after a game patch: `python3 tools/update_poe2db.py`.

## Data sources
Path of Building Community (MIT), [poe.ninja](https://poe.ninja) prices, [poe2db](https://poe2db.tw) mod weights,
the official trade site, item images from pobb.in.

## Development
`docs/` describes the architecture, the craft planner and the data sources (in Russian).
`python3 tests/regress.py` runs the reference crafts against `tests/baseline.json` (the server must be running).

## License
MIT, see [LICENSE](LICENSE). This project is not affiliated with or endorsed by Grinding Gear Games.
Path of Exile is a trademark of Grinding Gear Games.
