#!/usr/bin/env python3
"""Build the ready-to-run package (a zip for people who do not use git): the app, Path of Building without its images
(the headless engine does not draw anything; they are ~90% of its size), the LuaJIT built for this machine, and on
Windows a bundled Python.

    python3 tools/package.py <name> [--python DIR]     ->  dist/<name>.zip  (one folder <name>/ inside)

Run after install.sh (macOS / Linux) or after the Windows build steps of the release workflow."""
import argparse, os, shutil, sys, zipfile

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
APP_FILES = ["server.py", "craft.py", "gamedata.py", "static/index.html", "start.bat", "start.command", "install.sh",
             "README.md", "README.en.md", "LICENSE"]
ENGINE_FILES = ["worker.lua", "boot_lib.lua", "mod_weights.json", "alloys.json"]
SKIP_EXT = {".png", ".jpg", ".jpeg", ".webp", ".zst", ".dds", ".tga", ".dll", ".exe", ".zip"}


def copy(src, dst):
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    shutil.copy2(src, dst)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("name")
    ap.add_argument("--python", help="folder with a Python to bundle (Windows embeddable)")
    a = ap.parse_args()
    win = os.name == "nt"
    out = os.path.join(HERE, "dist", a.name)
    shutil.rmtree(out, ignore_errors=True)
    for f in APP_FILES:
        copy(os.path.join(HERE, f), os.path.join(out, f))
    eng, oeng = os.path.join(HERE, "engine"), os.path.join(out, "engine")
    for f in ENGINE_FILES:
        copy(os.path.join(eng, f), os.path.join(oeng, f))
    bins = ["luajit.exe", "lua51.dll"] if win else ["luajit"]
    for f in bins:
        if not os.path.exists(os.path.join(eng, f)):
            sys.exit(f"missing engine/{f}: build LuaJIT first")
        copy(os.path.join(eng, f), os.path.join(oeng, f))
    clib = "lua-utf8." + ("dll" if win else "so")
    copy(os.path.join(eng, "clib", clib), os.path.join(oeng, "clib", clib))
    pob, opob = os.path.join(eng, "pob2"), os.path.join(oeng, "pob2")
    copy(os.path.join(pob, "LICENSE.md"), os.path.join(opob, "LICENSE.md"))
    for sub in ("src", os.path.join("runtime", "lua")):
        for root, dirs, files in os.walk(os.path.join(pob, sub)):
            dirs[:] = [d for d in dirs if d != ".git"]
            for f in files:
                if os.path.splitext(f)[1].lower() in SKIP_EXT:
                    continue
                p = os.path.join(root, f)
                copy(p, os.path.join(opob, os.path.relpath(p, pob)))
    if a.python:
        shutil.copytree(a.python, os.path.join(out, "python"))
    os.makedirs(os.path.join(out, "cache"), exist_ok=True)
    zpath = out + ".zip"
    with zipfile.ZipFile(zpath, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as z:
        for root, dirs, files in os.walk(out):
            for f in files:
                p = os.path.join(root, f)
                info = zipfile.ZipInfo.from_file(p, os.path.relpath(p, os.path.dirname(out)))
                info.compress_type = zipfile.ZIP_DEFLATED
                with open(p, "rb") as fh:
                    z.writestr(info, fh.read(), compresslevel=9)  # keeps the executable bit for macOS / Linux
            if not files and not dirs:
                z.writestr(os.path.relpath(root, os.path.dirname(out)) + "/", "")
    print(f"{zpath}  {os.path.getsize(zpath) / 1e6:.1f} MB")


if __name__ == "__main__":
    main()
