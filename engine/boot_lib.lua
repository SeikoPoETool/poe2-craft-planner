local function readsrc(path)
  local f, err = io.open(path, "rb"); if not f then return nil, err end
  local s = f:read("*a"); f:close()
  return (s:gsub("^\239\187\191",""):gsub("^#[^\n]*\n","\n"))
end
local _loadstring = loadstring
function loadfile(path)
  local s, err = readsrc(path); if not s then return nil, err end
  return _loadstring(s, "@"..path)
end
function dofile(path)
  local fn = assert(loadfile(path)); return fn()
end
table.insert(package.loaders, 2, function(name)
  local p = package.searchpath and package.searchpath(name, package.path)
  if not p then return nil end
  return assert(loadfile(p))
end)
dofile("HeadlessWrapper.lua")
