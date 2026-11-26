"""Game rules and names in one place: what changes with a PoE2 patch (currency, omens, bones, affix limits,
item classes, trade categories, poe.ninja sections). craft.py and server.py import everything from here.

On a patch day go through this file first (see docs/PATCH_DAY.md). Mod pools, tiers and essences are NOT here:
they come from Path of Building (engine/pob2), spawn weights and Alloy mods from poe2db (tools/update_poe2db.py).
"""

DEFAULT_LEAGUE = "Forbidden Rites"  # default league (tests/regress.py prices); the UI has its own field

# ---------------------------------------------------------------- affix limits and other hard rules
AFFIX_MAX = {"P": 3, "S": 3}       # a rare item: 3 prefixes + 3 suffixes (Serle's Triumph rune: +1 suffix)
CRAFTED_MAX = 1                    # crafted mods (essence / Alloy); Astrid's Creativity rune: +1
FRACTURE_MIN_MODS = 4              # Fracturing Orb needs a rare with at least this many mods...
FRACTURE_MIN_MODS_BONE = 3         # ...or this many + an unrevealed desecrated mod (it is never the one fractured)
ANCIENT_BONE_FLOOR = 40            # Ancient bones reveal only mods of level >= 40 (Preserved: no floor)

# ---------------------------------------------------------------- item classes (Path of Building names)
PAGE = {"Gloves": "Gloves", "Helmet": "Helmets", "Body Armour": "Body_Armours", "Boots": "Boots", "Shield": "Shields",
        "Buckler": "Bucklers", "Focus": "Foci", "Quiver": "Quivers", "Ring": "Rings", "Amulet": "Amulets", "Belt": "Belts",
        "Wand": "Wands", "Staff": "Staves", "Warstaff": "Quarterstaves", "Sceptre": "Sceptres", "Bow": "Bows",
        "Crossbow": "Crossbows", "Spear": "Spears", "Flail": "Flails", "Talisman": "Talismans", "Dagger": "Daggers",
        "Claw": "Claws", "One Hand Sword": "One_Hand_Swords", "Two Hand Sword": "Two_Hand_Swords",
        "One Hand Axe": "One_Hand_Axes", "Two Hand Axe": "Two_Hand_Axes", "One Hand Mace": "One_Hand_Maces",
        "Two Hand Mace": "Two_Hand_Maces"}
ATTR = ("str_dex_int", "str_dex", "str_int", "dex_int", "str", "dex", "int")

JEWELLERY = {"Ring", "Amulet", "Belt"}
ARMOUR = {"Helmet", "Body Armour", "Gloves", "Boots", "Shield", "Buckler", "Focus"}

# ---------------------------------------------------------------- currency, omens, bones, runes (exact in-game names)
Q_RU = {"": "обычный", "Greater": "Greater", "Perfect": "Perfect"}
CUR = {
    "transmute": {"": "Orb of Transmutation", "Greater": "Greater Orb of Transmutation", "Perfect": "Perfect Orb of Transmutation"},
    "augment": {"": "Orb of Augmentation", "Greater": "Greater Orb of Augmentation", "Perfect": "Perfect Orb of Augmentation"},
    "regal": {"": "Regal Orb", "Greater": "Greater Regal Orb", "Perfect": "Perfect Regal Orb"},
    "exalt": {"": "Exalted Orb", "Greater": "Greater Exalted Orb", "Perfect": "Perfect Exalted Orb"},
    "chaos": {"": "Chaos Orb", "Greater": "Greater Chaos Orb", "Perfect": "Perfect Chaos Orb"},
}
FLOOR = {
    "transmute": {"": 0, "Greater": 44, "Perfect": 70}, "augment": {"": 0, "Greater": 44, "Perfect": 70},
    "regal": {"": 0, "Greater": 35, "Perfect": 50}, "exalt": {"": 0, "Greater": 35, "Perfect": 50},
    "chaos": {"": 0, "Greater": 35, "Perfect": 50},
}
PUTREF, ALCHEMY = "Omen of Putrefaction", "Orb of Alchemy"
BASES = ("Белая база", "Ваш предмет")  # priced by the base price field, not by poe.ninja
OMEN = {
    "exalt": {"P": "Omen of Sinistral Exaltation", "S": "Omen of Dextral Exaltation"},
    "annul": {"P": "Omen of Sinistral Annulment", "S": "Omen of Dextral Annulment"},
    "cryst": {"P": "Omen of Sinistral Crystallisation", "S": "Omen of Dextral Crystallisation"},
    "necro": {"P": "Omen of Sinistral Necromancy", "S": "Omen of Dextral Necromancy"},
}
GREATER, ECHOES, LIGHT = "Omen of Greater Exaltation", "Omen of Abyssal Echoes", "Omen of Light"
LORD_OMEN = {"kurgal": "Omen of the Blackblooded", "ulaman": "Omen of the Sovereign", "amanamu": "Omen of the Liege"}
LORD_RU = {"kurgal": "Kurgal", "ulaman": "Ulaman", "amanamu": "Amanamu", "altered": "Otherworldly (Altered)"}
ANNUL, FRACTURE, ARTIFICER = "Orb of Annulment", "Fracturing Orb", "Artificer's Orb"
WHITTLING = "Omen of Whittling"
RUNE = {"kolr": "Kolr's Hunt", "serle": "Serle's Triumph", "astrid": "Astrid's Creativity"}
# mod tier weights set by hand (mod id -> weight): tiers poe2db does not publish, where the rank estimate (craft.weigher)
# is known to be off. +2 to Level of all Projectile Skills (Kolr's Hunt): 200
WEIGHT_OVERRIDE = {"MarksmanInfluenceProjectileSkills2": 200}
CORRUPT_ESS = {"Essence of Hysteria", "Essence of Delirium", "Essence of Horror", "Essence of Insanity",
               "Essence of the Abyss", "Essence of the Breach"}
