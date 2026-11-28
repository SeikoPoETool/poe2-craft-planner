-- PoE2 optimizer worker: keeps Path of Building loaded and answers JSON commands.
-- Protocol: one JSON object per stdin line; every reply is a single stdout line "@@" .. json.
-- Run with cwd = engine/pob2/src.

dofile("../../boot_lib.lua")
local json = require("dkjson")
local tradeHelpers = LoadModule("Classes/TradeHelpers")
local QueryMods

local function reply(t) io.write("@@", json.encode(t), "\n"); io.flush() end

-- copied from PoB Classes/TradeQueryGenerator.lua (local there)
local function swapInverse(modLine)
  if modLine:match("increased") then modLine = modLine:gsub("([^ ]+) increased", "%1 reduced")
  elseif modLine:match("reduced") then modLine = modLine:gsub("([^ ]+) reduced", "%1 increased")
  elseif modLine:match("more") then modLine = modLine:gsub("([^ ]+) more", "%1 less")
  elseif modLine:match("less") then modLine = modLine:gsub("([^ ]+) less", "%1 more")
  elseif modLine:match("expires ([^ ]+) slower") then modLine = modLine:gsub("([^ ]+) slower", "%1 faster")
  elseif modLine:match("expires ([^ ]+) faster") then modLine = modLine:gsub("([^ ]+) faster", "%1 slower") end
  return modLine
end

local STATS = { "FullDPS", "CombinedDPS", "TotalDPS", "TotalDot", "AverageDamage", "Speed", "Life", "EnergyShield", "Mana",
  "Spirit", "Armour", "Evasion", "TotalEHP", "SecondMinimalMaximumHitTaken", "Str", "Dex", "Int", "EffectiveMovementSpeedMod",
  "BlockChance", "SpellBlockChance", "LifeRegen", "ManaRegen", "EnergyShieldRegen", "CritChance" }
local STAT_DEF = {}
for _, s in ipairs(data.powerStatList) do if s.stat then STAT_DEF[s.stat] = s end end

-- ---------------------------------------------------------------- beyond PoB: meta-gem energy
-- PoB2 cannot read "Meta Skills gain X% increased Energy" and does not model energy at all (a skill triggered by
-- Cast on Critical is calculated like a self-cast). With EXTRA.metaEnergy on, these lines become a "MetaEnergy" mod
-- (conditions after the number are parsed by PoB itself) and the DPS of a skill socketed in an energy-generating meta
-- gem is scaled by (100 + energy%) relative to the build as it is: triggers come as often as energy fills up.
local EXTRA = { metaEnergy = false }
local origParseMod = modLib.parseMod
local function energyLine(line)
  local num, rest = line:match("^[Mm]eta [Ss]kills gain (%d+)%% increased [Ee]nergy(.*)$")
  if not num then return nil end
  local l2, ex2 = origParseMod("10% increased Damage" .. rest)
  if not l2 or ex2 or not l2[1] then return nil end
  local m = copyTable(l2[1])
  m.name, m.flags, m.keywordFlags, m.value = "MetaEnergy", 0, 0, tonumber(num)
  return { m }
end
modLib.parseMod = function(line, isComb)
  local list, extra = origParseMod(line, isComb)
  if (not list or extra) and type(line) == "string" then
    local e = energyLine(line)
    if e then return e, nil end
  end
  return list, extra
end
-- trees parsed before this hook
for _, tree in pairs(main.tree or {}) do
  for _, node in pairs(tree.nodes or {}) do
    for _, l in ipairs(node.sd or {}) do
      if l:find("Meta Skills gain", 1, true) then pcall(tree.ProcessStats, tree, node); break end
    end
  end
end
local function metaGemOf(env)
  local ms = env and env.player and env.player.mainSkill
  local sg = ms and ms.socketGroup
  if not sg then return nil end
  for _, gem in ipairs(sg.gemList or {}) do
    local ge = gem.gemData and gem.gemData.grantedEffect
    if ge and ge.skillTypes and ge.skillTypes[SkillType.GeneratesEnergy] and gem.enabled ~= false then return gem, ge end
  end
end
local function gemEnergy(gem, ge)
  local total = 0
  for _, ss in ipairs(ge.statSets or {}) do
    for i, st in ipairs(ss.stats or {}) do
      if st == "energy_generated_+%" then
        local lv = ss.levels and ss.levels[gem.level or 1]
        if lv and type(lv[i]) == "number" then total = total + lv[i] end
      end
    end
    for _, cs in ipairs(ss.constantStats or {}) do
      if cs[1] == "energy_generated_+%" and type(cs[2]) == "number" then total = total + cs[2] end
    end
  end
  return total
end
local function wrapPerform()
  local c = build.calcsTab.calcs
  if not c or c.__energyWrapped then return end
  c.__energyWrapped = true
  local orig = c.perform
  c.perform = function(env, ...)
    local r = orig(env, ...)
    pcall(function()
      local gem, ge = metaGemOf(env)
      local out = env.player.output
      if gem then
        out.MetaEnergyGem, out.MetaEnergyGemValue = ge.name, gemEnergy(gem, ge)
        -- supports in the meta gem's group ("Supported Skills gain X% increased Energy", e.g. Boundless Energy)
        local sup = 0
        for _, sg in ipairs(env.player.mainSkill.socketGroup.gemList or {}) do
          local sge = sg.gemData and sg.gemData.grantedEffect
          if sge and sge.support and sg.enabled ~= false then sup = sup + gemEnergy(sg, sge) end
        end
        out.MetaEnergySupports = sup
        out.MetaEnergyMods = env.player.modDB:Sum("INC", nil, "MetaEnergy")
        out.MetaEnergyInc = out.MetaEnergyGemValue + sup + out.MetaEnergyMods
      end
    end)
    return r
  end
end
local ENERGY_SCALED = { FullDPS = true, CombinedDPS = true, TotalDPS = true, TotalDot = true, TotalDotDPS = true }

local function statVal(output, stat)
  if stat == "FullDPS" and (output.FullDPS or 0) == 0 then stat = "CombinedDPS" end
  local def = STAT_DEF[stat] or { stat = stat }
  local v = data.powerStatList.GetFromOutput(output, def, true)
  if EXTRA.metaEnergy and ENERGY_SCALED[stat] and output.MetaEnergyInc and type(v) == "number" and v ~= math.huge then
    local ref = build.calcsTab.mainOutput and build.calcsTab.mainOutput.MetaEnergyInc
    if ref then v = v * (100 + output.MetaEnergyInc) / (100 + ref) end
  end
  return v, def
end

local function pick(output)
  local r = {}
  for _, s in ipairs(STATS) do
    r[s] = (statVal(output, s))
  end
  return r
end

local function weightedRatio(baseOutput, newOutput, statWeights)
  local total = 0
  for _, sw in ipairs(statWeights) do
    local b, def = statVal(baseOutput, sw.stat)
    local n = statVal(newOutput, sw.stat)
    local ratio
    if b == math.huge then ratio = 0
    elseif n == math.huge then ratio = data.misc.maxStatIncrease
    else ratio = math.min(n / ((b ~= 0) and b or 1), data.misc.maxStatIncrease) end
    if def.transform then ratio = def.transform(ratio) end
    total = total + ratio * (sw.weightMult or 1)
  end
  return total
end

local function recalc() build.buildFlag = true; runCallback("OnFrame") end

