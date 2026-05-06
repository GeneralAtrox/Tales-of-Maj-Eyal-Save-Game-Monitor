import unittest
from unittest.mock import patch

from game_data.boss_templates import (
    BossActorRef,
    BossTemplate,
    _BossBlock,
    _boss_actor_refs,
    _boss_template_stats,
    _define_block_map,
    get_boss_templates,
)
from game_data.talent_db import TalentRecord
from scoring import combat_math as cm


class BossTemplateTests(unittest.TestCase):
    def tearDown(self) -> None:
        _boss_template_stats.cache_clear()
        _boss_actor_refs.cache_clear()
        _define_block_map.cache_clear()

    def test_display_label_uses_name_location_level_and_quest(self) -> None:
        templates = get_boss_templates()
        urkis = next(template for template in templates if template.name == "Urkis, the High Tempest")
        self.assertEqual(
            urkis.display_label,
            "Urkis, the High Tempest, Tempest Peak, 17+, Optional Quest: Storming the city",
        )

    def test_stats_fall_back_to_template_metadata_without_source_data(self) -> None:
        template = BossTemplate("Fallback Boss", "Somewhere", "42+", "Quest: Testing")
        with patch("game_data.boss_templates._resolve_boss_block", return_value=None):
            stats = _boss_template_stats(template)

        self.assertEqual(stats.name, "Fallback Boss")
        self.assertEqual(stats.level, 42.0)
        self.assertEqual(stats.rank, 4.0)
        self.assertFalse(stats.has_combat_data)
        self.assertIn("partial", stats.warning.lower())

    def test_stats_parse_scalar_and_damage_fields_from_lua_block(self) -> None:
        template = BossTemplate("The Test Boss", "Test Zone", "23+", "Quest: Testing")
        block = """
newEntity{
    name = "The Test Boss",
    type = "undead", subtype = "vampire", faction = "dreadfell",
    level_range = {23, nil},
    max_life = resolvers.rngavg(300, 500),
    rank = 5,
    global_speed_base = 1.2,
    combat_physcrit = 4,
    combat_generic_crit = 1,
    combat_critical_power = 20,
    combat = {
        dam = resolvers.rngavg(40, 60), atk = 35, apr = 12,
        physcrit = 5, crit_power = 10, physspeed = 2,
        damtype = DamageType.BLIGHT,
    },
    resolvers.talents{ [Talents.T_STUNNING_BLOW] = {base = 2, every = 5, max = 5} },
    inc_damage = { [DamageType.BLIGHT] = 25, all = 10 },
    resists_pen = { [DamageType.COLD] = 15 },
}
"""
        db = {
            "T_STUNNING_BLOW": TalentRecord(
                talent_id="T_STUNNING_BLOW",
                scaling_family="weapon",
                damage_low=1.0,
                damage_high=2.0,
            )
        }
        with (
            patch(
                "game_data.boss_templates._resolve_boss_block",
                return_value=_BossBlock("data/zones/test-zone/npcs.lua", block),
            ),
            patch("scoring.talent_weapon.get_talent_db_by_id", return_value=db),
        ):
            stats = _boss_template_stats(template)

        self.assertEqual(stats.level, 23.0)
        self.assertEqual(stats.max_life, 400.0)
        self.assertEqual(stats.rank, 5.0)
        self.assertEqual(stats.rank_name, "Elite Boss")
        self.assertEqual(stats.faction, "dreadfell")
        self.assertEqual(stats.type_name, "undead")
        self.assertEqual(stats.subtype, "vampire")
        self.assertEqual(stats.global_speed, 1.2)
        self.assertEqual(stats.dam, 50.0)
        self.assertEqual(stats.atk, 35.0)
        self.assertEqual(stats.apr, 12.0)
        self.assertEqual(stats.crit_chance_pct, 10.0)
        self.assertEqual(stats.crit_power_bonus_pct, 30.0)
        self.assertEqual(stats.physspeed, 2.0)
        self.assertEqual(stats.damage_type, "BLIGHT")
        self.assertEqual(stats.talent_max_weapon_mult, 2.0)
        self.assertEqual(stats.inc_damage["BLIGHT"], 25.0)
        self.assertEqual(stats.inc_damage["ALL"], 10.0)
        self.assertEqual(stats.resists_pen["COLD"], 15.0)
        self.assertTrue(stats.has_combat_data)
        self.assertEqual(stats.warning, "")

    def test_stats_estimate_caster_powers_from_autoleveled_stats(self) -> None:
        template = BossTemplate("The Test Boss", "Test Zone", "17+", "Quest: Testing")
        block = """
newEntity{
    name = "The Test Boss",
    level_range = {17, nil},
    stats = { mag = 25, wil = 16, cun = 14 },
    combat_spellpower = 5,
    combat_mindpower = 7,
    autolevel = "caster",
}
"""
        with patch(
            "game_data.boss_templates._resolve_boss_block",
            return_value=_BossBlock("data/zones/test-zone/npcs.lua", block),
        ):
            stats = _boss_template_stats(template)

        expected_spellpower = cm.rescale_combat_stats(5 + 25 + (17 - 1) * 2)
        expected_mindpower = cm.rescale_combat_stats(7 + (16 + (17 - 1)) * 0.7 + 14 * 0.4)
        self.assertEqual(stats.spellpower, expected_spellpower)
        self.assertEqual(stats.mindpower, expected_mindpower)

    def test_stats_inherit_base_template_fields(self) -> None:
        template = BossTemplate("The Test Boss", "Test Zone", "10+", "Quest: Testing")
        base_block = """
newEntity{ define_as = "BASE_TEST_CASTER",
    type = "humanoid", subtype = "elf", faction = "rhaloren",
    stats = { mag = 20, wil = 12, cun = 8 },
    combat_spellpower = 10,
    autolevel = "caster",
    inc_damage = { [DamageType.FIRE] = 20 },
    resists_pen = { [DamageType.FIRE] = 5 },
    resolvers.talents{ [Talents.T_FLAME] = 2 },
}
"""
        block = """
newEntity{ base = "BASE_TEST_CASTER",
    name = "The Test Boss",
    level_range = {10, nil},
    resolvers.talents{ [Talents.T_LIGHTNING] = 3 },
}
"""
        with (
            patch(
                "game_data.boss_templates._resolve_boss_block",
                return_value=_BossBlock("data/zones/test-zone/npcs.lua", block),
            ),
            patch(
                "game_data.boss_templates._define_block_map",
                return_value={"BASE_TEST_CASTER": _BossBlock("data/general/npcs/test.lua", base_block)},
            ),
        ):
            stats = _boss_template_stats(template)

        self.assertEqual(stats.type_name, "humanoid")
        self.assertEqual(stats.subtype, "elf")
        self.assertEqual(stats.faction, "rhaloren")
        self.assertEqual(stats.inc_damage["FIRE"], 20.0)
        self.assertEqual(stats.resists_pen["FIRE"], 5.0)
        self.assertEqual(stats.talents["T_FLAME"], 2)
        self.assertEqual(stats.talents["T_LIGHTNING"], 3)
        self.assertGreater(stats.spellpower, 0.0)

    def test_stats_estimate_engine_melee_damage_from_stats_and_dammod(self) -> None:
        template = BossTemplate("The Test Boss", "Test Zone", "23+", "Quest: Testing")
        block = """
newEntity{
    name = "The Test Boss",
    stats = { str = 30, mag = 20 },
    combat_dam = 10,
    combat = {
        dam = 50,
        atk = 35,
        dammod = { str = 1.0, mag = 0.5 },
    },
}
"""
        with patch(
            "game_data.boss_templates._resolve_boss_block",
            return_value=_BossBlock("data/zones/test-zone/npcs.lua", block),
        ):
            stats = _boss_template_stats(template)

        self.assertAlmostEqual(stats.dam, 39.1, places=1)

    def test_actor_refs_resolve_source_path_and_define_as(self) -> None:
        template = BossTemplate(
            "Tannen & Drolem",
            "Tannen's Tower",
            "35+",
            "Optional Quest: Back and there again",
            source_names=("Tannen", "Drolem"),
            source_zone="tannen-tower",
        )
        mapping = {
            "tannen": [
                _BossBlock(
                    "data/zones/tannen-tower/npcs.lua",
                    'newEntity{ name = "Tannen", define_as = "TANNEN" }',
                )
            ],
            "drolem": [
                _BossBlock(
                    "data/zones/tannen-tower/npcs.lua",
                    'newEntity{ name = "Drolem", define_as = "DROLEM" }',
                )
            ],
        }
        with patch("game_data.boss_templates._boss_block_map", return_value=mapping):
            refs = _boss_actor_refs(template)

        self.assertEqual(
            refs,
            (
                BossActorRef("/data/zones/tannen-tower/npcs.lua", "Tannen", "TANNEN"),
                BossActorRef("/data/zones/tannen-tower/npcs.lua", "Drolem", "DROLEM"),
            ),
        )


if __name__ == "__main__":
    unittest.main()
