# Установка и перенос на другой компьютер

## Что нужно
- Python 3.9+ (только стандартная библиотека; проверено на 3.9.6, macOS 26).
- `engine/luajit` и `engine/clib/lua-utf8.so` — **собраны под macOS arm64 (Apple Silicon)**.
- Интернет: трейд pathofexile.com, poe.ninja, poe2db.tw (для `tools/update_poe2db.py`), assets.pobb.in (картинки).

## Перенос
1. Скопировать папку `poe-optimizer` целиком (≈760 МБ, почти всё — `engine/pob2` с git-историей PoB).
   Можно без `cache/img/` (картинки скачаются заново) и без `.DS_Store`. **Обязательно** взять
   `cache/craft_links.json` — это ссылки пользователя на его крафты.
2. На Mac с Apple Silicon: если папка пришла через загрузку/AirDrop, снять карантин:
   `xattr -dr com.apple.quarantine engine/luajit engine/clib/lua-utf8.so`
3. Запуск: `python3 server.py` → http://127.0.0.1:8765. Проверка: загрузить билд (вкладка «Предметы» появится),
   во вкладке «Крафт» рассчитать любую цель; `python3 tests/regress.py` — все 8 целей `ok`.
4. Память Claude Code (`~/.claude/projects/...`) на новый компьютер не переезжает — всё нужное записано в
   `CLAUDE.md` и `docs/`. POESESSID попросить у пользователя заново (нигде не хранится).

## Другая архитектура (Intel Mac, Linux): пересобрать LuaJIT и lua-utf8
```bash
git clone https://github.com/LuaJIT/LuaJIT.git && cd LuaJIT && git checkout v2.1
make                                  # macOS: при ошибке линковщика новой SDK — MACOSX_DEPLOYMENT_TARGET=14.0 make
cp src/luajit ../poe-optimizer/engine/luajit && cd ..
git clone https://github.com/starwing/luautf8.git && cd luautf8
# macOS:
cc -O2 -fPIC -bundle -undefined dynamic_lookup -I../LuaJIT/src lutf8lib.c -o ../poe-optimizer/engine/clib/lua-utf8.so
# Linux:
cc -O2 -fPIC -shared -I../LuaJIT/src lutf8lib.c -o ../poe-optimizer/engine/clib/lua-utf8.so
```
(В ноябре 2026 LuaJIT собирался с macOS SDK 26.5: со SDK 27 падал линковщик.)
Windows не проверялся: в `server.py` (`Worker._start`) путь к `engine/luajit` без `.exe`, а `start.command` — bash.

## Готовые архивы (Releases) и Windows
- `.github/workflows/release.yml`: на каждый push в `main` GitHub Actions собирает архивы для Windows, macOS (arm64)
  и Linux, проверяет каждый `tools/smoke_test.py` (PoB грузит билд, крафт считается) и выкладывает их релизом
  `latest-build` (ссылка `releases/latest`).
- `tools/package.py <имя> [--python DIR]` — архив без git: приложение, PoB без картинок (`src` + `runtime/lua`,
  ≈50 МБ → zip ≈8 МБ), LuaJIT этой машины; на Windows ещё встроенный Python (embeddable 3.12, в `python312._pth`
  добавлен `..`, чтобы `server.py` видел `craft.py`).
- Windows: `engine/luajit.exe` + `engine/lua51.dll` (собраны `msvcbuild.bat`), `engine/clib/lua-utf8.dll` — из
  `engine/pob2/runtime/`. `server.py` сам выбирает `.exe`/`.dll`; все текстовые файлы читаются как UTF-8.
- Запуск для пользователя: `start.bat` / `start.command` (ставят `OPEN_BROWSER=1`: сервер открывает браузер, а если
  приложение уже запущено — просто открывает страницу).

## Path of Building
`engine/pob2` — git-клон `https://github.com/PathOfBuildingCommunity/PathOfBuilding-PoE2`. Если папку
переносили без него: `git clone <url> engine/pob2`. Обновить: `cd engine/pob2 && git pull` и перезапустить сервер.
Воркер запускается с `cwd = engine/pob2/src`, Lua-модули PoB берутся из `engine/pob2/runtime/lua`.

## Если что-то не работает
- «PoB worker exited» — LuaJIT не запускается (архитектура, карантин) или PoB обновился несовместимо: запустить
  руками `cd engine/pob2/src && ../../luajit -e 'package.path="../runtime/lua/?.lua;../runtime/lua/?/init.lua;./?.lua;"..package.path; package.cpath="../../clib/?.so;"..package.cpath' ../../worker.lua`
  и посмотреть ошибку (воркер ждёт JSON-команды на stdin; первая строка `@@...` — готов).
- 403 на `/api/*` — запрос не со страницы приложения (нужны `Origin`/`Referer` = адрес сервера).
- Порт занят — `PORT=8766 python3 server.py`.
