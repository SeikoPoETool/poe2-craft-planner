# Трейд, poe.ninja, poe2db, PoB: что известно и где грабли

## Официальный трейд (`https://www.pathofexile.com/api/trade2`)
- `POST /search/poe2/<лига>` → `{id, result: [ids], total}`; `GET /fetch/<до 10 id>?query=<id>&realm=poe2` → предметы.
- **Лимиты**: поиск ≈30 за 5 мин, fetch ≈50 за 5 мин; по 10 предметов за fetch. `RateLimiter` читает
  `X-Rate-Limit-*`, на 429 ждёт `Retry-After`. Одинаковые запросы кэшируются (`trade_call`).
- **Статус продавцов**: всегда `available` (= Instant Buyout and In Person). Не `online`/`any`.
- **Взвешенные группы** (`type: weight`) нормально работают только с POESESSID; без входа (или с устаревшим ключом)
  трейд отвечает «Query is too complex». Поиск замены тогда повторяет запрос с меньшим «бюджетом» сложности
  (200 → 110 → 70, самые ценные моды), сверка крафта — простой запрос минимумами.
- В запрос не идут моды, которые дают меньше 20% от самого ценного: они только сдвигают выдачу трейда, а найденное
  всё равно оценивает PoB.
- При ≥10000 совпадений результаты **не отсортированы глобально** — порог веса поднимается, пока совпадений меньше.
- Фильтр `explicit.stat_X` ловит и крафт-, и десекрейт-, и фрактур-версию мода; добавлять их id в ту же weight-группу
  — двойной счёт.
- Count не умеет «член группы = два стата сразу» (гибрид) → найденное проверяется по модам предмета (см.
  CRAFT_PLANNER, «Сверка с трейдом»).
- Поиск отдаёт **не больше 100 id** (самые дешёвые). Если Count пропускает много подделок (обычная +life/+mana
  вместо гибрида), настоящие лежат дальше: следующая сотня — новый поиск с
  `filters.trade_filters.filters.price = {"min": X}` от цены последнего проверенного. **Без `option` цена
  в Exalted-эквиваленте.** `run_craft_trade` берёт до 3
  сотен, пока не найдёт 5 подходящих; `exhausted` = проверено всё. «Похожих в продаже нет» — только если всё.
- JSON предмета: `explicitMods` — объекты `{description, hash, mods: [{name, tier: "P1"/"S2", level, magnitudes}]}`;
  текст с разметкой `[Tag|Text]` — чистить перед импортом в PoB. `extended.hashes` — какие строки к какому моду.
- Локальные статы брони (`% increased Energy Shield` на самом предмете) — отдельные id с `(local)` в тексте.
- Псевдо: `pseudo.pseudo_total_elemental_resistance`, `pseudo.pseudo_number_of_empty_prefix_mods` и т.д.
  (`gamedata.PSEUDO`) — работают в `and`/`count`/`weight`.
- Категории классов: `gamedata.TRADE_CAT` (`armour.helmet`, `accessory.amulet`, `weapon.wand`…).
- POESESSID пользователя: **не сохранять**. Сервер держит его только в памяти запроса; страница может помнить его
  в браузере, если пользователь поставил галочку.

## poe.ninja
- Цены валюты лиги по разделам `gamedata.NINJA_TYPES` (Currency, Essences, Runes, Abyss — кости, Ritual — омены,
  Verisium — Alloy, Breach — каталисты). Цена переводится в divine.
- Выбросы: цена, не подтверждённая объёмом торгов, заменяется (правило ×3 против `maxVolumeRate`).
- Нет цены → берётся 0.01 div и показывается предупреждение (`missingPrices`) — признак переименования в патче.

## poe2db.tw
- Веса выпадения модов (в файлах игры и PoB их нет: у PoB `weightVal = 1`): страница класса (`Helmets_int`,
  `Amulets`…), JSON `ModsView({...})`, список `normal`: `DropChance`, id мода — в ссылке `hover` (`Data/Mods/<id>`).
- Alloy: страница каждого Alloy, таблица «Class / Modifier / Pre/Suf / Required Level»; id — через текст мода
  в `ModItem.lua` PoB (моды `Alloy*`). Апостроф в адресе — как есть (`The_Runebinder's_Alloy`), не `%27`.
- Генератор: `python3 tools/update_poe2db.py` (превью `*.new.json`) → `--apply`. Пауза 1 с между запросами.
- poe2wiki.net блокирует автоматические запросы (Anubis) — для механик использовать poe2db.

## Path of Building (PoE2)
- `engine/pob2` — submodule `PathOfBuildingCommunity/PathOfBuilding-PoE2`. Обновление: `cd engine/pob2 && git pull`,
  затем перезапуск сервера.
- PoB не считает часть механик (энергия мета-гемов, многие строки дерева) — такие строки дают 0; для подбора
  предметов есть эвристики `HEUR_RULES` в worker.lua.
- Иконки узлов дерева/предметов: `assets.pobb.in/2/<name>.webp`, `cdn.poe2db.tw/image/...` (PoB хранит только .dds.zst).
