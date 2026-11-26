#!/bin/bash
# One-time setup on macOS or Linux: Path of Building (git submodule), LuaJIT and the lua-utf8 module.
# Needs git, make and a C compiler (macOS: xcode-select --install; Debian/Ubuntu: sudo apt install build-essential git).
set -e
cd "$(dirname "$0")"
ROOT="$(pwd)"

for tool in python3 git make cc; do
  command -v "$tool" >/dev/null || { echo "Missing: $tool"; exit 1; }
done

# macOS: a half-updated Command Line Tools install can ship an SDK its linker cannot read ("tapi error: malformed
# file"); then take the newest installed SDK that links
if [ "$(uname)" = "Darwin" ]; then
  PROBE="$(mktemp -d)"
  printf 'int main(void){return 0;}\n' > "$PROBE/p.c"
  if ! cc "$PROBE/p.c" -o "$PROBE/p" 2>/dev/null; then
    for sdk in $(ls -d /Library/Developer/CommandLineTools/SDKs/MacOSX[0-9]*.sdk 2>/dev/null | sort -rV); do
      if SDKROOT="$sdk" cc "$PROBE/p.c" -o "$PROBE/p" 2>/dev/null; then
        export SDKROOT="$sdk"; echo "Using SDK: $sdk"; break
      fi
    done
  fi
  rm -rf "$PROBE"
fi

# 1. Path of Building (PoE2): the calculation engine, pinned as a submodule
if [ ! -f engine/pob2/src/Launch.lua ]; then
  echo "== Path of Building"
  git submodule update --init --depth 1 engine/pob2 || git submodule update --init engine/pob2
fi

# 2. LuaJIT and lua-utf8, built for this machine
if ! engine/luajit -v >/dev/null 2>&1 || [ ! -f engine/clib/lua-utf8.so ]; then
  echo "== LuaJIT and lua-utf8"
  TMP="$(mktemp -d)"
  trap 'rm -rf "$TMP"' EXIT
  git clone --depth 1 --branch v2.1 https://github.com/LuaJIT/LuaJIT.git "$TMP/LuaJIT"
  if [ "$(uname)" = "Darwin" ]; then
    # newer macOS SDKs break the LuaJIT link step without a deployment target
    (cd "$TMP/LuaJIT" && MACOSX_DEPLOYMENT_TARGET="${MACOSX_DEPLOYMENT_TARGET:-14.0}" make -j4)
  else
    (cd "$TMP/LuaJIT" && make -j4)
  fi
  cp "$TMP/LuaJIT/src/luajit" engine/luajit
  git clone --depth 1 https://github.com/starwing/luautf8.git "$TMP/luautf8"
  mkdir -p engine/clib
  if [ "$(uname)" = "Darwin" ]; then
    cc -O2 -fPIC -bundle -undefined dynamic_lookup -I"$TMP/LuaJIT/src" "$TMP/luautf8/lutf8lib.c" -o engine/clib/lua-utf8.so
  else
    cc -O2 -fPIC -shared -I"$TMP/LuaJIT/src" "$TMP/luautf8/lutf8lib.c" -o engine/clib/lua-utf8.so
  fi
fi

mkdir -p cache
echo
echo "Done. Start:  python3 server.py   then open http://127.0.0.1:8765"
[ "$(uname)" = "Darwin" ] && echo "(or double-click start.command)"
exit 0