# catalysts (jewellery quality) by the mod tag they scale, as PoB lists them
CATALYST = [("life", "Flesh Catalyst"), ("mana", "Neural Catalyst"), ("defences", "Carapace Catalyst"),
            ("armour", "Carapace Catalyst"), ("evasion", "Carapace Catalyst"), ("energyshield", "Carapace Catalyst"),
            ("physical", "Uul-Netol's Catalyst"), ("fire", "Xoph's Catalyst"), ("cold", "Tul's Catalyst"),
            ("lightning", "Esh's Catalyst"), ("chaos", "Chayula's Catalyst"), ("attack", "Reaver Catalyst"),
            ("caster", "Sibilant Catalyst"), ("speed", "Skittering Catalyst"), ("attribute", "Adaptive Catalyst"),
            ("minion", "Necrotic Catalyst")]
SIDE_RU = {"P": "префикс", "S": "суффикс"}


def bone_kind(cls):
    return "Collarbone" if cls in JEWELLERY else "Rib" if cls in ARMOUR else "Jawbone"


# ---------------------------------------------------------------- trade site and poe.ninja
PSEUDO = {  # trade-site pseudo stats a goal can use: total over these mod groups, or empty affixes
    "pseudo.pseudo_total_elemental_resistance": ("+#% total Elemental Resistance", ("FireResistance", "ColdResistance", "LightningResistance")),
    "pseudo.pseudo_total_resistance": ("+#% total Resistance", ("FireResistance", "ColdResistance", "LightningResistance", "ChaosResistance")),
    "pseudo.pseudo_total_fire_resistance": ("+#% total to Fire Resistance", ("FireResistance",)),
    "pseudo.pseudo_total_cold_resistance": ("+#% total to Cold Resistance", ("ColdResistance",)),
    "pseudo.pseudo_total_lightning_resistance": ("+#% total to Lightning Resistance", ("LightningResistance",)),
    "pseudo.pseudo_total_chaos_resistance": ("+#% total to Chaos Resistance", ("ChaosResistance",)),
    "pseudo.pseudo_number_of_empty_prefix_mods": ("# Empty Prefix Modifiers", ()),
    "pseudo.pseudo_number_of_empty_suffix_mods": ("# Empty Suffix Modifiers", ()),
}

TRADE_CAT = {"Gloves": "armour.gloves", "Boots": "armour.boots", "Helmet": "armour.helmet", "Body Armour": "armour.chest",
             "Shield": "armour.shield", "Buckler": "armour.buckler", "Focus": "armour.focus", "Quiver": "armour.quiver",
             "Ring": "accessory.ring", "Amulet": "accessory.amulet", "Belt": "accessory.belt",
             "Wand": "weapon.wand", "Staff": "weapon.staff", "Warstaff": "weapon.warstaff", "Sceptre": "weapon.sceptre",
             "Bow": "weapon.bow", "Crossbow": "weapon.crossbow", "Spear": "weapon.spear", "Flail": "weapon.flail",
             "Talisman": "weapon.talisman", "Dagger": "weapon.dagger", "Claw": "weapon.claw",
             "One Hand Sword": "weapon.onesword", "Two Hand Sword": "weapon.twosword", "One Hand Axe": "weapon.oneaxe",
             "Two Hand Axe": "weapon.twoaxe", "One Hand Mace": "weapon.onemace", "Two Hand Mace": "weapon.twomace"}

NINJA_TYPES = ("Currency", "Essences", "Runes", "Abyss", "Ritual", "Verisium", "Breach")  # + bones, omens, alloys, catalysts

META_RUNES = {"kolr": "Can roll Marksman modifiers", "serle": "+1 Suffix Modifier allowed",
              "astrid": "Can have 1 additional Crafted Modifier"}
