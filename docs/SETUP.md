# Установка из исходников и сборка

## Что нужно
- Python 3.9+ (только стандартная библиотека).
- git, make и компилятор C — чтобы собрать LuaJIT и модуль lua-utf8 (`install.sh`).
- Интернет: трейд pathofexile.com, poe.ninja, poe2db.tw (только для `tools/update_poe2db.py`), assets.pobb.in (картинки).

## Установка
```bash
git clone https://github.com/SeikoPoETool/poe2-craft-planner.git
cd poe2-craft-planner
./install.sh        # Path of Building (git submodule) + сборка engine/luajit и engine/clib/lua-utf8.so
python3 server.py   # http://127.0.0.1:8765, другой порт — PORT=8800
```
`install.sh` на macOS сам выбирает рабочий SDK, если установленный Command Line Tools не умеет линковать
(«tapi error: malformed file»).

Папку `cache/` можно удалять целиком, **кроме `cache/craft_links.json`** — это сохранённые ссылки на крафты.

## Готовые архивы и установщик
`.github/workflows/release.yml` на каждый push в `main`:
- собирает Windows (LuaJIT через `msvcbuild.bat`, `lua-utf8.dll` из `engine/pob2/runtime/`, встроенный Python 3.12),
  macOS (arm64) и Linux;
- `tools/package.py` кладёт в zip приложение, PoB без картинок (`src` + `runtime/lua`) и LuaJIT;
- `tools/installer.iss` (Inno Setup) делает `poe2-craft-planner-setup.exe`: установка без прав администратора,
  ярлыки, деинсталлятор, папка `cache` при обновлении сохраняется;
- каждую сборку проверяет `tools/smoke_test.py` (сервер стартует, PoB грузит билд, крафт считается), установщик —
  тихой установкой, самопроверкой и удалением;
- публикует всё релизом `latest-build`. Если macOS-машины у GitHub не нашлось, в релиз переносится прошлый архив.

Запуск для пользователя — `start.bat` / `start.command`: они ставят `OPEN_BROWSER=1`, и сервер сам открывает
браузер (а если приложение уже запущено — просто открывает страницу).

## Path of Building
`engine/pob2` — submodule `PathOfBuildingCommunity/PathOfBuilding-PoE2`. Обновить: `cd engine/pob2 && git pull`
и перезапустить сервер. Воркер запускается с `cwd = engine/pob2/src`, Lua-модули берутся из `engine/pob2/runtime/lua`.

## Если что-то не работает
- **«PoB worker exited»** — LuaJIT не запускается (не та архитектура, карантин macOS) или PoB обновился несовместимо.
  Запустить вручную и посмотреть ошибку:
  `cd engine/pob2/src && ../../luajit -e 'package.path="../runtime/lua/?.lua;../runtime/lua/?/init.lua;./?.lua;"..package.path; package.cpath="../../clib/?.so;"..package.cpath' ../../worker.lua`
  (первая строка `@@{"ready":true}` — воркер готов и ждёт JSON-команды).
- **403 на `/api/*`** — запрос не со страницы приложения (нужны `Origin`/`Referer` = адрес сервера).
- **macOS: «не удаётся проверить разработчика»** — `xattr -dr com.apple.quarantine .` в папке приложения
  (`start.command` делает это сам).
