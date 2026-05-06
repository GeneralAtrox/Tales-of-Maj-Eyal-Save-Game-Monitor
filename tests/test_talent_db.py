import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from game_data import talent_db


class TalentDbTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.tome_team = self.root / "tome.team"
        self.cache_file = self.root / "_talent_cache.json"
        talent_a = """
newTalent{
    name = "Stunning Blow",
    type = {"technique/2hweapon", 1},
    getDamage = function(self, t) return self:combatTalentWeaponDamage(t, 1.2, 2.0) end,
}
"""
        talent_b = """
newTalent{
    name = "Stunning Blow", short_name = "STUNNING_BLOW_ASSAULT",
    type = {"technique/2h-assault", 1},
    getDamage = function(self, t) return self:combatTalentWeaponDamage(t, 0.5, 0.7) end,
}
"""
        with zipfile.ZipFile(self.tome_team, "w") as zf:
            zf.writestr("data/talents/techniques/2hweapon.lua", talent_a)
            zf.writestr("data/talents/techniques/2h-assault.lua", talent_b)
        talent_db._db = None
        talent_db._db_by_id = None

    def tearDown(self) -> None:
        talent_db._db = None
        talent_db._db_by_id = None
        self.tmp.cleanup()

    def test_id_index_preserves_duplicate_display_names(self) -> None:
        with (
            patch("game_data.talent_db._TOME_TEAM", self.tome_team),
            patch("game_data.talent_db._CACHE_FILE", self.cache_file),
        ):
            by_id = talent_db.get_talent_db_by_id()
            by_name = talent_db.get_talent_db()

        self.assertIn("T_STUNNING_BLOW", by_id)
        self.assertIn("T_STUNNING_BLOW_ASSAULT", by_id)
        self.assertEqual(by_id["T_STUNNING_BLOW"].damage_high, 2.0)
        self.assertEqual(by_id["T_STUNNING_BLOW_ASSAULT"].damage_high, 0.7)
        self.assertIn(by_name["Stunning Blow"].talent_id, {"T_STUNNING_BLOW", "T_STUNNING_BLOW_ASSAULT"})

    def test_id_index_round_trips_through_cache(self) -> None:
        patches = (
            patch("game_data.talent_db._TOME_TEAM", self.tome_team),
            patch("game_data.talent_db._CACHE_FILE", self.cache_file),
        )
        with patches[0], patches[1]:
            talent_db.get_talent_db_by_id()
            talent_db._db = None
            talent_db._db_by_id = None
            cached = talent_db.get_talent_db_by_id()

        self.assertIn("T_STUNNING_BLOW", cached)
        self.assertIn("T_STUNNING_BLOW_ASSAULT", cached)

    def test_mode_defaults_to_engine_activated(self) -> None:
        lua = """
newTalent{
    name = "Default Action",
    type = {"spell/fire", 1},
    cooldown = 4,
    getDamage = function(self, t) return self:combatTalentSpellDamage(t, 10, 100) end,
}
"""
        [(_name, record)] = talent_db._parse_lua(lua)

        self.assertEqual(record.mode, "activated")

    def test_explicit_mode_is_preserved(self) -> None:
        lua = """
newTalent{
    name = "Passive Action",
    type = {"spell/fire", 1},
    mode = "passive",
}
"""
        [(_name, record)] = talent_db._parse_lua(lua)

        self.assertEqual(record.mode, "passive")

    def test_no_npc_use_is_preserved(self) -> None:
        lua = """
newTalent{
    name = "Forbidden Action",
    no_npc_use = true,
}
newTalent{
    name = "Commented Action",
    -- no_npc_use = true,
}
newTalent{
    name = "Explicitly Allowed Action",
    no_npc_use = false,
}
"""
        records = dict(talent_db._parse_lua(lua))

        self.assertFalse(records["Forbidden Action"].npc_usable)
        self.assertTrue(records["Commented Action"].npc_usable)
        self.assertTrue(records["Explicitly Allowed Action"].npc_usable)

    def test_numeric_resource_costs_are_preserved(self) -> None:
        lua = """
newTalent{
    name = "Resource Action",
    mana = 12,
    stamina = 4.5,
    -- vim = 99,
    action = function(self, t)
        local mana = 100
    end,
}
"""
        [(_name, record)] = talent_db._parse_lua(lua)

        self.assertEqual(record.resource_costs, {"mana": 12.0, "stamina": 4.5})

    def test_simple_range_metadata_is_preserved(self) -> None:
        lua = """
newTalent{
    name = "Ranged Action",
    requires_target = true,
    range = 6,
    radius = 2,
    action = function(self, t)
        local range = 99
    end,
}
"""
        [(_name, record)] = talent_db._parse_lua(lua)

        self.assertTrue(record.requires_target)
        self.assertEqual(record.target_range, 6.0)
        self.assertEqual(record.target_radius, 2.0)

    def test_scaled_target_metadata_uses_maximum_reach(self) -> None:
        lua = """
newTalent{
    name = "Ghoul Leap",
    requires_target = true,
    range = function(self, t) return math.floor(self:combatTalentScale(t, 5, 10, 0.5, 0, 1)) end,
    radius = function(self, t) return math.ceil(self:combatTalentLimit(t, 15, 3, 10)) end,
}
"""
        [(_name, record)] = talent_db._parse_lua(lua)

        self.assertTrue(record.requires_target)
        self.assertEqual(record.target_range, 10.0)
        self.assertEqual(record.target_radius, 15.0)

    def test_damage_type_can_come_from_direct_project_payload(self) -> None:
        lua = """
newTalent{
    name = "Fire Burst",
    getDamage = function(self, t) return self:combatTalentSpellDamage(t, 10, 100) end,
    action = function(self, t)
        self:project(tg, x, y, DamageType.FIREKNOCKBACK, {dist=3, dam=self:spellCrit(t.getDamage(self, t))})
    end,
}
"""
        [(_name, record)] = talent_db._parse_lua(lua)

        self.assertEqual(record.damage_type, "FIRE")
        self.assertEqual(record.scaling_family, "spell")
        self.assertEqual(record.crit_family, "spell")

    def test_non_damage_project_payload_stays_untyped(self) -> None:
        lua = """
newTalent{
    name = "Congeal Time",
    getProj = function(self, t) return self:combatTalentSpellDamage(t, 5, 700) end,
    action = function(self, t)
        self:projectile(tg, x, y, DamageType.CONGEAL_TIME, {slow=t.getSlow(self, t), proj=t.getProj(self, t)})
    end,
}
"""
        [(_name, record)] = talent_db._parse_lua(lua)

        self.assertEqual(record.damage_type, "")
        self.assertEqual(record.scaling_family, "spell")

    def test_status_projector_with_dam_field_stays_untyped(self) -> None:
        lua = """
newTalent{
    name = "Confuse",
    getConfusion = function(self, t) return self:combatTalentSpellDamage(t, 10, 80) end,
    action = function(self, t)
        self:project(tg, x, y, DamageType.CONFUSION, {dur=4, dam=t.getConfusion(self, t)})
    end,
}
"""
        [(_name, record)] = talent_db._parse_lua(lua)

        self.assertEqual(record.damage_type, "")
        self.assertEqual(record.scaling_family, "spell")

    def test_heal_projector_stays_untyped(self) -> None:
        lua = """
newTalent{
    name = "Heal",
    getHeal = function(self, t) return self:combatTalentSpellDamage(t, 10, 80) end,
    action = function(self, t)
        self:project(tg, x, y, DamageType.HEALING_POWER, self:spellCrit(t.getHeal(self, t)))
    end,
}
"""
        [(_name, record)] = talent_db._parse_lua(lua)

        self.assertEqual(record.damage_type, "")
        self.assertEqual(record.scaling_family, "spell")

    def test_poison_projector_maps_to_nature_damage(self) -> None:
        lua = """
newTalent{
    name = "Poison Spit",
    getDamage = function(self, t) return self:combatTalentMindDamage(t, 10, 100) end,
    action = function(self, t)
        self:project(tg, x, y, DamageType.POISON, {dam=self:mindCrit(t.getDamage(self, t))})
    end,
}
"""
        [(_name, record)] = talent_db._parse_lua(lua)

        self.assertEqual(record.damage_type, "NATURE")
        self.assertEqual(record.scaling_family, "mind")
        self.assertEqual(record.crit_family, "mind")

    def test_stat_damage_scaling_metadata_is_parsed(self) -> None:
        lua = """
newTalent{
    name = "Venom Burst",
    getDamage = function(self, t) return self:combatTalentStatDamage(t, "wil", 30, 460) end,
    action = function(self, t)
        self:project(tg, x, y, DamageType.POISON, {dam=self:mindCrit(t.getDamage(self, t))})
    end,
}
"""
        [(_name, record)] = talent_db._parse_lua(lua)

        self.assertEqual(record.damage_type, "NATURE")
        self.assertEqual(record.scaling_family, "stat")
        self.assertEqual(record.scaling_stat, "wil")
        self.assertFalse(record.scaling_no_dr)
        self.assertEqual(record.damage_low, 30.0)
        self.assertEqual(record.damage_high, 460.0)

    def test_stat_damage_no_dr_flag_is_parsed(self) -> None:
        lua = """
newTalent{
    name = "Raw Stat Burst",
    getDamage = function(self, t) return self:combatTalentStatDamage(t, "str", 10, 100, true) end,
    action = function(self, t)
        self:project(tg, x, y, DamageType.PHYSICAL, t.getDamage(self, t))
    end,
}
"""
        [(_name, record)] = talent_db._parse_lua(lua)

        self.assertEqual(record.scaling_family, "stat")
        self.assertEqual(record.scaling_stat, "str")
        self.assertTrue(record.scaling_no_dr)

    def test_physical_crit_metadata_is_parsed(self) -> None:
        lua = """
newTalent{
    name = "Warcry",
    getDamage = function(self, t) return self:combatTalentPhysicalDamage(t, 10, 100) end,
    action = function(self, t)
        self:project(tg, x, y, DamageType.PHYSICAL, self:physicalCrit(t.getDamage(self, t)))
    end,
}
"""
        [(_name, record)] = talent_db._parse_lua(lua)

        self.assertEqual(record.crit_family, "physical")

    def test_weapon_damage_uses_strongest_direct_weapon_hit(self) -> None:
        lua = """
newTalent{
    name = "Shield Pummel",
    action = function(self, t)
        self:attackTargetWith(target, shield_combat, nil, self:combatTalentWeaponDamage(t, 1, 1.7, self:getTalentLevel(self.T_SHIELD_EXPERTISE)))
        self:attackTargetWith(target, shield_combat, nil, self:combatTalentWeaponDamage(t, 1.2, 2.1, self:getTalentLevel(self.T_SHIELD_EXPERTISE)))
    end,
    info = function(self, t)
        return ([[%d%% and %d%% damage]]):tformat(
            100 * self:combatTalentWeaponDamage(t, 1, 1.7),
            100 * self:combatTalentWeaponDamage(t, 1.2, 2.1))
    end,
}
"""
        [(_name, record)] = talent_db._parse_lua(lua)

        self.assertEqual(record.scaling_family, "weapon")
        self.assertEqual(record.damage_low, 1.2)
        self.assertEqual(record.damage_high, 2.1)
        self.assertEqual(record.weapon_burst_low, 2.2)
        self.assertEqual(record.weapon_burst_high, 3.8)
        self.assertEqual(record.weapon_burst_hits, 2)
        self.assertEqual(record.weapon_aux_talent_id, "T_SHIELD_EXPERTISE")

    def test_weapon_damage_ignores_unrelated_bleed_helper(self) -> None:
        lua = """
newTalent{
    name = "Bleeding Edge",
    action = function(self, t)
        local hit = self:attackTarget(target, nil, self:combatTalentWeaponDamage(t, 1, 1.7), true)
        if hit then
            local dam = self:combatDamage(sw)
            dam = dam * self:combatTalentWeaponDamage(t, 2, 3.2)
            target:setEffect(target.EFF_DEEP_WOUND, 7, {src=self, power=dam / 7})
        end
    end,
    info = function(self, t)
        return ([[%d%% hit, %d%% bleed]]):tformat(
            100 * self:combatTalentWeaponDamage(t, 1, 1.7),
            100 * self:combatTalentWeaponDamage(t, 2, 3.2))
    end,
}
"""
        [(_name, record)] = talent_db._parse_lua(lua)

        self.assertEqual(record.scaling_family, "weapon")
        self.assertEqual(record.damage_low, 1.0)
        self.assertEqual(record.damage_high, 1.7)
        self.assertEqual(record.weapon_burst_low, 1.0)
        self.assertEqual(record.weapon_burst_high, 1.7)
        self.assertEqual(record.weapon_burst_hits, 1)


if __name__ == "__main__":
    unittest.main()