local function info()
  local skills = {}
  for i, g in ipairs(build.skillsTab.socketGroupList) do
    local names = {}
    for _, gem in ipairs(g.gemList or {}) do
      if gem.grantedEffect and not gem.grantedEffect.support then names[#names + 1] = gem.grantedEffect.name end
    end
    skills[#skills + 1] = { index = i, label = g.displayLabel or g.label or table.concat(names, " + "), enabled = g.enabled ~= false, slot = g.slot, actives = names }
  end
  local slots = {}
  for _, slotName in ipairs({ "Weapon 1", "Weapon 2", "Helmet", "Body Armour", "Gloves", "Boots", "Amulet", "Ring 1", "Ring 2", "Ring 3", "Belt", "Flask 1", "Flask 2", "Charm 1", "Charm 2", "Charm 3" }) do
    local slot = build.itemsTab.slots[slotName]
    if slot then
      local item = build.itemsTab.items[slot.selItemId]
      local catStr, cat = tradeHelpers.getTradeCategory(slotName, item)
      slots[#slots + 1] = { slot = slotName, item = item and item.name or nil, rarity = item and item.rarity or nil,
        base = item and item.baseName or nil, category = catStr, queryCategory = cat }
    end
  end
  for nodeId, slot in pairs(build.itemsTab.sockets or {}) do
    local item = build.itemsTab.items[slot.selItemId]
    if item and build.spec.allocNodes[nodeId] then
      slots[#slots + 1] = { slot = slot.slotName, item = item.name, rarity = item.rarity, base = item.baseName, category = "jewel", queryCategory = "BaseJewel", nodeId = nodeId }
    end
  end
  -- allocated jewel sockets (with or without a jewel) for the jewel search
  local jewelSockets = {}
  for nodeId, slot in pairs(build.itemsTab.sockets or {}) do
    if build.spec.allocNodes[nodeId] then
      local item = build.itemsTab.items[slot.selItemId]
      local node = build.spec.nodes[nodeId]
      jewelSockets[#jewelSockets + 1] = { slot = slot.slotName, nodeId = nodeId, item = item and (item.title or item.name) or nil,
        base = item and item.baseName or nil, rarity = item and item.rarity or nil,
        name = node and node.dn or nil }
    end
  end
  table.sort(jewelSockets, function(a, b) return a.nodeId < b.nodeId end)
  return {
    className = build.spec.curClassName, ascendancy = build.spec.curAscendClassName, level = build.characterLevel,
    mainSocketGroup = build.mainSocketGroup, skills = skills, slots = slots, stats = pick(build.calcsTab.mainOutput),
    jewelSockets = jewelSockets,
  }
end

local findSlot
local EXTRA_STATS = { "AverageHit", "CritMultiplier", "HitChance", "FireResist", "ColdResist", "LightningResist", "ChaosResist",
  "FireResistOverCap", "ColdResistOverCap", "LightningResistOverCap", "EvadeChance", "SpiritUnreserved", "ManaUnreserved",
  "LifeUnreserved", "PhysicalDamageReduction", "PhysicalMaximumHitTaken", "FireMaximumHitTaken", "ColdMaximumHitTaken",
  "LightningMaximumHitTaken", "ChaosMaximumHitTaken", "IgniteDPS", "TotalDotDPS", "ManaCost", "ManaPerSecondCost", "AreaOfEffectRadiusMetres",
  "MetaEnergyInc", "MetaEnergyGem", "MetaEnergyGemValue", "MetaEnergyMods", "MetaEnergySupports" }

local function lines(item, key, kind)
  local t = {}
  for _, l in ipairs(item[key] or {}) do
    t[#t + 1] = { text = l.line, kind = kind, crafted = l.crafted or nil, fractured = l.fractured or nil,
      desecrated = l.desecrated or nil, rune = l.rune or nil }
  end
  return t
end

local function itemView(item)
  if not item then return nil end
  local mods = {}
  for _, part in ipairs({ { "enchantModLines", "enchant" }, { "runeModLines", "rune" }, { "implicitModLines", "implicit" }, { "explicitModLines", "explicit" } }) do
    for _, l in ipairs(lines(item, part[1], part[2])) do mods[#mods + 1] = l end
  end
  local runes = {}
  for _, r in ipairs(item.runes or {}) do runes[#runes + 1] = type(r) == "table" and (r.name or r.baseName) or r end
  local props = {}
  local ad = item.armourData or {}
  for _, k in ipairs({ { "Armour", "Armour" }, { "Evasion", "Evasion Rating" }, { "EnergyShield", "Energy Shield" }, { "Ward", "Runic Ward" } }) do
    if (ad[k[1]] or 0) > 0 then props[#props + 1] = { label = k[2], value = tostring(math.floor(ad[k[1]] + 0.5)) } end
  end
  if item.base and item.base.armour and item.base.armour.BlockChance then props[#props + 1] = { label = "Block chance", value = item.base.armour.BlockChance .. "%" } end
  local wd = item.weaponData and item.weaponData[1]
  if wd then
    if (wd.PhysicalMax or 0) > 0 then props[#props + 1] = { label = "Physical Damage", value = math.floor(wd.PhysicalMin) .. "-" .. math.floor(wd.PhysicalMax) } end
    for _, el in ipairs({ "Fire", "Cold", "Lightning", "Chaos" }) do
      if (wd[el .. "Max"] or 0) > 0 then props[#props + 1] = { label = el .. " Damage", value = math.floor(wd[el .. "Min"]) .. "-" .. math.floor(wd[el .. "Max"]) } end
    end
    if wd.CritChance and wd.CritChance > 0 then props[#props + 1] = { label = "Critical Hit Chance", value = string.format("%.2f%%", wd.CritChance) } end
    if wd.AttackRate and wd.AttackRate > 0 then props[#props + 1] = { label = "Attacks per Second", value = string.format("%.2f", wd.AttackRate) } end
  end
  local rq = item.requirements or {}
  local req = { level = rq.level, str = (rq.strMod or 0) > 0 and rq.strMod or nil, dex = (rq.dexMod or 0) > 0 and rq.dexMod or nil, int = (rq.intMod or 0) > 0 and rq.intMod or nil }
  return { name = item.title or item.name, base = item.baseName, rarity = item.rarity, itemLevel = item.itemLevel,
    itemClass = item.base and item.base.type or nil, props = props, reqs = req,
    quality = item.quality, corrupted = item.corrupted or nil, sanctified = item.sanctified or nil, mods = mods, runes = runes,
    sockets = item.itemSocketCount, req = item.requirements and item.requirements.level or nil, raw = item.raw }
end

local function skillDps()
  -- DPS of every active skill, like pobb's skill list; restores the saved main skill
  local savedGroup = build.mainSocketGroup
  local savedActive = {}
  for i, g in ipairs(build.skillsTab.socketGroupList) do savedActive[i] = g.mainActiveSkill end
  local res = {}
  for i, g in ipairs(build.skillsTab.socketGroupList) do
    res[i] = {}
    if g.enabled ~= false then
      for a = 1, #(g.displaySkillList or {}) do
        build.mainSocketGroup = i; g.mainActiveSkill = a; recalc()
        local o = build.calcsTab.mainOutput
        res[i][a] = { dps = o.CombinedDPS or 0, hit = o.TotalDPS or 0, dot = o.TotalDotDPS or 0 }
      end
    end
  end
  build.mainSocketGroup = savedGroup
  for i, g in ipairs(build.skillsTab.socketGroupList) do g.mainActiveSkill = savedActive[i] end
  recalc()
  return res
end

local function allStats(output)
  local r = pick(output)
  for _, k in ipairs(EXTRA_STATS) do r[k] = output[k] end
  return r
end

local function itemsView(base)
  local items = {}
  for _, s in ipairs(base.slots) do
    local slot = findSlot(s.slot)
    local it = slot and build.itemsTab.items[slot.selItemId]
    if it then local v = itemView(it); v.slot = s.slot; v.category = s.category; items[#items + 1] = v end
  end
  return items
end

-- problems an item swap causes: unmet attribute requirements, a resistance dropping below its cap
local ATTR_RU = { Str = "сила", Dex = "ловкость", Int = "интеллект" }
local RES_RU = { Fire = "огонь", Cold = "холод", Lightning = "молния", Chaos = "хаос" }
local function warnings(b, a)
  local w = {}
  for _, at in ipairs({ "Str", "Dex", "Int" }) do
    local req, have = a["Req" .. at] or 0, a[at] or 0
    if req > have and not ((b["Req" .. at] or 0) > (b[at] or 0)) then
      w[#w + 1] = string.format("не хватает атрибута: %s %d, нужно %d", ATTR_RU[at], have, req)
    end
  end
  local ci = false
  pcall(function() ci = build.calcsTab.mainEnv.modDB:Flag(nil, "ChaosInoculation") end)
  for _, el in ipairs({ "Fire", "Cold", "Lightning", "Chaos" }) do
    local bv, av = b[el .. "Resist"] or 0, a[el .. "Resist"] or 0
    local miss = a["Missing" .. el .. "Resist"] or 0
    -- chaos: only when it was capped before (most builds never cap it); CI makes it irrelevant
    local relevant = el ~= "Chaos" or (not ci and (b["MissingChaosResist"] or 1) == 0)
    if relevant and av < bv and miss > 0 then
      w[#w + 1] = string.format("резист %s падает: %d%% → %d%%", RES_RU[el], bv, av)
    end
  end
  return w
end

local heurCompare  -- indirect mods of the new item vs the current one, defined with the rules further down
local function compareItem(params)
  local slot = assert(findSlot(params.slot), "unknown slot")
  local item = new("Item"):Item(params.raw)
  if not item.base then error("не удалось разобрать предмет: неизвестная база") end
  item:BuildModList()
  local calcFunc, baseOutput = build.calcsTab:GetMiscCalculator()
  local out = calcFunc({ repSlotName = slot.slotName, repItem = item })
  local cur = build.itemsTab.items[slot.selItemId]
  local okh, heur = pcall(heurCompare, slot, item, cur, calcFunc)
  return { before = allStats(baseOutput), after = allStats(out), item = itemView(item), current = itemView(cur), warn = warnings(baseOutput, out),
    heur = okh and heur or nil }
end

local uniqueJewelCatalog  -- defined further down
local function fullView()
  local base = info()
  local dps = skillDps()
  local groups = {}
  for i, g in ipairs(build.skillsTab.socketGroupList) do
    local gems = {}
    for _, gem in ipairs(g.gemList or {}) do
      local gd = gem.gemData
      gems[#gems + 1] = { name = gem.nameSpec or (gd and gd.name), level = gem.level, quality = gem.quality,
        enabled = gem.enabled ~= false, color = gem.color, support = gd and gd.grantedEffect and gd.grantedEffect.support or false,
        tags = gd and gd.tagString or nil }
    end
    local actives = {}
    for a, as in ipairs(g.displaySkillList or {}) do
      actives[#actives + 1] = { index = a, name = as.activeEffect.grantedEffect.name, dps = dps[i] and dps[i][a] }
    end
    groups[#groups + 1] = { index = i, label = g.displayLabel or g.label, enabled = g.enabled ~= false, slot = g.slot,
      mainActiveSkill = g.mainActiveSkill, includeInFullDPS = g.includeInFullDPS or false, gems = gems, actives = actives }
  end
  local items = {}
  for _, s in ipairs(base.slots) do
    local slot = findSlot(s.slot)
    local it = slot and build.itemsTab.items[slot.selItemId]
    if it then
      local v = itemView(it); v.slot = s.slot; v.category = s.category
      items[#items + 1] = v
    end
  end
  local o = build.calcsTab.mainOutput
  local extra = {}
  for _, k in ipairs(EXTRA_STATS) do extra[k] = o[k] end
  base.extra = extra
  base.groups = groups
  base.items = items
  local uj = {}
  for _, u in ipairs(uniqueJewelCatalog()) do uj[#uj + 1] = { name = u.name, base = u.base, variants = u.variants } end
  base.uniqueJewels = uj
  return base
end

findSlot = function(name)
  local s = build.itemsTab.slots[name]
  if s then return s end
  for _, slot in pairs(build.itemsTab.sockets or {}) do if slot.slotName == name then return slot end end
end

-- PoB TradeQueryGenerator:GenerateModWeights, headless
-- "+N% to Fire Resistance", "Bonded: +N% to all Elemental Resistances", "+N% to Cold and Chaos Resistances" ...
-- (not maximum resistances, not penetration / enemy resistance lines)
local function isResLine(line)
  local l = line:gsub("{[^}]*}", ""):gsub("^Bonded: ", "")
  if l:find("maximum") then return false end
  return l:match("^[+-]?[%d%.]+%%? to [%a%s,]-Resistances?$") ~= nil
end

-- item copy without its resistance lines (keeps the "Implicits: N" count consistent)
local function stripRes(raw)
  local out, implLeft, implIdx = {}, 0, nil
  for l in (raw .. "\n"):gmatch("([^\n]*)\n") do
    local n = l:match("^Implicits: (%d+)")
    if n then implLeft = tonumber(n); out[#out + 1] = l; implIdx = #out
    else
      local isImpl = implLeft > 0
      if isImpl then implLeft = implLeft - 1 end
      if isResLine(l) then
        if isImpl and implIdx then out[implIdx] = "Implicits: " .. (tonumber(out[implIdx]:match("%d+")) - 1) end
      else out[#out + 1] = l end
    end
  end
  local item = new("Item"):Item(table.concat(out, "\n"))
  item:BuildModList()
  return item
end

-- ---------------------------------------------------------------- beyond PoB: indirect mods (low confidence)
-- Some mods make the main skill stronger in ways PoB does not count: PoB cannot read the line (dodge roll distance,
-- Archon, Infusion conditions), or reads it but the single-target DPS of the main skill does not change (projectile
-- speed, extra projectiles, pierce, area). Each rule turns such a line into "≈ N% of main-skill DPS".
-- Game numbers (poe2db): Cast on Dodge gains energy per metre rolled, base roll 3.7 m; an Archon buff is 25% more
-- damage for 10 s, then 20 s of recovery; projectile speed does not add range in PoE2. Clear / comfort coefficients
-- are judgement calls. A rule applies only when the build has the mechanic and PoB itself gives the line no DPS;
-- the result is kept apart from PoB's numbers and marked as low (or medium) confidence.
local DODGE_BASE_M = 3.7
local ARCHON_MORE, ARCHON_DUR, ARCHON_REC = 25, 10, 20

local function heurLines(item)
  local t = {}
  for _, k in ipairs({ "enchantModLines", "runeModLines", "implicitModLines", "explicitModLines" }) do
    for _, l in ipairs(item and item[k] or {}) do if l.line then t[#t + 1] = l.line end end
  end
  return t
end

local function heurContext()
  local env = build.calcsTab.mainEnv
  local player = env and env.player
  local ms = player and player.mainSkill
  local c = { types = {}, gens = {}, lines = {}, nCommand = 0, ar = { dur = 0, rec = 0, eff = 0 } }
  if not ms then return c end
  local ge = ms.activeEffect and ms.activeEffect.grantedEffect
  c.types = ms.skillTypes or {}
  c.minion = env.minion ~= nil
  c.consumesInf = ((ge and ge.description) or ""):find("Infusion") ~= nil
  local _, mge = metaGemOf(env)
  c.meta = mge and mge.name
  c.invocation = mge and mge.skillTypes and mge.skillTypes[SkillType.Invocation] or false
  local okf, fk = pcall(function() return ms.skillModList:Flag(ms.skillCfg, "ForkOnce") end)
  c.forks = okf and fk
  for _, g in ipairs(build.skillsTab.socketGroupList) do
    if g.enabled ~= false then
      for _, gem in ipairs(g.gemList or {}) do
        local e = gem.gemData and gem.gemData.grantedEffect
        if e and gem.enabled ~= false and not e.support then
          local st, d = e.skillTypes or {}, e.description or ""
          if st[SkillType.GeneratesInfusion] then
            for _, el in ipairs({ "Fire", "Cold", "Lightning" }) do
              if st[SkillType[el]] then c.gens[el] = c.gens[el] or e.name end
            end
          end
          if st[SkillType.GeneratesRemnants] then c.remnants = c.remnants or e.name end
          if st[SkillType.CommandsMinions] then c.nCommand = c.nCommand + 1 end
          if st[SkillType.Offering] then c.offering = true end
          if d:find("Glory") then c.glory = c.glory or e.name end
          if d:find("[Cc]onsum[^.]*[Cc]harge") then c.charges = c.charges or e.name end
          if e.name == "Cast on Dodge" then c.codAny = true end
          if e.name == "Archon of Chayula" then c.archonSrc, c.archonKind = e.name, "any" end
        end
      end
    end
  end
  -- passive and item lines of the build: PoB reads none of the Archon / dodge roll lines, so they are summed here
  for _, node in pairs(build.spec.allocNodes or {}) do
    for _, l in ipairs(node.sd or {}) do c.lines[#c.lines + 1] = l end
  end
  for _, item in pairs(player.itemList or {}) do
    for _, l in ipairs(heurLines(item)) do c.lines[#c.lines + 1] = l end
  end
  local ar = c.ar
  for _, l in ipairs(c.lines) do
    if l:match("[Gg]ain %a+ Archon") or l:match("^Allocates Archon of the") then c.archonSrc, c.archonKind = l, "spell" end
    if l:find("[Gg]ain Archon of Undeath") or l:find("^Allocates Archon of Undeath") then c.archonSrc, c.archonKind = c.archonSrc or l, c.archonKind or "minion" end
    local n = tonumber(l:match("(%d+)%% increased Archon Buff duration")); if n then ar.dur = ar.dur + n end
    n = tonumber(l:match("Archon recovery period expires (%d+)%% faster")); if n then ar.rec = ar.rec + n end
    n = tonumber(l:match("Archon recovery period expires (%d+)%% slower")); if n then ar.rec = ar.rec - n end
    n = tonumber(l:match("(%d+)%% increased effect of Archon Buffs on you")); if n then ar.eff = ar.eff + n end
    n = tonumber(l:match("(%d+)%% reduced effect of Archon Buffs on you")); if n then ar.eff = ar.eff - n end
    if l:find("Archon Buffs have no recovery period") then ar.noRec = true end
    n = tonumber(l:match("^%+?([%d%.]+) metres? to Dodge Roll distance$")); if n then c.dodgeExtra = (c.dodgeExtra or 0) + n end
  end
  -- elemental Archons end on any non-instant attack: they help spells; Archon of Undeath helps minions
  c.archonApplies = c.archonKind == "any" or (c.archonKind == "spell" and c.types[SkillType.Spell]) or (c.archonKind == "minion" and c.minion) or false
  return c
end

-- average damage multiplier of an Archon buff: 25% more for D seconds out of every D + R (re-gained as soon as allowed)
local function archonAvg(ar)
  local D = ARCHON_DUR * math.max(0, 1 + ar.dur / 100)
  local R = ar.noRec and 0 or ARCHON_REC / math.max(0.1, 1 + ar.rec / 100)
  return 1 + ARCHON_MORE / 100 * math.max(0, 1 + ar.eff / 100) * D / math.max(0.01, D + R)
end
local function archonGain(c, key, n)
  local a2 = { dur = c.ar.dur, rec = c.ar.rec, eff = c.ar.eff, noRec = c.ar.noRec }
  a2[key] = a2[key] + n
  return (archonAvg(a2) / archonAvg(c.ar) - 1) * 100
end
local function archonWhy(c, what)
  local D = ARCHON_DUR * (1 + c.ar.dur / 100)
  local R = c.ar.noRec and 0 or ARCHON_REC / math.max(0.1, 1 + c.ar.rec / 100)
  return string.format("%s. PoB не считает Archon: 25%% more урона на %.0f с, затем %.0f с перезарядки (аптайм %.0f%%), считаю, что бафф берётся сразу, как только можно", what, D, R, D / (D + R) * 100)
end

local function typeOk(c, line)
  if line:find("Spell") and not c.types[SkillType.Spell] then return false end
  if (line:find("Attack") or line:find("with this Weapon")) and not c.types[SkillType.Attack] then return false end
  return true
end
local function projOk(c, line) return c.types[SkillType.Projectile] and typeOk(c, line) end
local function areaOk(c, line) return c.types[SkillType.Area] and typeOk(c, line) end
local EXTRA_PROJ = "больше снарядов — шире покрытие: зачистка (DPS по одной цели PoB уже посчитал)"
local AREA = "больше площадь (радиус растёт как корень из площади): быстрее зачистка, DPS по одной цели не меняется"

local HEUR_RULES = {
  -- projectiles
  { pat = "^(%d+)%% increased Projectile Speed", f = function(c, x, l)
      if projOk(c, l) then return x[1] * 0.05, "снаряды быстрее долетают (в PoE2 скорость не прибавляет дальности): комфорт и надёжность попаданий" end end },
  { pat = "^(%d+)%% chance for Spell Skills to fire (%d+) additional Projectiles", f = function(c, x, l)
      if projOk(c, l) then return x[1] / 100 * x[2] * 3, EXTRA_PROJ end end },
  { pat = "^%+?(%d+)%% Surpassing chance to fire an additional Projectile", f = function(c, x, l)
      if projOk(c, l) then return x[1] / 100 * 3, EXTRA_PROJ end end },
  { pat = "^Projectile Attacks have a (%d+)%% chance to fire two additional Projectiles while moving", f = function(c, x, l)
      if projOk(c, l) then return x[1] / 100 * 2 * 0.5 * 3, EXTRA_PROJ .. "; только в движении — считаю половину времени" end end },
  { pat = "^Grenade Skills Fire an additional Projectile", f = function(c)
      if c.types[SkillType.Grenade] then return 3, EXTRA_PROJ end end },
  { pat = "^Projectiles have (%d+)%% chance for an additional Projectile when Forking", f = function(c, x, l)
      if projOk(c, l) and c.forks then return x[1] / 100 * 2, "доп. снаряд при форке: шире покрытие" end end },
  { pat = "^(%d+)%% chance to Pierce an Enemy", f = function(c, x, l)
      if projOk(c, l) then return x[1] / 100 * 2, "пробивание: снаряд проходит сквозь пачку — зачистка" end end },
  { pat = "^Projectiles have (%d+)%% chance to Chain an additional time from terrain", f = function(c, x, l)
      if projOk(c, l) then return x[1] / 100 * 1.5, "отскок от стен: зачистка" end end },
  { pat = "^Attacks Chain an additional time", f = function(c, x, l)
      if projOk(c, l) then return 2, "доп. цепь: зачистка" end end },
  -- area
  { pat = "^(%d+)%% increased Area of Effect$", f = function(c, x, l) if areaOk(c, l) then return x[1] * 0.06, AREA end end },
  { pat = "^(%d+)%% increased Area of Effect for Attacks$", f = function(c, x, l) if areaOk(c, l) then return x[1] * 0.06, AREA end end },
  { pat = "^Spell Skills have (%d+)%% increased Area of Effect$", f = function(c, x, l) if areaOk(c, l) then return x[1] * 0.06, AREA end end },
  { pat = "^(%d+)%% increased Area of Effect if you've Killed Recently$", f = function(c, x, l)
      if areaOk(c, l) then return x[1] * 0.06 * 0.8, AREA .. "; при убийствах — 80% времени" end end },
  -- dodge roll + Cast on Dodge
  { pat = "^%+?([%d%.]+) metres? to Dodge Roll distance(.*)$", f = function(c, x)
      local cond = type(x[2]) == "string" and x[2] or ""
      local k = cond == "" and 1 or (cond:find("haven't") and 0.3 or 0.9)
      local base = DODGE_BASE_M + (c.dodgeExtra or 0)
      local energy = x[1] / base * 100 * k
      local cnd = cond == "" and "" or (k < 0.5 and "; условие «не перекатывались недавно» редко выполняется при спаме переката — 30%" or "; условие почти всегда выполняется — 90%")
      if c.meta == "Cast on Dodge" then
        return energy * 0.8, string.format("Cast on Dodge копит энергию за метр переката (2 энергии/м): +%s м к %.1f м = +%.0f%% энергии → основной скилл триггерится чаще; ×0.8, если упор не только в энергию%s", tostring(x[1]), base, energy / k, cnd)
      elseif c.codAny then
        return energy * 0.25, string.format("Cast on Dodge в билде, но триггерит не основной скилл: +%.0f%% энергии вспомогательному скиллу, считаю четверть%s", energy / k, cnd)
      end end },
  -- Archon
  { pat = "^(%d+)%% increased Archon Buff duration", f = function(c, x)
      if c.archonApplies then return archonGain(c, "dur", x[1]), archonWhy(c, "Дольше Archon-бафф") end end },
  { pat = "^Archon recovery period expires (%d+)%% faster", f = function(c, x)
      if c.archonApplies then return archonGain(c, "rec", x[1]), archonWhy(c, "Короче перезарядка Archon") end end },
  { pat = "^(%d+)%% increased effect of Archon Buffs on you", f = function(c, x)
      if c.archonApplies then return archonGain(c, "eff", x[1]), archonWhy(c, "Сильнее Archon-бафф") end end },
  { pat = "^(%d+)%% to gain Archon of Undeath when you create an Offering", f = function(c, x)
      if c.minion and c.offering and not c.archonSrc then return (archonAvg(c.ar) - 1) * 100 * math.min(1, x[1] / 50), archonWhy(c, "Даёт Archon of Undeath (миньоны) при Offering") end end },
  { pat = "^(%d+)%% chance to gain Archon of Undeath when you use a Command skill", f = function(c, x)
      if c.minion and c.nCommand > 0 and not c.archonSrc then return (archonAvg(c.ar) - 1) * 100 * math.min(1, x[1] / 50), archonWhy(c, "Даёт Archon of Undeath (миньоны) при Command-скилле") end end },
  -- Elemental Infusions (20 s, up to 3 of each type)
  { pat = "^%+?(%d+) to maximum number of Elemental Infusions", f = function(c, x)
      if c.consumesInf then return x[1] * 2, "основной скилл тратит Infusion: больше запас (база 3 каждого типа) — реже каст без усиления" end end },
  { pat = "^(%d+)%% chance when collecting an Elemental Infusion to gain an additional Elemental Infusion of the same type", f = function(c, x)
      if c.consumesInf and next(c.gens) then return x[1] * 0.1, "основной скилл тратит Infusion: их становится больше — чаще усиленный каст" end end },
  { pat = "^Skills have (%d+)%% chance to not remove Elemental Infusions but still count as consuming them$", f = function(c, x)
      if c.consumesInf then return x[1] * 0.1, "основной скилл тратит Infusion: часть кастов бесплатна — чаще усиленный каст" end end },
  { pat = "^Skills have (%d+)%% chance to not remove Elemental Infusions but still count as consuming them if you've lost an Archon Buff", f = function(c, x)
      if c.consumesInf and c.archonApplies then return x[1] * 0.05, "как сохранение Infusion, но только после потери Archon — половина времени" end end },
  { pat = "^(%d+)%% increased (%a+) Damage if you've collected an? (%a+) Infusion in the last 8 seconds", conf = "mid", f = function(c, x, l, pob)
      local src = c.gens[x[3]]
      if src then return pob(x[1] .. "% increased " .. x[2] .. " Damage") * 0.75,
        string.format("PoB не знает этого условия; %s создаёт %s Infusion — посчитано в PoB как обычный «%d%% increased %s Damage» × 75%% времени", src, x[3], x[1], x[2]) end end },
  { pat = "^Every second, gain a Verisium Infusion", f = function(c)
      if c.consumesInf then return 3, "Verisium Infusion заменяет любую стихийную: основной скилл чаще усилен" end end },
  -- spell ailments / minions / Invocation
  { pat = "^(%d+)%% increased Magnitude of Elemental Ailments you inflict with Spells", conf = "mid", f = function(c, x, l, pob)
      if c.types[SkillType.Spell] then return pob(x[1] .. "% increased Magnitude of Ailments you inflict"), "PoB не читает «with Spells»; основной скилл — заклинание: посчитано в PoB как «Magnitude of Ailments»" end end },
  { pat = "^(%d+)%% increased Minion Damage per different Command Skill used in the past 15 seconds", conf = "mid", f = function(c, x, l, pob)
      if c.minion and c.nCommand > 0 then
        local n = math.min(c.nCommand, 3)
        return pob("Minions deal " .. (x[1] * n) .. "% increased Damage") * 0.5, string.format("в билде Command-скиллов: %d; посчитано в PoB как +%d%% урона миньонов × 50%% времени", c.nCommand, x[1] * n) end end },
  { pat = "^Invocated Spells have (%d+)%% chance to consume half as much Energy", conf = "mid", f = function(c, x)
      if c.invocation then return (1 / (1 - x[1] / 200) - 1) * 100 * 0.8, "основной скилл в Invocation: в среднем на каст нужно меньше энергии → касты чаще; ×0.8, если упор не только в энергию" end end },
  -- Glory, Remnants, charges
  { pat = "^(%d+)%% increased Glory generation", f = function(c, x)
      if c.glory then return x[1] * 0.05, "Glory для «" .. c.glory .. "» копится быстрее — скилл чаще доступен" end end },
  { pat = "^(%d+)%% chance for Skills to retain 40%% of Glory on use", f = function(c, x)
      if c.glory then return x[1] * 0.04, "после «" .. c.glory .. "» часть Glory остаётся — скилл чаще доступен" end end },
  { pat = "^Remnants can be collected from (%d+)%% further away", f = function(c, x)
      if c.remnants then return x[1] * 0.02, "остатки (" .. c.remnants .. ") собираются с большего расстояния: комфорт" end end },
  { pat = "^(%d+)%% chance for Remnants you pick up to count as picking up an additional Remnant", f = function(c, x)
      if c.remnants then return x[1] * 0.05, "остатки (" .. c.remnants .. ") иногда считаются за два" end end },
  { pat = "^Remnants you create have (%d+)%% increased effect", f = function(c, x)
      if c.remnants then return x[1] * 0.05, "эффект остатков (" .. c.remnants .. ") сильнее; PoB его в DPS не переводит" end end },
  { pat = "^Skills have (%d+)%% chance to not remove Charges but still count as consuming them", f = function(c, x)
      if c.charges then return x[1] * 0.1, "«" .. c.charges .. "» тратит заряды: часть трат бесплатна" end end },
}

-- one line → { pct, why, conf } or nil. pob(line) = PoB's own DPS change of the main skill from that line (%);
-- a line PoB already values is left to PoB.
local function heurLine(c, line, pob)
  local l = (line:gsub("^%s+", ""):gsub("%s+$", ""))
  for ri, r in ipairs(HEUR_RULES) do
    local caps = { l:match(r.pat) }
    if caps[1] ~= nil then
      for i, v in ipairs(caps) do caps[i] = tonumber(v) or v end
      if math.abs(pob(l)) > 0.05 then return nil end
      local ok, pct, why = pcall(r.f, c, caps, l, pob)
      if ok and type(pct) == "number" and pct == pct and math.abs(pct) >= 0.01 then
        return { pct = pct, why = why, conf = r.conf or "low", rule = ri }
      end
      return nil
    end
  end
end

local function heurItem(c, item, pob)
  local res = { pct = 0, lines = {} }
  if not (c and item) then return res end
  for _, line in ipairs(heurLines(item)) do
    local h = heurLine(c, line, pob)
    if h then
      res.pct = res.pct + h.pct
      res.lines[#res.lines + 1] = { t = line, pct = h.pct, why = h.why, conf = h.conf, rule = h.rule }
    end
  end
  return res
end

-- PoB's DPS change (%) of the main skill from one line alone, on a blank item of this base in this slot
local function pobProbe(slotName, baseName, cf)
  local tester = new("Item"):Item("Rarity: RARE\nStat Tester\n" .. baseName)
  if not tester.base then return function() return 0 end end
  tester.explicitModLines = {}; tester:BuildAndParseRaw()
  local b = statVal(cf({ repSlotName = slotName, repItem = tester }), "FullDPS")
  local cache = {}
  return function(line)
    if cache[line] == nil then
      tester.explicitModLines = { { line = line, custom = true } }; tester:BuildAndParseRaw()
      local n = statVal(cf({ repSlotName = slotName, repItem = tester }), "FullDPS")
      cache[line] = (b and b > 0 and b ~= math.huge and n and n ~= math.huge) and (n / b - 1) * 100 or 0
    end
    return cache[line]
  end
end

-- new item vs the one it replaces, netted per rule: the same line on both cancels out, the same rule with other values
-- becomes one "was → now" line with the difference. nil when nothing differs.
local function heurDiff(hn, hc)
  local order, by = {}, {}
  local function add(l, side)
    local g = by[l.rule]
    if not g then g = { new = {}, old = {}, why = l.why, conf = l.conf }; by[l.rule] = g; order[#order + 1] = l.rule end
    table.insert(g[side], l)
    if side == "new" then g.why, g.conf = l.why, l.conf end
  end
  for _, l in ipairs(hn.lines) do add(l, "new") end
  for _, l in ipairs(hc.lines) do add(l, "old") end
  local lines, total = {}, 0
  for _, r in ipairs(order) do
    local g, pct = by[r], 0
    local tn, to = {}, {}
    for _, l in ipairs(g.new) do pct = pct + l.pct; tn[#tn + 1] = l.t end
    for _, l in ipairs(g.old) do pct = pct - l.pct; to[#to + 1] = l.t end
    if math.abs(pct) >= 0.05 then
      total = total + pct
      lines[#lines + 1] = { t = #tn > 0 and table.concat(tn, "; ") or nil, was = #to > 0 and table.concat(to, "; ") or nil,
        pct = pct, why = g.why, conf = g.conf }
    end
  end
  if #lines == 0 then return nil end
  return { pct = total, lines = lines }
end

heurCompare = function(slot, item, cur, cf)
  local c = heurContext()
  local hn = heurItem(c, item, pobProbe(slot.slotName, item.baseName, cf))
  local hc = cur and heurItem(c, cur, pobProbe(slot.slotName, cur.baseName, cf)) or { pct = 0, lines = {} }
  return heurDiff(hn, hc)
end

-- weight of main-skill DPS among the chosen metrics (the indirect value is a share of DPS)
local function dpsWeight(sw)
  local w = 0
  for _, x in ipairs(sw or {}) do if ENERGY_SCALED[x.stat] then w = w + (x.weightMult or 1) end end
  return w
end

local function modWeights(params)
  QueryMods = QueryMods or LoadModule("Data/QueryMods.lua")
  local slot = assert(findSlot(params.slot), "unknown slot " .. tostring(params.slot))
  local existing = build.itemsTab.items[slot.selItemId]
  local catStr, cat = tradeHelpers.getTradeCategory(params.slot, existing)
  if not cat then error("slot not supported for weighted search") end
  local isJewel = cat == "Jewel" or cat == "BaseJewel"
  if cat == "Jewel" then cat = "BaseJewel" end
  local testBase = params.base or (existing and existing.baseName) or (isJewel and "Diamond") or nil
  if not testBase then error("в слоте нет предмета — не из чего взять базу") end
  if isJewel and data.itemBases[testBase] and data.itemBases[testBase].subType == "Radius" then cat = "RadiusJewel" end
  local testItem = new("Item"):Item("Rarity: RARE\nStat Tester\n" .. testBase)
  -- with a chosen jewel base, weigh only the mods that can roll on it (trade ids via the mods' trade hashes)
  local allowed
  if isJewel and params.base and testItem.affixes then
    allowed = {}
    for _, mod in pairs(testItem.affixes) do
      if testItem:GetModSpawnWeight(mod) > 0 then
        for hash in pairs(mod.tradeHashes or {}) do allowed[tostring(hash)] = true end
      end
    end
    if next(allowed) == nil then allowed = nil end
  end
  -- Time-Lost (radius) jewels only do something with a radius that reaches allocated nodes: weigh mods on a Large one
  local baseLines = {}
  if cat == "RadiusJewel" then baseLines = { { line = "Upgrades Radius to Large", custom = true } } end
  local function setLines(extra)
    local t = {}
    for _, l in ipairs(baseLines) do t[#t + 1] = l end
    for _, l in ipairs(extra or {}) do t[#t + 1] = l end
    testItem.explicitModLines = t
    testItem:BuildAndParseRaw()
  end
  setLines()
  local calcFunc, baseOutput = build.calcsTab:GetMiscCalculator()
  local sw = params.statWeights
  local blankOut = calcFunc({ repSlotName = slot.slotName, repItem = testItem })
  local blankStat = weightedRatio(baseOutput, blankOut, sw) * 1000
  -- indirect (low-confidence) values for mods PoB gives nothing to
  local HC = params.heur ~= false and heurContext() or nil
  local dpsW = dpsWeight(sw)
  local blankDps = statVal(blankOut, "FullDPS")
  local probeCache = {}
  local function probe(l)
    if probeCache[l] == nil then
      setLines({ { line = l, custom = true } })
      local n = statVal(calcFunc({ repSlotName = slot.slotName, repItem = testItem }), "FullDPS")
      probeCache[l] = (blankDps and blankDps > 0 and blankDps ~= math.huge and n and n ~= math.huge) and (n / blankDps - 1) * 100 or 0
    end
    return probeCache[l]
  end
  local weights, seen, resRoll = {}, {}, {}
  local ceiling, ceilingUnits
  local groups = { "Explicit", "Implicit" }
  if params.includeCorrupted then groups[#groups + 1] = "Corrupted" end
  if params.includeRunes then groups[#groups + 1] = "Rune" end
  for _, g in ipairs(groups) do
    for _, entry in pairs(QueryMods[g] or {}) do
      local range = entry[cat]
      local hash = entry.tradeMod.id:match("stat_(%d+)")
      if range and allowed and g ~= "Implicit" and hash and not allowed[hash] then range = nil end
      if range and not seen[entry.tradeMod.id] then
        seen[entry.tradeMod.id] = true
        local v = math.ceil((range.max - range.min) * 0.5 + range.min)
        local line
        local sc = entry.specialCaseData or {}
        if (v == 1 or v == -1) and sc.overrideModLineSingular then line = sc.overrideModLineSingular
        elseif (v ~= 1 and v ~= -1) and sc.overrideModLinePlural then line = sc.overrideModLinePlural
        elseif sc.overrideModLine then line = sc.overrideModLine
        else line = entry.tradeMod.text end
        local mv = v
        if entry.invertOnNegative and mv < 0 then line = swapInverse(line); mv = -mv end
        if line:find("+#") and mv >= 0 then line = line:gsub("#", mv)
        elseif entry.usePositiveSign and mv >= 0 then line = line:gsub("#", "+" .. tostring(mv))
        else line = line:gsub("+?#", mv) end
        line = line:gsub("%(Local%)", "")
        if isResLine(line) then
          -- resistances are weighted below through the trade's pseudo totals (one per element, valued up to the cap)
          local el = line:match("^%+?[%d%.]+%% to (%a+) Resistance$")
          if el and RES_RU[el] and g == "Explicit" then resRoll[el] = math.max(resRoll[el] or 0, range.max) end
          goto continue
        end
        setLines({ { line = line, custom = true } })
        local out = calcFunc({ repSlotName = slot.slotName, repItem = testItem })
        local diff = weightedRatio(baseOutput, out, sw) * 1000 - blankStat
        if diff > 0.01 and v ~= 0 then
          weights[#weights + 1] = { id = entry.tradeMod.id, text = entry.tradeMod.text, weight = diff / v, meanStatDiff = diff, testValue = v, group = g, line = line }
        elseif HC and dpsW > 0 and v ~= 0 then
          local h = heurLine(HC, line, probe)
          if h and h.pct > 0 then
            local units = h.pct * 10 * dpsW  -- +1% DPS with weight 1 = 10 units (weightedRatio * 1000)
            weights[#weights + 1] = { id = entry.tradeMod.id, text = entry.tradeMod.text, weight = units / v, meanStatDiff = units, testValue = v,
              group = g, heur = true, why = h.why, conf = h.conf, heurPct = h.pct }
          end
        end
      end
      ::continue::
    end
  end
  -- Resistances: a linear trade sum cannot stop at the cap, so each element gets one weight on its pseudo total
  -- ("+#% total to Cold Resistance", which also sums hybrid and all-elemental mods without double counting).
  -- gap = what is missing to the cap with this slot empty; one point is worth (value of +gap) / gap,
  -- scaled so that a top single-element roll for this slot is worth the gap: weight = perPoint * min(1, gap / roll)
  -- (checked on 141 real gloves: rank agreement with PoB 0.44 -> 0.63, a hard cap would give 0.65).
  if not params.ignoreRes and not isJewel then
    for _, el in ipairs({ "Fire", "Cold", "Lightning", "Chaos" }) do
      local gap = math.floor(blankOut["Missing" .. el .. "Resist"] or 0)
      if gap > 0 then
        testItem.explicitModLines = { { line = "+" .. gap .. "% to " .. el .. " Resistance", custom = true } }
        testItem:BuildAndParseRaw()
        local out = calcFunc({ repSlotName = slot.slotName, repItem = testItem })
        local diff = weightedRatio(baseOutput, out, sw) * 1000 - blankStat
        if diff > 0.01 then
          local perPoint, roll = diff / gap, resRoll[el] or 40
          weights[#weights + 1] = { id = "pseudo.pseudo_total_" .. el:lower() .. "_resistance", text = "+#% total to " .. el .. " Resistance",
            weight = perPoint * math.min(1, gap / roll), meanStatDiff = perPoint * math.min(roll, gap), testValue = gap, group = "Pseudo", gap = gap }
        end
      end
    end
  end
  if EXTRA.metaEnergy then
    for _, st in ipairs(params.extraStats or {}) do
      local v = 10
      local line = st.text:gsub("#", tostring(v))
      setLines({ { line = line, custom = true } })
      local out = calcFunc({ repSlotName = slot.slotName, repItem = testItem })
      local diff = weightedRatio(baseOutput, out, sw) * 1000 - blankStat
      if diff > 0.01 then
        weights[#weights + 1] = { id = st.id, text = st.text, weight = diff / v, meanStatDiff = diff, testValue = v, group = "Explicit", line = line }
      end
    end
    setLines()
  end
  table.sort(weights, function(a, b) return a.meanStatDiff > b.meanStatDiff end)
  -- Ceiling: a test item with the 4 most valuable mods (typical rolls) against the build as it is — if even that
  -- does not beat the current item, a trade search for this kind of item is pointless
  do
    local lines = {}
    for _, w in ipairs(weights) do
      if w.line and #lines < 4 then lines[#lines + 1] = { line = w.line, custom = true } end
    end
    setLines(lines)
    local o = calcFunc({ repSlotName = slot.slotName, repItem = testItem })
    local tw = 0; for _, x in ipairs(sw) do tw = tw + (x.weightMult or 1) end
    ceiling = (weightedRatio(baseOutput, o, sw) - weightedRatio(baseOutput, baseOutput, sw)) / tw * 100
    ceilingUnits = weightedRatio(baseOutput, o, sw) * 1000 - blankStat
    setLines()
  end
  -- current item stat diff vs blank (all its explicit + implicit lines), as PoB does for the default min sum
  testItem.explicitModLines = {}
  if existing then
    for _, l in ipairs(existing.explicitModLines) do testItem.explicitModLines[#testItem.explicitModLines + 1] = l end
    for _, l in ipairs(existing.implicitModLines) do testItem.explicitModLines[#testItem.explicitModLines + 1] = l end
  end
  testItem:BuildAndParseRaw()
  local curOut = existing and calcFunc({ repSlotName = slot.slotName, repItem = testItem }) or baseOutput
  local currentDiff = weightedRatio(baseOutput, curOut, sw) * 1000 - blankStat
  local lines = {}
  if existing then
    for _, k in ipairs({ "enchantModLines", "runeModLines", "implicitModLines", "explicitModLines" }) do
      for _, l in ipairs(existing[k] or {}) do lines[#lines + 1] = l.line end
    end
  end
  -- the current item's resistance totals, as the trade's pseudo stats would count them
  local pseudo = {}
  local function addRes(el, n) local id = "pseudo.pseudo_total_" .. el:lower() .. "_resistance"; pseudo[id] = (pseudo[id] or 0) + n end
  for _, ln in ipairs(lines) do
    local l = ln:gsub("{[^}]*}", ""):gsub("^Bonded: ", "")
    local n = tonumber(l:match("^%+?(%d+)%% to all Elemental Resistances$"))
    if n then addRes("Fire", n); addRes("Cold", n); addRes("Lightning", n)
    else
      local n2, a, b = l:match("^%+?(%d+)%% to (%a+) and (%a+) Resistances$")
      if n2 then addRes(a, tonumber(n2)); addRes(b, tonumber(n2))
      else
        local n3, c = l:match("^%+?(%d+)%% to (%a+) Resistance$")
        if n3 and RES_RU[c] then addRes(c, tonumber(n3)) end
      end
    end
  end
  return { category = catStr, queryCategory = cat, weights = weights, currentDiff = currentDiff, currentItem = existing and existing.name or nil, currentLines = lines, currentPseudo = pseudo, blankStat = blankStat, ceiling = ceiling, ceilingUnits = ceilingUnits, currentRaw = existing and existing.raw or nil,
    currentRarity = existing and existing.rarity or nil, currentTitle = existing and existing.title or nil, currentBase = existing and existing.baseName or nil }
end

local function clean(s)
  s = s:gsub("%[([^%]|]*)|([^%]]*)%]", "%2"); s = s:gsub("%[([^%]]*)%]", "%1"); return s
end
local function normalise(it)
  for _, k in ipairs({ "runeMods", "enchantMods", "implicitMods" }) do
    if it[k] then
      local t = {}
      for _, m in ipairs(it[k]) do t[#t + 1] = clean(type(m) == "table" and m.description or m) end
      it[k] = t
    end
  end
  local ex = {}
  for _, m in ipairs(it.explicitMods or {}) do
    if type(m) == "table" then
      local fl = m.flags or {}
      if m.domain == "desecrated" then fl.desecrated = true end
      if m.domain == "fractured" then fl.fractured = true end
      ex[#ex + 1] = { description = clean(m.description), flags = fl }
    else ex[#ex + 1] = clean(m) end
  end
  for _, k in ipairs({ "fracturedMods", "desecratedMods", "craftedMods" }) do
    for _, m in ipairs(it[k] or {}) do ex[#ex + 1] = { description = clean(type(m) == "table" and m.description or m), flags = { [k:gsub("Mods", "")] = true } } end
    it[k] = nil
  end
  it.explicitMods = ex
  it.ilvl = it.ilvl or 0
  it.name = it.name or ""
  return it
end

-- Build a PoB item from trade JSON without leaving it equipped (ImportItem equips; the selection is restored at once)
local function importTradeItem(it, slotName)
  -- Jewels are never put into a tree socket, not even for a moment: taking out e.g. From Nothing makes PoB
  -- deallocate the nodes it allowed, and they would not come back. An unknown slot name makes ImportItem skip equipping.
  if slotName:find("^Jewel") then slotName = "Optimizer Import" end
  local slot = findSlot(slotName)
  local origId = slot and slot.selItemId
  local itemsTab = build.itemsTab
  local captured
  local origAdd = itemsTab.AddItem
  itemsTab.AddItem = function(self, item, ...) captured = item; return origAdd(self, item, ...) end
  local ok, err = pcall(function() build.importTab:ImportItem(normalise(it), slotName) end)
  itemsTab.AddItem = origAdd
  if slot and slot.selItemId ~= origId then slot:SetSelItemId(origId) end
  if not ok then error(err) end
  if captured then return captured, true end
  -- the same listing as an item already in the build: ImportItem refreshed that item in place
  for _, x in pairs(itemsTab.items) do if it.id and x.uniqueID == it.id then return x, false end end
  error("предмет не распознан (неизвестная база)")
end

-- Every candidate is scored with PoB's calculator "with this item in the slot" (no full recalculation per item)
local function evalItems(params)
  local slot = assert(findSlot(params.slot), "unknown slot")
  local sw = params.statWeights
  local cf, realBase = build.calcsTab:GetMiscCalculator()
  local base = realBase
  local cur = build.itemsTab.items[slot.selItemId]
  -- several candidate slots (jewel sockets): every item goes where it gains the most
  local multi
  if params.slots and #params.slots > 0 then
    multi = {}
    for _, n in ipairs(params.slots) do local sl = findSlot(n); if sl then multi[#multi + 1] = sl end end
  end
  -- ignoreRes: score both the current and the new item without their resistance lines
  if params.ignoreRes and cur and not multi then base = cf({ repSlotName = slot.slotName, repItem = stripRes(cur.raw) }) end
  -- indirect (low-confidence) values: the new item's minus those of the item it replaces
  local HC = params.heur ~= false and heurContext() or nil
  local dpsW = dpsWeight(sw)
  local probes, curHeur = {}, {}
  local function probeFor(sl, baseName)
    local key = sl.slotName .. "|" .. tostring(baseName)
    if not probes[key] then
      local ok, f = pcall(pobProbe, sl.slotName, baseName, cf)
      probes[key] = ok and f or function() return 0 end
    end
    return probes[key]
  end
  local function curHeurFor(sl)
    if curHeur[sl.slotName] == nil then
      local ci = sl.selItemId and build.itemsTab.items[sl.selItemId]
      curHeur[sl.slotName] = ci and heurItem(HC, ci, probeFor(sl, ci.baseName)) or { pct = 0, lines = {} }
    end
    return curHeur[sl.slotName]
  end
  local res = {}
  for i, it in ipairs(params.items) do
    local item, added
    local ok, err = pcall(function()
      item, added = importTradeItem(it, params.slot)
      local out, useSlot = nil, slot
      if multi then
        local bestS = -1e9
        for _, sl in ipairs(multi) do
          local o = cf({ repSlotName = sl.slotName, repItem = item })
          local sc = weightedRatio(base, o, sw)
          if sc > bestS then bestS, out, useSlot = sc, o, sl end
        end
      else
        out = cf({ repSlotName = slot.slotName, repItem = item })
      end
      local entry = { idx = i, id = it.id, stats = pick(out), score = weightedRatio(base, out, sw),
        raw = item.raw, view = itemView(item), warn = warnings(realBase, out), slot = useSlot.slotName }
      if params.ignoreRes and entry.raw and not multi then
        local o2 = cf({ repSlotName = slot.slotName, repItem = stripRes(entry.raw) })
        entry.stats = pick(o2); entry.score = weightedRatio(base, o2, sw)
      end
      if HC and dpsW > 0 then
        local h = heurDiff(heurItem(HC, item, probeFor(useSlot, item.baseName)), curHeurFor(useSlot))
        if h then
          entry.heur = h
          entry.score = entry.score + h.pct / 100 * dpsW
        end
      end
      res[#res + 1] = entry
    end)
    if added and item then pcall(build.itemsTab.DeleteItem, build.itemsTab, item, true) end
    if not ok then res[#res + 1] = { idx = i, id = it.id, error = tostring(err) } end
  end
  return { base = pick(base), baseScore = weightedRatio(base, base, sw), items = res }
end

-- ---------------------------------------------------------------- unique jewels from PoB's database
local uniqueJewelCache
uniqueJewelCatalog = function()
  if uniqueJewelCache then return uniqueJewelCache end
  local list = {}
  for _, key in ipairs({ "jewel", "generated", "new" }) do
    for _, raw in ipairs(data.uniques[key] or {}) do
      local ok, it = pcall(function() return new("Item"):Item("Rarity: UNIQUE\n" .. raw) end)
      if ok and it and it.base and it.base.type == "Jewel" and it.baseName ~= "Timeless Jewel" then
        list[#list + 1] = { name = it.title, base = it.baseName, raw = "Rarity: UNIQUE\n" .. raw, variants = it.variantList and #it.variantList or 0 }
      end
    end
  end
  uniqueJewelCache = list
  return list
end

-- Every unique jewel of the database in its best variant and best socket (sockets = the ones it may go into)
local function evalUniqueJewels(params)
  local sw = params.statWeights
  local cf, base = build.calcsTab:GetMiscCalculator()
  local bs = weightedRatio(base, base, sw)
  local tw = 0; for _, w in ipairs(sw) do tw = tw + (w.weightMult or 1) end
  local slots = {}
  for _, n in ipairs(params.sockets or {}) do local sl = findSlot(n); if sl then slots[#slots + 1] = sl end end
  if #slots == 0 then return { items = {} } end
  local excl = {}; for _, n in ipairs(params.exclude or {}) do excl[n] = true end
  local only = params.only
  local gems = {}
  for _, g in ipairs(build.skillsTab.socketGroupList) do
    for _, gem in ipairs(g.gemList or {}) do if gem.nameSpec and #gem.nameSpec > 3 then gems[gem.nameSpec:lower()] = true end end
  end
  local function score(item, slot) local o = cf({ repSlotName = slot.slotName, repItem = item }); return (weightedRatio(base, o, sw) - bs) / tw * 100, o end
  local res = {}
  for _, u in ipairs(uniqueJewelCatalog()) do
    if not excl[u.name] and (not only or only == u.name) then
      local ok, err = pcall(function()
        local item = new("Item"):Item(u.raw)
        local nv = item.variantList and #item.variantList or 0
        local cands = {}
        if nv > 1 then
          for k = 1, nv do
            if nv <= 80 then cands[#cands + 1] = k
            else  -- e.g. Prism of Belief: only the variants naming a gem the build uses
              local lbl = (item.variantList[k] or ""):lower()
              for g in pairs(gems) do if lbl:find(g, 1, true) then cands[#cands + 1] = k; break end end
            end
          end
        end
        local function apply(k, alt)
          if k then item.variant = k end
          if alt then item.variantAlt = alt end
          item:BuildAndParseRaw()
        end
        -- the best possible roll: every ranged line at its top or its bottom (for "less ..." lines the bottom is better)
        local function setRoll(q)
          for _, l in ipairs(item.explicitModLines or {}) do
            if l.line and l.line:match("%(%-?[%d%.]+%-%-?[%d%.]+%)") then l.range = q end
          end
          item:BuildAndParseRaw()
        end
        local function bestRoll(slot)
          setRoll(0); local g0 = score(item, slot)
          setRoll(1); local g1, o1 = score(item, slot)
          if g0 > g1 then setRoll(0); local g, o = score(item, slot); return g, o end
          return g1, o1
        end
        local probe = slots[1]
        local bestK = item.variant
        if #cands > 0 then
          local bestG = -1e9
          for _, k in ipairs(cands) do apply(k); local g = bestRoll(probe); if g > bestG then bestG, bestK = g, k end end
          apply(bestK)
        end
        local bestAlt
        if item.hasAltVariant and nv > 1 and nv <= 80 then
          local bestA = -1e9
          for k = 1, nv do if k ~= bestK then apply(nil, k); local g = bestRoll(probe); if g > bestA then bestA, bestAlt = g, k end end end
          if bestAlt then apply(nil, bestAlt) end
        end
        local bestS, bestSlot, bestOut = -1e9, nil, nil
        for _, sl in ipairs(slots) do local g, o = bestRoll(sl); if g > bestS then bestS, bestSlot, bestOut = g, sl, o end end
        bestRoll(bestSlot)
        local vlines = {}
        if nv > 1 then
          for _, ml in ipairs(item.explicitModLines or {}) do
            if ml.variantList and (ml.variantList[bestK] or (bestAlt and ml.variantList[bestAlt])) then vlines[#vlines + 1] = ml.line end
          end
        end
        res[#res + 1] = { name = u.name, base = u.base, gain = bestS, slot = bestSlot.slotName, stats = pick(bestOut),
          variant = nv > 1 and item.variantList[bestK] or nil, variantAlt = bestAlt and item.variantList[bestAlt] or nil,
          variantLines = vlines, skipped = (nv > 80 and #cands == 0) or nil, raw = item.raw, view = itemView(item) }
      end)
      if not ok then res[#res + 1] = { name = u.name, base = u.base, error = tostring(err) } end
    end
  end
  table.sort(res, function(a, b) return (a.gain or -1e9) > (b.gain or -1e9) end)
  return { items = res }
end

-- ---------------------------------------------------------------- PoB configuration
local CFG_OPTS = require("Modules.ConfigOptions")
local cfgCondMap
local function cfgConds()
  if cfgCondMap then return cfgCondMap end
  cfgCondMap = {}
  for i, v in ipairs(CFG_OPTS) do
    if v.type == "check" and v.apply and v.var then
      local ml, eml = new("ModList"):ModList(), new("ModList"):ModList()
      if pcall(v.apply, true, ml, eml, build) then
        local function add(key) cfgCondMap[key] = cfgCondMap[key] or {}; table.insert(cfgCondMap[key], i) end
        for _, m in ipairs(ml) do local c = m.name and m.name:match("^Condition:(.+)"); if c then add(c) end end
        for _, m in ipairs(eml) do local c = m.name and m.name:match("^Condition:(.+)"); if c then add("enemy:" .. c) end end
      end
    end
  end
  return cfgCondMap
end
-- can the build do it (main skill numbers from PoB)? Only things the player does to enemies.
local SUGGEST = {
  ["enemy:Shocked"] = { "ShockChance", "шанс шока" }, ShockedEnemyRecently = { "ShockChance", "шанс шока" },
  ["enemy:Ignited"] = { "IgniteChance", "шанс поджога" }, IgnitedEnemyRecently = { "IgniteChance", "шанс поджога" },
  ["enemy:Chilled"] = { "ChillChance", "шанс охлаждения" }, ChilledEnemyRecently = { "ChillChance", "шанс охлаждения" },
  CritRecently = { "CritChance", "шанс крита" }, SkillCritRecently = { "CritChance", "шанс крита" },
}
-- PoE2 freeze/electrocute/pin/heavy stun are build-ups (PoB2 keeps the PoE1 "freeze chance" at 0): a hit fills
-- X% of the enemy's threshold. "Is the enemy Frozen?" means frozen all the time — a weak suggestion (not pre-selected).
local BUILDUP = {
  ["enemy:Frozen"] = { "FreezeBuildupAvg", "заморозки", true }, FrozenEnemyRecently = { "FreezeBuildupAvg", "заморозки" },
  ["enemy:Electrocuted"] = { "ElectrocuteBuildupAvg", "электрошока", true }, ElectrocutedEnemyRecently = { "ElectrocuteBuildupAvg", "электрошока" },
  ["enemy:Pinned"] = { "PinBuildupAvg", "пина", true }, PinnedEnemyRecently = { "PinBuildupAvg", "пина" },
  ["enemy:HeavyStunned"] = { "HeavyStunBuildupAvg", "тяжёлого стана", true },
}
local function condReason(key, out)
  local o, env = out or build.calcsTab.mainOutput, build.calcsTab.mainEnv
  local sg = SUGGEST[key]
  if sg then
    local v = o[sg[1]] or 0
    if v > 0 then return string.format("%s %d%%", sg[2], math.floor(math.min(v, 100) + 0.5)) end
    return nil
  end
  local bu = BUILDUP[key]
  if bu then
    local v = o[bu[1]] or 0
    if v <= 0 then return nil end
    local hits = math.ceil(100 / v)
    if hits > 10 then return nil end
    return string.format("накопление %s %.1f%% за удар — ~%d уд.%s", bu[2], v, hits, bu[3] and " (не постоянно — решайте сами)" or ""), bu[3]
  end
  if key == "TriggeredSkillRecently" and env then
    for _, sk in ipairs(env.player.activeSkillList or {}) do
      if sk.skillTypes and sk.skillTypes[SkillType.Triggered] then return "триггерится " .. sk.activeEffect.grantedEffect.name end
    end
  end
  return nil
end
local function cfgSet() return build.configTab.configSets[build.configTab.activeConfigSetId] end
function configSnapshot()
  local cs = cfgSet()
  CFG_ORIG = { input = copyTable(cs.input), custom = copyTable(cs.customModsList or {}) }
end
local function strip(t) return ((tostring(t or "")):gsub("%^x%x%x%x%x%x%x", ""):gsub("%^%d", "")) end
local function tipOf(v)
  if type(v.tooltip) == "string" then return strip(v.tooltip) end
  if type(v.tooltip) == "function" then
    local ok, t = pcall(v.tooltip, build.configTab.modList, build)
    if ok and type(t) == "string" then return strip(t) end
  end
  if v.tooltipFunc then
    local lines = {}
    local tt = { Clear = function() lines = {} end, AddLine = function(_, _, t) lines[#lines + 1] = strip(t) end, AddSeparator = function() end }
    pcall(v.tooltipFunc, tt, "HOVER", v.defaultIndex or 1, v.list and v.list[v.defaultIndex or 1] or { val = "None" })
    return table.concat(lines, "\n")
  end
  return nil
end
-- PoB's own visibility rules (the "shown" function of the option's control), judged at the default value
local function applicable(v)
  local ctl = build.configTab.varControls[v.var]
  if not ctl then return true end
  local input = cfgSet().input
  local cur = input[v.var]
  input[v.var] = build.configTab:GetDefaultState(v.var, type(cur))
  local ok, sh = pcall(function()
    local f = ctl.shown
    if type(f) == "function" then return f() end
    return f ~= false
  end)
  input[v.var] = cur
  return (not ok) or sh and true or false
end
-- conditions each option sets
local optCondCache
local function optConds()
  if optCondCache then return optCondCache end
  optCondCache = {}
  for key, list in pairs(cfgConds()) do
    for _, i in ipairs(list) do
      optCondCache[i] = optCondCache[i] or {}
      table.insert(optCondCache[i], key)
    end
  end
  return optCondCache
end
local function mainSkillIs(t)
  local env = build.calcsTab.mainEnv
  local ms = env and env.player.mainSkill
  return ms and ms.skillTypes and ms.skillTypes[t] or false
end
-- generic "you do this all the time" conditions, judged from the main skill
local GENERIC = {
  HitRecently = function(o) return (o.TotalDPS or 0) > 0 and "основной скилл бьёт" end,
  HitSpellRecently = function(o) return (o.TotalDPS or 0) > 0 and mainSkillIs(SkillType.Spell) and "бьёт спеллом" end,
  CastSpellRecently = function() return mainSkillIs(SkillType.Spell) and "основной скилл — спелл" end,
  AttackedRecently = function() return mainSkillIs(SkillType.Attack) and "основной скилл — атака" end,
  UsedSkillRecently = function() return "скиллы используются постоянно" end,
  KilledRecently = function() return "обычно да при зачистке карт (решайте сами)", true end,
}
local function optReason(i, v)
  for _, key in ipairs(optConds()[i] or {}) do
    local plain = (key:gsub("^enemy:", "Enemy")):lower()
    if v.var:lower():find(plain, 1, true) then
      local r, weak = condReason(key)
      if r then return r, weak end
      local g = GENERIC[key]
      if g then r, weak = g(build.calcsTab.mainOutput or {}); if r then return r, weak end end
    end
  end
  return nil
end
local function deltaOf(out, base)
  local function d(stat)
    local b, n = statVal(base, stat), statVal(out, stat)
    if not b or b == 0 or b == math.huge or n == math.huge then return nil end
    return (n / b - 1) * 100
  end
  return { dps = d("FullDPS"), ehp = d("TotalEHP"), life = d("Life"), es = d("EnergyShield") }
end
-- what setting `vars` would do, without keeping it
local function configTry(vars)
  local input = cfgSet().input
  local saved = {}
  for var, val in pairs(vars) do saved[var] = { input[var] }; input[var] = val end
  build.configTab:BuildModList()
  local cf, base = build.calcsTab:GetMiscCalculator()
  local out = cf({})
  for var, sv in pairs(saved) do input[var] = sv[1] end
  build.configTab:BuildModList()
  return deltaOf(out, base)
end

local function extrasView()
  local o = build.calcsTab.mainOutput or {}
  return { metaEnergy = { on = EXTRA.metaEnergy, gem = o.MetaEnergyGem, gemValue = o.MetaEnergyGemValue, mods = o.MetaEnergyMods, total = o.MetaEnergyInc } }
end

local function configView()
  local ct, cs = build.configTab, cfgSet()
  local sections, cur = {}, nil
  for i, v in ipairs(CFG_OPTS) do
    if v.section then
      cur = { name = v.section, col = v.col, options = {} }
      sections[#sections + 1] = cur
    elseif v.var and cur and v.type then
      local val = cs.input[v.var]
      local def = ct:GetDefaultState(v.var, type(val))
      local o = { var = v.var, type = v.type, label = strip(v.label), value = val, default = def,
        placeholder = cs.placeholder[v.var], tip = tipOf(v), applicable = applicable(v) }
      o.changed = val ~= nil and val ~= def
      if v.list then
        o.list = {}
        for _, it in ipairs(v.list) do o.list[#o.list + 1] = { val = it.val, label = strip(it.label) } end
      end
      if v.type == "check" and o.applicable and not val then o.suggest, o.weak = optReason(i, v) end
      if val and AUTO_ON and AUTO_ON[v.var] then o.auto = AUTO_ON[v.var] end
      cur.options[#cur.options + 1] = o
    end
  end
  local custom = {}
  for gi, g in ipairs(cs.customModsList or {}) do
    local lines = {}
    for line in (g.text or ""):gmatch("[^\r\n]+") do
      local l = line:gsub("^%s+", ""):gsub("%s+$", "")
      if l ~= "" then
        local list, extra = modLib.parseMod(l)
        lines[#lines + 1] = { text = l, ok = (list and #list > 0 and not extra) and true or false }
      end
    end
    custom[#custom + 1] = { title = g.title, enabled = g.enabled ~= false, text = g.text or "", lines = lines }
  end
  return { sections = sections, custom = custom, extras = extrasView(), autoInfo = AUTO_INFO }
end

local function buildAfterConfig()
  build.configTab:BuildModList()
  recalc()
  local base = info()
  base.extra = allStats(build.calcsTab.mainOutput)
  base.items = itemsView(base)
  return base
end

function clearInvalid()
  local ct, input, cleared = build.configTab, cfgSet().input, {}
  for _, v in ipairs(CFG_OPTS) do
    if v.var and v.type then
      local cur = input[v.var]
      if cur ~= nil and cur ~= ct:GetDefaultState(v.var, type(cur)) and not applicable(v) then
        input[v.var] = nil
        cleared[#cleared + 1] = strip(v.label)
      end
    end
  end
  return cleared
end

-- on load (and by the button): drop options that do nothing for this build, switch on everything it does
AUTO_ON, AUTO_INFO, USER_SET = {}, nil, {}
function configAutoApply()
  local cleared = clearInvalid()
  build.configTab:BuildModList(); recalc()
  local input, enabled = cfgSet().input, {}
  for i, v in ipairs(CFG_OPTS) do
    if v.var and v.type == "check" and not input[v.var] and not USER_SET[v.var] and applicable(v) then
      local r, weak = optReason(i, v)
      if r and not weak then input[v.var] = true; AUTO_ON[v.var] = r; enabled[#enabled + 1] = strip(v.label) end
    end
  end
  build.configTab:BuildModList(); recalc()
  AUTO_INFO = { cleared = cleared, enabled = enabled }
  return AUTO_INFO
end

-- another main skill: what was switched on automatically for the old one is judged again
function configReauto()
  local input = cfgSet().input
  for var in pairs(AUTO_ON) do if not USER_SET[var] then input[var] = nil end end
  AUTO_ON = {}
  build.configTab:BuildModList(); recalc()
  return configAutoApply()
end

local function setConfig(p)
  local input = cfgSet().input
  for var, val in pairs(p.vars or {}) do
    if val == json.null or val == "__default__" then val = nil end
    input[var] = val
    USER_SET[var] = true
    AUTO_ON[var] = nil
  end
  if p.custom then
    local list = {}
    for _, g in ipairs(p.custom) do list[#list + 1] = { title = g.title or "Default", enabled = g.enabled ~= false, text = g.text or "" } end
    if #list == 0 then list[1] = { title = "Default", enabled = true, text = "" } end
    cfgSet().customModsList = list
  end
  if p.extras then
    for k, v in pairs(p.extras) do EXTRA[k] = v and true or false end
  end
  -- options set (often in someone else's build) that PoB marks red: no source in this build, so they do nothing now,
  -- but would wrongly count mods like "damage against Bleeding enemies" on new items/nodes
  local cleared = p.clearInvalid and clearInvalid() or {}
  if p.reset and CFG_ORIG then
    local cs = cfgSet()
    for k in pairs(cs.input) do cs.input[k] = nil end
    for k, v in pairs(CFG_ORIG.input) do cs.input[k] = v end
    cs.customModsList = copyTable(CFG_ORIG.custom)
  end
  if p.auto then configAutoApply() end
  local r = buildAfterConfig()
  r.cleared = cleared
  r.autoInfo = p.auto and AUTO_INFO or nil
  return r
end

-- every applicable checkbox the build can satisfy, with what it would give on its own and all together
local function configAuto()
  local props, all = {}, {}
  for i, v in ipairs(CFG_OPTS) do
    if v.var and v.type == "check" and not cfgSet().input[v.var] and applicable(v) then
      local r, weak = optReason(i, v)
      if r then
        local d = configTry({ [v.var] = true })
        props[#props + 1] = { var = v.var, label = strip(v.label), reason = r, delta = d, weak = weak or nil }
        if not weak then all[v.var] = true end
      end
    end
  end
  local total = next(all) and configTry(all) or nil
  return { proposals = props, total = total }
end

-- several items at once (a purchase plan): equipped together, measured, then the build is put back
local function evalCombo(p)
  local sw = p.statWeights
  local cf, base = build.calcsTab:GetMiscCalculator()
  local saved, added = {}, {}
  local HC = p.heur ~= false and heurContext() or nil
  local heurSum = 0
  local ok, err = pcall(function()
    for _, x in ipairs(p.items or {}) do
      local slot = assert(findSlot(x.slot), "unknown slot " .. tostring(x.slot))
      local item = new("Item"):Item(x.raw)
      if not item.base then error("не удалось разобрать предмет") end
      item:BuildModList()
      if HC then
        local cur = build.itemsTab.items[saved[slot] or slot.selItemId]
        local okh, h = pcall(function()
          local hn = heurItem(HC, item, pobProbe(slot.slotName, item.baseName, cf))
          local hc = cur and heurItem(HC, cur, pobProbe(slot.slotName, cur.baseName, cf)) or { pct = 0 }
          return hn.pct - hc.pct
        end)
        if okh then heurSum = heurSum + h end
      end
      build.itemsTab:AddItem(item, true)
      added[#added + 1] = item
      if saved[slot] == nil then saved[slot] = slot.selItemId or 0 end
      slot:SetSelItemId(item.id)
    end
    recalc()
  end)
  local out = build.calcsTab.mainOutput
  local res
  if ok then
    local tw = 0; for _, w in ipairs(sw) do tw = tw + (w.weightMult or 1) end
    local function d(stat)
      local b, n = statVal(base, stat), statVal(out, stat)
      if not b or b == 0 or b == math.huge or n == math.huge then return nil end
      return (n / b - 1) * 100
    end
    res = { gain = (weightedRatio(base, out, sw) - weightedRatio(base, base, sw)) / (tw > 0 and tw or 1) * 100,
      dps = d("FullDPS"), ehp = d("TotalEHP"), warn = warnings(base, out) }
    if HC and heurSum ~= 0 then
      res.heur = heurSum
      res.gain = res.gain + heurSum * dpsWeight(sw) / (tw > 0 and tw or 1)
    end
  end
  for slot, id in pairs(saved) do slot:SetSelItemId(id) end
  for _, item in ipairs(added) do pcall(build.itemsTab.DeleteItem, build.itemsTab, item, true) end
  recalc()
  if not ok then error(err, 0) end
  return res
end

-- crafting data for a base: its natural mod pool (PoB has no real spawn weights: weightVal is 1 for every tier),
-- essences for its class, alloy mods, and the parsed item if one was pasted
local function craftClass(base)
  if base.subType == "Warstaff" then return "Warstaff" end
  if base.type == "Shield" and base.tags and base.tags.buckler then return "Buckler" end
  return base.type
end
local function craftMod(id)
  return data.itemMods.Item[id] or data.itemMods.Exclusive[id]
end
local function craftView(id, m)
  local ls = {}
  for _, l in ipairs(m) do ls[#ls + 1] = l end
  return { id = id, side = m.type == "Prefix" and "P" or m.type == "Suffix" and "S" or nil, group = m.group,
    level = m.level or 1, lines = ls, tags = m.modTags }
end
local function craftData(p)
  local item, baseName
  if p.raw and p.raw ~= "" then
    item = new("Item"):Item(p.raw)
    if not item.base then error("не удалось разобрать предмет: неизвестная база") end
    baseName = item.baseName
  else
    baseName = p.base
  end
  local base = data.itemBases[baseName or ""]
  if not base then error("неизвестная база: " .. tostring(baseName)) end
  local cls = craftClass(base)
  local runeText = {}
  if item then
    for _, l in ipairs(item.runeModLines or {}) do runeText[#runeText + 1] = l.line end
  end
  local marksman = p.marksman
  for _, t in ipairs(runeText) do if t:match("Marksman") then marksman = true end end
  local tags = {}
  for k in pairs(base.tags or {}) do tags[k] = true end
  local pool, mpool = {}, {}
  for id, m in pairs(data.itemMods.Item) do
    if (m.type == "Prefix" or m.type == "Suffix") and m.weightKey then
      local w, mk = 0, false
      for i, k in ipairs(m.weightKey) do
        if tags[k] or (k == "marksman" and cls == "Gloves") then w = m.weightVal[i] or 0; mk = k == "marksman"; break end
      end
      if w > 0 then
        local v = craftView(id, m)
        if mk then v.marksman = true; mpool[#mpool + 1] = v else pool[#pool + 1] = v end
      end
    end
  end
  -- desecrated-only mods (Abyssal lords Kurgal / Amanamu / Ulaman) that this base can reveal
  local dpool = {}
  for id, m in pairs(data.itemMods.Desecrated) do
    if (m.type == "Prefix" or m.type == "Suffix") and m.weightKey then
      local w = 0
      for i, k in ipairs(m.weightKey) do if tags[k] then w = m.weightVal[i] or 0; break end end
      if w > 0 then
        local v = craftView(id, m)
        for _, t in ipairs(m.modTags or {}) do local l = t:match("^(%a+)_mod$"); if l and l ~= "unveiled" then v.lord = l end end
        dpool[#dpool + 1] = v
      end
    end
  end
  -- Otherworldly mods (Altered Collarbone, Breach): PoB gives them weight only with the bone's "breach_desecration" tag
  if cls == "Amulet" or cls == "Ring" or cls == "Belt" then
    local t2 = { breach_desecration = true }
    for k in pairs(tags) do t2[k] = true end
    for id, m in pairs(data.itemMods.Desecrated) do
      if (m.type == "Prefix" or m.type == "Suffix") and m.weightKey then
        local w0, w = 0, 0
        for i, k in ipairs(m.weightKey) do if tags[k] then w0 = m.weightVal[i] or 0; break end end
        for i, k in ipairs(m.weightKey) do if t2[k] then w = m.weightVal[i] or 0; break end end
        if w0 <= 0 and w > 0 then
          local v = craftView(id, m); v.lord = "altered"
          dpool[#dpool + 1] = v
        end
      end
    end
  end
  local ess = {}
  for _, e in pairs(data.essences) do
    local mid = e.mods and e.mods[cls]
    local m = mid and craftMod(mid)
    if m then
      local v = craftView(mid, m); v.name = e.name; v.tierLevel = e.tierLevel
      ess[#ess + 1] = v
    end
  end
  local alloys = {}
  for id, m in pairs(data.itemMods.Item) do
    if id:match("^Alloy") then alloys[#alloys + 1] = craftView(id, m) end
  end
  local cur
  if item then
    cur = { rarity = item.rarity, ilvl = item.itemLevel, runes = item.runes, runeLines = runeText,
      corrupted = item.corrupted or nil, name = item.title or item.name, mods = {}, sockets = item.itemSocketCount }
    for _, l in ipairs(item.explicitModLines or {}) do
      -- "Grants Skill: ..." comes with the base (its level is raised by a currency), not an affix
      if not l.line:match("^Grants Skill:") then
        cur.mods[#cur.mods + 1] = { line = l.line, fractured = l.fractured or nil, crafted = l.crafted or nil,
          desecrated = l.desecrated or nil }
      end
    end
    -- "Quality (Caster Modifiers): +40%": mods with that tag show values x1.4
    local CAT = { { "life" }, { "mana" }, { "defences", "armour", "evasion", "energyshield" }, { "physical" }, { "fire" },
      { "cold" }, { "lightning" }, { "chaos" }, { "attack" }, { "caster" }, { "speed" }, { "attribute" }, { "minion" } }
    if item.catalyst and CAT[item.catalyst] and item.catalystQuality then
      cur.catalystTags = CAT[item.catalyst]; cur.catalystQuality = item.catalystQuality
    end
  end
  local bases = {}
  if p.listBases then
    for name, b in pairs(data.itemBases) do
      if not b.hidden and b.type ~= "Flask" and b.type ~= "Charm" and b.type ~= "Jewel" and b.type ~= "TrapTool"
        and b.type ~= "Fishing Rod" and b.type ~= "Transcendent Limb" and not name:match("^Runeforged") then
        -- armour / shield defence type from the base tags: str = armour, dex = evasion, int = energy shield
        local def
        for k in pairs(b.tags or {}) do
          local d = k:match("^(.+)_armour$") or k:match("^(.+)_shield$")
          if d and d ~= "body" then def = d end
        end
        bases[#bases + 1] = { name = name, cls = craftClass(b), level = b.req and b.req.level or 1, def = def }
      end
    end
  end
  local tagList = {}
  for k in pairs(base.tags or {}) do tagList[#tagList + 1] = k end
  local affix = { P = 0, S = 0 }
  for l in (base.implicit or ""):gmatch("[^\n]+") do
    local n, kind = l:match("^([%+%-]%d+) (%a+) Modifiers? allowed")
    if n and kind == "Prefix" then affix.P = affix.P + tonumber(n) end
    if n and kind == "Suffix" then affix.S = affix.S + tonumber(n) end
  end
  return { base = baseName, cls = cls, tags = tagList, socketLimit = base.socketLimit, marksman = marksman or nil,
    pool = pool, marksmanPool = mpool, desPool = dpool, essences = ess, alloys = alloys, item = cur, bases = bases,
    affixDelta = affix,
    baseInfo = { type = base.type, subType = base.subType, armour = base.armour, weapon = base.weapon, req = base.req,
      implicit = base.implicit, socketLimit = base.socketLimit, quality = base.quality } }
end

-- budget craft with the build: what each mod tier of a base is worth in a slot, in % of the weighted metrics, one mod at
-- a time on a blank base of that type; "blank" is the bare base against the item worn there now
local function craftValues(p)
  local slot = assert(findSlot(p.slot), "unknown slot " .. tostring(p.slot))
  -- the crafted item gets what the worn one of the same type has besides its affixes: quality and the same runes
  -- (otherwise a bare base loses to any socketed item before the first mod)
  local head = "Rarity: RARE\nStat Tester\n" .. p.base
  local nb, cur = data.itemBases[p.base], build.itemsTab.items[slot.selItemId]
  if nb and cur and cur.base and cur.base.type == nb.type then
    if (cur.quality or 0) > 0 then head = head .. "\nQuality: " .. cur.quality end
    local n = math.min(cur.itemSocketCount or 0, nb.socketLimit or 0)
    if n > 0 then
      head = head .. "\nSockets: " .. ("S "):rep(n):gsub(" $", "")
      for i = 1, n do head = head .. "\nRune: " .. (cur.runes[i] or "None") end
    end
  end
  local testItem = new("Item"):Item(head)
  if not testItem.base then error("неизвестная база: " .. tostring(p.base)) end
  local sw = p.statWeights
  local tw = 0
  for _, w in ipairs(sw) do tw = tw + (w.weightMult or 1) end
  if tw <= 0 then error("не выбрано, что улучшать (метрики в «Поиске замены»)") end
  local calcFunc, baseOutput = build.calcsTab:GetMiscCalculator()
  local function score(lines)
    testItem.explicitModLines = lines
    testItem:BuildAndParseRaw()
    return weightedRatio(baseOutput, calcFunc({ repSlotName = slot.slotName, repItem = testItem }), sw)
  end
  local now = weightedRatio(baseOutput, baseOutput, sw)
  local blank = score({})
  local vals = {}
  for i, l in ipairs(p.lines) do
    local ls = {}
    for t in l:gmatch("[^\n]+") do ls[#ls + 1] = { line = t, custom = true } end
    vals[i] = (score(ls) - blank) / tw * 100
  end
  return { blank = (blank - now) / tw * 100, vals = vals }
end

reply({ ready = true })
for line in io.lines() do
  local ok, err = pcall(function()
    local cmd = json.decode(line)
    if cmd.op == "load" then
      loadBuildFromXML(cmd.xml, "opt"); runCallback("OnFrame")
      wrapPerform(); recalc(); configSnapshot()
      AUTO_ON, USER_SET = {}, {}; configAutoApply()
      reply({ ok = true, data = fullView() })
    elseif cmd.op == "setmain" then
      build.mainSocketGroup = cmd.index
      local g = build.skillsTab.socketGroupList[cmd.index]
      if g and cmd.active then g.mainActiveSkill = cmd.active end
      recalc()
      pcall(configReauto)
      local v = info(); v.extra = allStats(build.calcsTab.mainOutput)
      reply({ ok = true, data = v })
    elseif cmd.op == "compare" then
      reply({ ok = true, data = compareItem(cmd) })
    elseif cmd.op == "parse" then
      local item = new("Item"):Item(cmd.raw)
      if not item.base then error("не удалось разобрать предмет: неизвестная база") end
      item:BuildModList()
      local v = itemView(item); v.raw = cmd.raw
      reply({ ok = true, data = v })
    elseif cmd.op == "craftValues" then
      reply({ ok = true, data = craftValues(cmd) })
    elseif cmd.op == "craftData" then
      reply({ ok = true, data = craftData(cmd) })
    elseif cmd.op == "weights" then
      reply({ ok = true, data = modWeights(cmd) })
    elseif cmd.op == "info" then
      reply({ ok = true, data = info() })
    elseif cmd.op == "uniqueJewels" then
      reply({ ok = true, data = evalUniqueJewels(cmd) })
    elseif cmd.op == "eval" then
      reply({ ok = true, data = evalItems(cmd) })
    elseif cmd.op == "setConfig" then
      reply({ ok = true, data = setConfig(cmd) })
    elseif cmd.op == "export" then
      reply({ ok = true, data = { xml = build:SaveDB("code") } })
    elseif cmd.op == "evalCombo" then
      reply({ ok = true, data = evalCombo(cmd) })
    elseif cmd.op == "configView" then
      reply({ ok = true, data = configView() })
    elseif cmd.op == "configTry" then
      local v = cmd.value; if v == json.null or v == "__default__" then v = nil end
      reply({ ok = true, data = configTry({ [cmd.var] = v }) })
    elseif cmd.op == "configAuto" then
      reply({ ok = true, data = configAuto() })
    else
      reply({ ok = false, error = "unknown op" })
    end
  end)
  if not ok then reply({ ok = false, error = tostring(err) }) end
end
