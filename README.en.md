# poe2-craft-planner

A helper for Path of Exile 2: it works out the cheapest way to craft the item you want and finds gear upgrades for
your build on the trade site. It runs on your computer; every build number comes from the real
[Path of Building](https://github.com/PathOfBuildingCommunity/PathOfBuilding-PoE2).

Русская версия (основная): [README.md](README.md)

## What it does

- **Crafting.** Tell it the item you want — it tries the methods (Exalted, Chaos, essences, alloys, bones, fracturing,
  omens…), picks the cheapest on average at live poe.ninja prices, and walks you through it step by step.
- **Gear search.** Load your build — it searches the trade site for the items that raise your DPS or defences the
  most and checks every candidate in PoB.
- **Jewels, PoB configuration, build export** back to Path of Building.

## Install

**Windows.** Download [poe2-craft-planner-setup.exe](https://github.com/SeikoPoETool/poe2-craft-planner/releases/latest/download/poe2-craft-planner-setup.exe)
and run it. No admin rights and no Python needed. You get a "PoE2 Craft Planner" shortcut.

**macOS (Apple Silicon) and Linux.** Download the zip for your system from the
[release page](https://github.com/SeikoPoETool/poe2-craft-planner/releases/latest), unzip it and run `start.command`
(Linux: `./start.command` in a terminal, needs `python3`).

The app opens in your browser. Keep the black window open while you use it.

> The app is not signed, so the first start shows a warning.
> **Windows:** *More info* → *Run anyway*. **macOS:** *System Settings → Privacy & Security* → *Open Anyway*.

**Updating:** Windows — run the new setup over the old one. macOS/Linux — download the new zip and copy the `cache`
folder from the old one (your saved craft links).

## Crafting

1. Open the "Крафт" (Craft) tab. Paste an item from the game (Ctrl+C) or pick a base.
2. Parse it and tick the mods you want and their minimum tier. A "Count" group means "any N of these", as on trade.
3. Enter the base price and run the calculation.
4. You get a step-by-step guide and the average cost in divines; the step-by-step mode asks what you rolled and tells
   you what to do next; you can compare the cost with the trade price of a finished item.

## Gear search

1. Paste a pobb.in link or a PoB code and load it.
2. "Поиск замены" (find a replacement): pick a slot, optionally required mods and a max price, paste your POESESSID
   and search. Or click an item on the paper doll, pick the mods to keep and search from there.
3. The table shows the gain in percent and the price; "Примерить" compares an item with the one you wear.

**POESESSID** is needed for weighted trade searches. Log in at pathofexile.com, press F12 → Application → Cookies →
POESESSID. It is sent only to pathofexile.com and stored only in your browser, and only if you tick "remember".

## For developers

Python 3.9+, git and a C compiler are needed.

```bash
git clone https://github.com/SeikoPoETool/poe2-craft-planner.git
cd poe2-craft-planner
./install.sh          # once: Path of Building and a LuaJIT build
python3 server.py     # http://127.0.0.1:8765
```

`python3 tests/regress.py` runs the reference crafts; [`docs/`](docs) describes how it works (in Russian).
Every push to `main` builds the installer and the packages and publishes them as the latest release.

## License

MIT, see [LICENSE](LICENSE). Not affiliated with or endorsed by Grinding Gear Games. Path of Exile is a trademark of
Grinding Gear Games.
