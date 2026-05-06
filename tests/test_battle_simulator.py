import math
import unittest
from unittest.mock import patch

from game_data.talent_db import TalentRecord
from scoring import combat_math as cm
from scoring.battle_simulator import (
    BattleEnemySnapshot,
    BattleSimulatorState,
    battle_calibration_estimate,
    combined_threat_pct,
    threat_damage_type_label,
    threat_tier_label,
)
from scoring.combat_advice import survive_one_hit_advice
from scoring.enemy_threat import EnemyOffense, PlayerDefenses, weapon_threat
from scoring.talent_threat import (
    EnemyPowers,
    TalentThreatReport,
    compute_talent_threat,
    enemy_powers_from_fields,
    talent_timing_label,
)


class BattleSimulatorStateTests(unittest.TestCase):
    def test_player_overrides_merge_without_losing_base_dicts(self) -> None:
        state = BattleSimulatorState()
        state.set_live_player(
            PlayerDefenses(
                max_life=100,
                armor=12,
                defense=18,
                resists={"PHYSICAL": 15},
                resists_cap={"all": 70},
                combat_crit_reduction_pct=10,
            )
        )

        state.set_player_scalar("armor", 30)
        state.set_player_scalar("combat_crit_reduction_pct", 20)
        state.set_player_damage_value("resists", "FIRE", 25)

        player = state.resolved_player()
        assert player is not None
        self.assertEqual(player.armor, 30)
        self.assertEqual(player.combat_crit_reduction_pct, 20)
        self.assertEqual(player.resists["PHYSICAL"], 15)
        self.assertEqual(player.resists["FIRE"], 25)
        self.assertEqual(player.resists_cap["all"], 70)

    def test_reset_player_uses_latest_live_snapshot(self) -> None:
        state = BattleSimulatorState()
        state.set_live_player(PlayerDefenses(max_life=100, armor=10))
        state.set_player_scalar("armor", 40)
        state.set_live_player(PlayerDefenses(max_life=150, armor=18))

        player = state.resolved_player()
        assert player is not None
        self.assertEqual(player.armor, 40)

        state.reset_player_to_live()
        player = state.resolved_player()
        assert player is not None
        self.assertEqual(player.max_life, 150)
        self.assertEqual(player.armor, 18)

    def test_compute_returns_report_and_advice(self) -> None:
        state = BattleSimulatorState()
        state.set_live_player(PlayerDefenses(max_life=100, armor=0, defense=0))
        state.load_enemy(
            BattleEnemySnapshot(
                name="Training Dummy",
                level=25,
                max_life=500,
                offense=EnemyOffense(
                    name="Training Dummy",
                    atk=40,
                    dam=140,
                    apr=0,
                    crit_chance_pct=0,
                    global_speed=1.0,
                    rank=1.0,
                ),
            )
        )

        result = state.compute()
        self.assertIsNotNone(result.report)
        assert result.report is not None
        self.assertTrue(result.report.can_one_shot)
        self.assertTrue(result.advice)

    def test_compute_surfaces_non_weapon_talent_threat(self) -> None:
        state = BattleSimulatorState()
        state.set_live_player(PlayerDefenses(max_life=100, resists_cap={"all": 70}))
        state.load_enemy(
            BattleEnemySnapshot(
                name="Caster",
                level=25,
                max_life=500,
                offense=EnemyOffense(name="Caster", atk=10, dam=5),
                powers=EnemyPowers(
                    spellpower=100,
                    inc_damage={"FIRE": 50},
                    talents={"T_FLAME": 5},
                    talents_cd={"T_FLAME": 2},
                ),
            )
        )
        db = {
            "T_FLAME": TalentRecord(
                talent_id="T_FLAME",
                damage_type="FIRE",
                scaling_family="spell",
                damage_low=10.0,
                damage_high=100.0,
                cooldown=3,
                mode="activated",
                tactical_disable=["stun"],
            )
        }

        with patch("scoring.talent_threat.get_talent_db_by_id", return_value=db):
            result = state.compute()

        self.assertIsNotNone(result.talent_report)
        assert result.talent_report is not None
        expected = round(cm.rescale_damage(100.0) * 1.5, 1)
        self.assertEqual(result.talent_report.max_expected_damage, expected)
        self.assertEqual(result.talent_report.max_threat_pct, expected)
        self.assertEqual(result.talent_report.max_available_expected_damage, 0.0)
        self.assertEqual(result.talent_report.max_available_threat_pct, 0.0)
        self.assertEqual(result.talent_report.worst_talent_name, "Flame")
        self.assertEqual(result.talent_report.worst_cooldown, 3)
        self.assertEqual(result.talent_report.worst_current_cooldown, 2)
        self.assertEqual(result.talent_report.worst_mode, "activated")
        self.assertEqual(result.talent_report.entries[0].cooldown, 3)
        self.assertEqual(result.talent_report.entries[0].current_cooldown, 2)
        self.assertEqual(result.talent_report.entries[0].mode, "activated")
        self.assertIsNone(result.talent_report.strongest_available_entry())
        self.assertEqual(result.talent_report.cc_tags, ["stun"])

    def test_compute_skips_no_npc_use_talent_threat(self) -> None:
        state = BattleSimulatorState()
        state.set_live_player(PlayerDefenses(max_life=100, resists_cap={"all": 70}))
        state.load_enemy(
            BattleEnemySnapshot(
                name="Caster",
                level=25,
                max_life=500,
                offense=EnemyOffense(name="Caster", atk=10, dam=5),
                powers=EnemyPowers(spellpower=100, talents={"T_FLAME": 5}),
            )
        )
        db = {
            "T_FLAME": TalentRecord(
                talent_id="T_FLAME",
                damage_type="FIRE",
                scaling_family="spell",
                damage_low=10.0,
                damage_high=100.0,
                npc_usable=False,
                tactical_disable=["stun"],
            )
        }

        with patch("scoring.talent_threat.get_talent_db_by_id", return_value=db):
            result = state.compute()

        self.assertIsNotNone(result.talent_report)
        assert result.talent_report is not None
        self.assertEqual(result.talent_report.max_expected_damage, 0.0)
        self.assertEqual(result.talent_report.entries, [])
        self.assertEqual(result.talent_report.cc_tags, [])

    def test_compute_scales_talent_threat_by_global_speed(self) -> None:
        state = BattleSimulatorState()
        state.set_live_player(PlayerDefenses(max_life=100, resists_cap={"all": 70}))
        state.load_enemy(
            BattleEnemySnapshot(
                name="Fast Caster",
                offense=EnemyOffense(name="Fast Caster", global_speed=2.0),
                powers=EnemyPowers(spellpower=100, talents={"T_FLAME": 5}),
            )
        )
        db = {
            "T_FLAME": TalentRecord(
                talent_id="T_FLAME",
                damage_type="FIRE",
                scaling_family="spell",
                damage_low=10.0,
                damage_high=50.0,
            )
        }

        with patch("scoring.talent_threat.get_talent_db_by_id", return_value=db):
            result = state.compute()

        self.assertIsNotNone(result.talent_report)
        assert result.talent_report is not None
        expected = round(cm.rescale_damage(50.0), 1)
        self.assertEqual(result.talent_report.max_expected_damage, expected)
        self.assertEqual(result.talent_report.max_available_threat_pct, round(cm.rescale_damage(50.0) * 2.0, 1))

    def test_compute_skips_passive_talent_threat(self) -> None:
        state = BattleSimulatorState()
        state.set_live_player(PlayerDefenses(max_life=100, resists_cap={"all": 70}))
        state.load_enemy(
            BattleEnemySnapshot(
                name="Passive Caster",
                level=25,
                max_life=500,
                offense=EnemyOffense(name="Passive Caster", atk=10, dam=5),
                powers=EnemyPowers(spellpower=100, talents={"T_REACTIVE_FLAME": 5}),
            )
        )
        db = {
            "T_REACTIVE_FLAME": TalentRecord(
                talent_id="T_REACTIVE_FLAME",
                damage_type="FIRE",
                scaling_family="spell",
                damage_low=10.0,
                damage_high=100.0,
                mode="passive",
                tactical_disable=["stun"],
            )
        }

        with patch("scoring.talent_threat.get_talent_db_by_id", return_value=db):
            result = state.compute()

        self.assertIsNotNone(result.talent_report)
        assert result.talent_report is not None
        self.assertEqual(result.talent_report.max_expected_damage, 0.0)
        self.assertEqual(result.talent_report.entries, [])
        self.assertEqual(result.talent_report.cc_tags, [])

    def test_compute_marks_resource_blocked_talent_unavailable(self) -> None:
        state = BattleSimulatorState()
        state.set_live_player(PlayerDefenses(max_life=100, resists_cap={"all": 70}))
        state.load_enemy(
            BattleEnemySnapshot(
                name="Caster",
                level=25,
                max_life=500,
                offense=EnemyOffense(name="Caster", atk=10, dam=5),
                powers=EnemyPowers(
                    spellpower=100,
                    talents={"T_FLAME": 5},
                    resources={"mana": 5},
                    has_resource_snapshot=True,
                ),
            )
        )
        db = {
            "T_FLAME": TalentRecord(
                talent_id="T_FLAME",
                damage_type="FIRE",
                scaling_family="spell",
                damage_low=10.0,
                damage_high=100.0,
                resource_costs={"mana": 30.0},
            )
        }

        with patch("scoring.talent_threat.get_talent_db_by_id", return_value=db):
            result = state.compute()

        self.assertIsNotNone(result.talent_report)
        assert result.talent_report is not None
        self.assertEqual(result.talent_report.entries[0].resource_shortages, {"mana": 25.0})
        self.assertEqual(result.talent_report.max_available_expected_damage, 0.0)
        self.assertEqual(result.talent_report.max_available_threat_pct, 0.0)
        self.assertIsNone(result.talent_report.strongest_available_entry())

    def test_compute_marks_out_of_range_talent_unavailable(self) -> None:
        state = BattleSimulatorState()
        state.set_live_player(PlayerDefenses(max_life=100, resists_cap={"all": 70}, x=0, y=0))
        state.load_enemy(
            BattleEnemySnapshot(
                name="Distant Caster",
                level=25,
                max_life=500,
                offense=EnemyOffense(name="Distant Caster", atk=10, dam=5),
                powers=EnemyPowers(spellpower=100, talents={"T_FLAME": 5}, x=10, y=0),
            )
        )
        db = {
            "T_FLAME": TalentRecord(
                talent_id="T_FLAME",
                damage_type="FIRE",
                scaling_family="spell",
                damage_low=10.0,
                damage_high=100.0,
                requires_target=True,
                target_range=6.0,
                target_radius=1.0,
            )
        }

        with patch("scoring.talent_threat.get_talent_db_by_id", return_value=db):
            result = state.compute()

        self.assertIsNotNone(result.talent_report)
        assert result.talent_report is not None
        self.assertTrue(result.talent_report.entries[0].is_out_of_range)
        self.assertEqual(result.talent_report.max_available_expected_damage, 0.0)
        self.assertEqual(result.talent_report.max_available_threat_pct, 0.0)
        self.assertIsNone(result.talent_report.strongest_available_entry())

    def test_talent_timing_label_includes_mode_and_cooldown(self) -> None:
        self.assertEqual(talent_timing_label("activated", 4), "activated, cd 4")
        self.assertEqual(talent_timing_label("activated", 4, 2), "activated, cd 4, cooling 2")
        self.assertEqual(
            talent_timing_label("activated", 4, 0, {"mana": 25.0}),
            "activated, cd 4, needs mana +25",
        )
        self.assertEqual(
            talent_timing_label("activated", 4, 0, None, 10.0, 7.0),
            "activated, cd 4, out of range 10>7",
        )
        self.assertEqual(talent_timing_label("activated", 0), "activated, no cd")
        self.assertEqual(talent_timing_label("passive", 0), "passive")

    def test_combined_threat_prefers_talent_pressure(self) -> None:
        talent_report = TalentThreatReport(max_threat_pct=85.0, max_available_threat_pct=85.0)

        self.assertEqual(combined_threat_pct(None, talent_report), 85.0)
        self.assertEqual(threat_tier_label(85.0), "Deadly")
        self.assertEqual(threat_tier_label(35.0), "High")
        self.assertEqual(threat_tier_label(20.0), "Mediocre")

    def test_combined_threat_ignores_unavailable_talent_pressure(self) -> None:
        talent_report = TalentThreatReport(max_threat_pct=85.0, max_available_threat_pct=0.0)

        self.assertEqual(combined_threat_pct(None, talent_report), 0.0)

    def test_calibration_estimate_includes_talent_damage(self) -> None:
        state = BattleSimulatorState()
        state.set_live_player(PlayerDefenses(max_life=100, resists_cap={"all": 70}))
        state.load_enemy(
            BattleEnemySnapshot(
                name="Caster",
                offense=EnemyOffense(name="Caster", atk=10, dam=5, damage_type="PHYSICAL"),
                powers=EnemyPowers(spellpower=100, talents={"T_FLAME": 5}),
            )
        )
        db = {
            "T_FLAME": TalentRecord(
                talent_id="T_FLAME",
                damage_type="FIRE",
                scaling_family="spell",
                damage_low=10.0,
                damage_high=100.0,
            )
        }

        with patch("scoring.talent_threat.get_talent_db_by_id", return_value=db):
            result = state.compute()

        estimate = battle_calibration_estimate(result)

        expected = round(cm.rescale_damage(100.0), 1)
        self.assertEqual(estimate.expected_damage, expected)
        self.assertEqual(estimate.peak_damage, expected)
        self.assertEqual(estimate.damage_types, ("PHYSICAL", "FIRE"))

    def test_calibration_estimate_includes_weapon_project_damage_types(self) -> None:
        state = BattleSimulatorState()
        state.set_live_player(PlayerDefenses(max_life=100, resists_cap={"all": 70}))
        state.load_enemy(
            BattleEnemySnapshot(
                name="Proc Fighter",
                offense=EnemyOffense(
                    name="Proc Fighter",
                    atk=100,
                    dam=10,
                    damage_type="PHYSICAL",
                    melee_project={"FIRE": 20},
                    burst_on_hit={"COLD": 5},
                    burst_on_crit={"LIGHTNING": 10},
                ),
            )
        )

        estimate = battle_calibration_estimate(state.compute())

        self.assertEqual(estimate.damage_types, ("PHYSICAL", "FIRE", "COLD", "LIGHTNING"))

    def test_compute_applies_talent_crit_wrappers(self) -> None:
        state = BattleSimulatorState()
        state.set_live_player(PlayerDefenses(max_life=100, resists_cap={"all": 70}))
        state.load_enemy(
            BattleEnemySnapshot(
                name="Caster",
                offense=EnemyOffense(name="Caster", atk=10, dam=5),
                powers=EnemyPowers(
                    spellpower=100,
                    spell_crit_pct=50,
                    crit_power_bonus_pct=50,
                    talents={"T_FLAME": 5},
                ),
            )
        )
        db = {
            "T_FLAME": TalentRecord(
                talent_id="T_FLAME",
                damage_type="FIRE",
                scaling_family="spell",
                crit_family="spell",
                damage_low=10.0,
                damage_high=100.0,
            )
        }

        with patch("scoring.talent_threat.get_talent_db_by_id", return_value=db):
            result = state.compute()

        expected = round(cm.rescale_damage(100.0) * 1.5, 1)
        self.assertIsNotNone(result.talent_report)
        assert result.talent_report is not None
        self.assertEqual(result.talent_report.max_expected_damage, expected)
        self.assertEqual(result.talent_report.max_available_expected_damage, expected)

    def test_player_ignore_direct_crits_reduces_talent_crit_power(self) -> None:
        state = BattleSimulatorState()
        state.set_live_player(PlayerDefenses(max_life=100, resists_cap={"all": 70}, ignore_direct_crits_pct=100))
        state.load_enemy(
            BattleEnemySnapshot(
                name="Caster",
                offense=EnemyOffense(name="Caster", atk=10, dam=5),
                powers=EnemyPowers(
                    spellpower=100,
                    spell_crit_pct=100,
                    crit_power_bonus_pct=100,
                    talents={"T_FLAME": 5},
                ),
            )
        )
        db = {
            "T_FLAME": TalentRecord(
                talent_id="T_FLAME",
                damage_type="FIRE",
                scaling_family="spell",
                crit_family="spell",
                damage_low=10.0,
                damage_high=100.0,
            )
        }

        with patch("scoring.talent_threat.get_talent_db_by_id", return_value=db):
            result = state.compute()

        self.assertIsNotNone(result.talent_report)
        assert result.talent_report is not None
        self.assertEqual(result.talent_report.max_expected_damage, round(cm.rescale_damage(100.0), 1))

    def test_compute_ignores_untyped_non_weapon_scaling_helpers(self) -> None:
        state = BattleSimulatorState()
        state.set_live_player(PlayerDefenses(max_life=100, resists_cap={"all": 70}))
        state.load_enemy(
            BattleEnemySnapshot(
                name="Time Mage",
                offense=EnemyOffense(name="Time Mage", atk=10, dam=5),
                powers=EnemyPowers(spellpower=100, talents={"T_CONGEAL_TIME": 5}),
            )
        )
        db = {
            "T_CONGEAL_TIME": TalentRecord(
                talent_id="T_CONGEAL_TIME",
                damage_type="",
                scaling_family="spell",
                damage_low=5.0,
                damage_high=700.0,
            )
        }

        with patch("scoring.talent_threat.get_talent_db_by_id", return_value=db):
            result = state.compute()

        self.assertIsNotNone(result.talent_report)
        assert result.talent_report is not None
        self.assertEqual(result.talent_report.max_expected_damage, 0.0)
        self.assertEqual(result.talent_report.entries, [])

    def test_compute_surfaces_stat_scaled_talent_threat(self) -> None:
        state = BattleSimulatorState()
        state.set_live_player(PlayerDefenses(max_life=100, resists_cap={"all": 70}))
        state.load_enemy(
            BattleEnemySnapshot(
                name="Venom Drake",
                offense=EnemyOffense(name="Venom Drake", atk=10, dam=5),
                powers=EnemyPowers(stats={"wil": 80.0}, talents={"T_POISON_SPIT": 5}),
            )
        )
        db = {
            "T_POISON_SPIT": TalentRecord(
                talent_id="T_POISON_SPIT",
                damage_type="NATURE",
                scaling_family="stat",
                scaling_stat="wil",
                damage_low=30.0,
                damage_high=460.0,
            )
        }

        with patch("scoring.talent_threat.get_talent_db_by_id", return_value=db):
            result = state.compute()

        max_factor = (math.sqrt(5.0) - 1.0) * 0.8 + 1.0
        raw = (30.0 + 80.0) * max_factor * 460.0 / ((30.0 + 100.0) * max_factor)
        expected = round(raw * (1.0 - math.log10(raw * 2.0) / 7.0), 1)
        self.assertIsNotNone(result.talent_report)
        assert result.talent_report is not None
        self.assertEqual(result.talent_report.max_expected_damage, expected)
        self.assertEqual(result.talent_report.worst_damage_type, "NATURE")

    def test_weapon_threat_uses_actual_damage_type(self) -> None:
        player = PlayerDefenses(
            max_life=100,
            resists={"PHYSICAL": 50, "FIRE": -50},
            resists_cap={"all": 70},
        )
        enemy = EnemyOffense(
            atk=100,
            dam=100,
            damage_type="PHYSICAL",
            inc_damage={"FIRE": 100},
        )

        report = weapon_threat(enemy, player)

        self.assertEqual(report.damage_type, "PHYSICAL")
        self.assertAlmostEqual(report.worst_resist_multiplier, 0.5)
        self.assertEqual(report.best_inc_pct, 0.0)
        self.assertEqual(report.expected_damage, 50.0)

    def test_engine_resist_stack_damage_bonus_and_penetration(self) -> None:
        player = PlayerDefenses(
            max_life=100,
            resists={"all": 10, "FIRE": 20},
            resists_cap={"all": 70},
        )
        enemy = EnemyOffense(
            atk=100,
            dam=100,
            damage_type="FIRE",
            inc_damage={"all": 10, "FIRE": 25},
            resists_pen={"all": 10, "FIRE": 20},
        )

        report = weapon_threat(enemy, player)

        self.assertAlmostEqual(cm.effective_resist_pct(player.resists, "FIRE", player.resists_cap), 28.0)
        self.assertEqual(cm.damage_increase_for_type(enemy.inc_damage, "FIRE"), 35.0)
        self.assertAlmostEqual(report.worst_resist_multiplier, 0.804)
        self.assertEqual(report.best_inc_pct, 35.0)
        self.assertEqual(report.expected_damage, 108.5)

    def test_rank_and_speed_scale_threat_not_single_hit_damage(self) -> None:
        player = PlayerDefenses(max_life=100)
        enemy = EnemyOffense(
            atk=100,
            dam=90,
            rank=4.0,
            global_speed=2.0,
        )

        report = weapon_threat(enemy, player)

        self.assertEqual(report.expected_damage, 90.0)
        self.assertEqual(report.peak_damage, 90.0)
        self.assertEqual(report.weapon_threat_pct, 216.0)
        self.assertFalse(report.can_one_shot)
        self.assertIn("Can remove ~90% HP per hit", report.notes)
        self.assertIn("Acts 2.0x per turn", report.notes)

    def test_peak_crit_controls_one_shot_flag_and_advice(self) -> None:
        player = PlayerDefenses(max_life=100)
        enemy = EnemyOffense(
            atk=100,
            dam=70,
            crit_chance_pct=5,
            crit_power_bonus_pct=50,
        )

        report = weapon_threat(enemy, player)

        self.assertEqual(report.expected_damage, 77.0)
        self.assertEqual(report.peak_damage, 140.0)
        self.assertTrue(report.can_one_shot)
        self.assertIn("Can one-shot you (140 peak damage vs 100 effective HP)", report.notes)
        self.assertTrue(survive_one_hit_advice(enemy, player))

    def test_player_crit_reduction_lowers_weapon_crit_chance(self) -> None:
        player = PlayerDefenses(max_life=100, combat_crit_reduction_pct=20)
        enemy = EnemyOffense(
            atk=100,
            dam=70,
            crit_chance_pct=20,
            crit_power_bonus_pct=50,
        )

        report = weapon_threat(enemy, player)

        self.assertEqual(report.crit_chance_pct, 0.0)
        self.assertEqual(report.crit_used_pct, 0.0)
        self.assertEqual(report.expected_damage, 70.0)
        self.assertEqual(report.peak_damage, 70.0)
        self.assertFalse(report.can_one_shot)
        self.assertFalse(survive_one_hit_advice(enemy, player))

    def test_axe_accuracy_effect_adds_weapon_crit_after_player_reduction(self) -> None:
        player = PlayerDefenses(max_life=200, defense=0, combat_crit_reduction_pct=20)
        enemy = EnemyOffense(
            atk=100,
            dam=100,
            crit_chance_pct=20,
            accuracy_effect="axe",
        )

        report = weapon_threat(enemy, player)

        self.assertEqual(report.crit_chance_pct, 25.0)
        self.assertEqual(report.crit_used_pct, 50.0)
        self.assertEqual(report.expected_damage, 125.0)
        self.assertEqual(report.peak_damage, 150.0)

    def test_sword_accuracy_effect_adds_weapon_crit_power(self) -> None:
        player = PlayerDefenses(max_life=300, defense=0)
        enemy = EnemyOffense(
            atk=100,
            dam=100,
            crit_chance_pct=100,
            accuracy_effect="sword",
        )

        report = weapon_threat(enemy, player)

        self.assertEqual(report.expected_damage, 190.0)
        self.assertEqual(report.peak_damage, 190.0)

    def test_accuracy_effect_scale_halves_weapon_accuracy_bonus(self) -> None:
        player = PlayerDefenses(max_life=200, defense=0)
        enemy = EnemyOffense(
            atk=100,
            dam=100,
            crit_chance_pct=0,
            accuracy_effect="axe",
            accuracy_effect_scale=True,
        )

        report = weapon_threat(enemy, player)

        self.assertEqual(report.crit_chance_pct, 12.5)
        self.assertEqual(report.crit_used_pct, 25.0)
        self.assertEqual(report.expected_damage, 112.5)

    def test_mace_accuracy_effect_increases_weapon_damage_before_armor(self) -> None:
        player = PlayerDefenses(max_life=300, defense=0, armor=20, armor_hardiness_pct=100)
        enemy = EnemyOffense(
            atk=100,
            dam=100,
            accuracy_effect="mace",
        )

        report = weapon_threat(enemy, player)

        self.assertEqual(report.expected_damage, 100.0)
        self.assertIn("Mace accuracy bonus: +20% base damage", report.notes)

    def test_knife_accuracy_effect_increases_apr_before_armor(self) -> None:
        player = PlayerDefenses(max_life=300, defense=0, armor=60, armor_hardiness_pct=100)
        enemy = EnemyOffense(
            atk=100,
            dam=100,
            apr=40,
            accuracy_effect="knife",
        )

        report = weapon_threat(enemy, player)

        self.assertEqual(report.expected_damage, 100.0)
        self.assertIn("Knife accuracy bonus: +50% armor penetration", report.notes)

    def test_accuracy_damage_bonus_feeds_survival_advice(self) -> None:
        player = PlayerDefenses(max_life=100, defense=0)
        enemy = EnemyOffense(
            atk=100,
            dam=90,
            accuracy_effect="mace",
        )

        report = weapon_threat(enemy, player)

        self.assertEqual(report.peak_damage, 108.0)
        self.assertTrue(report.can_one_shot)
        self.assertTrue(survive_one_hit_advice(enemy, player))

    def test_damage_range_uses_average_for_expected_and_high_roll_for_peak(self) -> None:
        player = PlayerDefenses(max_life=300, defense=0)
        enemy = EnemyOffense(
            atk=100,
            dam=100,
            damage_range=1.4,
        )

        report = weapon_threat(enemy, player)

        self.assertEqual(report.expected_damage, 120.0)
        self.assertEqual(report.peak_damage, 140.0)
        self.assertIn("Weapon damage range: 100-140 before armor", report.notes)

    def test_damage_range_high_roll_feeds_survival_advice(self) -> None:
        player = PlayerDefenses(max_life=100, defense=0)
        enemy = EnemyOffense(
            atk=100,
            dam=80,
            damage_range=1.4,
        )

        report = weapon_threat(enemy, player)

        self.assertEqual(report.expected_damage, 96.0)
        self.assertEqual(report.peak_damage, 112.0)
        self.assertTrue(report.can_one_shot)
        self.assertTrue(survive_one_hit_advice(enemy, player))

    def test_melee_project_damage_adds_to_weapon_hit(self) -> None:
        player = PlayerDefenses(max_life=300, defense=0, resists={"FIRE": 50})
        enemy = EnemyOffense(
            atk=100,
            dam=100,
            melee_project={"FIRE": 40},
        )

        report = weapon_threat(enemy, player)

        self.assertEqual(report.expected_damage, 120.0)
        self.assertEqual(report.peak_damage, 120.0)
        self.assertEqual(report.damage_types, ("PHYSICAL", "FIRE"))
        self.assertEqual(threat_damage_type_label(report), "PHYSICAL, FIRE  (base PHYSICAL x1.00)")
        self.assertIn("On-hit project adds ~20 damage", report.notes)
        self.assertIn("Project damage types: FIRE", report.notes)

    def test_special_project_damage_types_use_base_resists(self) -> None:
        player = PlayerDefenses(
            max_life=300,
            defense=0,
            resists={"COLD": 50, "NATURE": 25, "BLIGHT": 10},
        )
        enemy = EnemyOffense(
            atk=100,
            dam=0,
            melee_project={"ICE": 40, "SLIME": 40, "DRAINLIFE": 40},
        )

        report = weapon_threat(enemy, player)

        self.assertEqual(report.expected_damage, 86.0)
        self.assertEqual(report.damage_types, ("PHYSICAL", "COLD", "NATURE", "BLIGHT"))

    def test_staff_accuracy_multiplies_melee_project_damage(self) -> None:
        player = PlayerDefenses(max_life=300, defense=0)
        enemy = EnemyOffense(
            atk=100,
            dam=100,
            accuracy_effect="staff",
            melee_project={"FIRE": 40},
        )

        report = weapon_threat(enemy, player)

        self.assertEqual(report.expected_damage, 220.0)
        self.assertIn("Staff accuracy bonus: +200% project damage", report.notes)

    def test_burst_on_crit_project_damage_uses_crit_probability(self) -> None:
        player = PlayerDefenses(max_life=300, defense=0)
        enemy = EnemyOffense(
            atk=100,
            dam=100,
            crit_chance_pct=25,
            burst_on_crit={"FIRE": 40},
        )

        report = weapon_threat(enemy, player)

        self.assertEqual(report.expected_damage, 145.0)
        self.assertEqual(report.peak_damage, 190.0)

    def test_on_hit_project_damage_repeats_for_weapon_burst_hits(self) -> None:
        player = PlayerDefenses(max_life=300, defense=0)
        enemy = EnemyOffense(
            atk=100,
            dam=100,
            melee_project={"FIRE": 20},
            talent_burst_weapon_mult=2.0,
            talent_burst_weapon_hits=2,
        )

        report = weapon_threat(enemy, player)

        self.assertEqual(report.expected_damage, 120.0)
        self.assertEqual(report.burst_expected_damage, 240.0)

    def test_hit_rate_ceil_matches_engine(self) -> None:
        self.assertEqual(cm.hit_rate(10.1, 10.0), 51.0)

    def test_enemy_offense_reads_live_damage_type(self) -> None:
        offense = EnemyOffense.from_all_fields({"combat.damtype": "fire"}, "Test")

        self.assertEqual(offense.damage_type, "FIRE")

    def test_enemy_offense_prefers_forced_live_melee_damage_type(self) -> None:
        offense = EnemyOffense.from_all_fields(
            {
                "combat.damtype": "physical",
                "force_melee_damtype": "shadowflame",
            },
            "Test",
        )

        self.assertEqual(offense.damage_type, "SHADOWFLAME")

    def test_enemy_offense_reads_accuracy_effect_from_live_combat_table(self) -> None:
        offense = EnemyOffense.from_all_fields(
            {
                "combat.talented": "sword",
                "combat.accuracy_effect_scale": 0.5,
                "combat.damrange": 1.4,
                "combat.melee_project.FIRE": 10.0,
                "melee_project.COLD": 5.0,
                "combat.burst_on_hit.BLIGHT": 7.0,
                "combat.burst_on_crit.LIGHTNING": 9.0,
            },
            "Test",
        )
        override = EnemyOffense.from_all_fields(
            {
                "combat.talented": "bow",
                "combat.accuracy_effect": "axe",
            },
            "Test",
        )

        self.assertEqual(offense.accuracy_effect, "sword")
        self.assertTrue(offense.accuracy_effect_scale)
        self.assertEqual(offense.damage_range, 1.4)
        self.assertEqual(offense.melee_project, {"FIRE": 10.0, "COLD": 5.0})
        self.assertEqual(offense.burst_on_hit, {"BLIGHT": 7.0})
        self.assertEqual(offense.burst_on_crit, {"LIGHTNING": 9.0})
        self.assertEqual(override.accuracy_effect, "axe")

    def test_enemy_offense_defaults_live_weapon_damage_range_like_engine(self) -> None:
        offense = EnemyOffense.from_all_fields({"combat.dam": 50.0}, "Test")

        self.assertEqual(offense.damage_range, 1.1)

    def test_enemy_offense_applies_live_actor_damage_range_modifier(self) -> None:
        offense = EnemyOffense.from_all_fields(
            {
                "combat.dam": 50.0,
                "combat.damrange": 1.4,
                "combat_damrange": 0.2,
            },
            "Test",
        )

        self.assertAlmostEqual(offense.damage_range, 1.6)

    def test_enemy_offense_applies_live_actor_physical_speed_modifier(self) -> None:
        offense = EnemyOffense.from_all_fields(
            {
                "combat.physspeed": 1.0,
                "combat_physspeed": 2.0,
            },
            "Test",
        )

        self.assertEqual(offense.physspeed, 0.5)

    def test_enemy_offense_reads_engine_crit_fields(self) -> None:
        offense = EnemyOffense.from_all_fields(
            {
                "combat_physcrit": 4.0,
                "combat_generic_crit": 1.5,
                "combat_critical_power": 20.0,
                "stats.cun": 20.0,
                "stats.lck": 55.0,
                "combat.physcrit": 5.0,
                "combat.crit_power": 10.0,
            },
            "Test",
        )

        self.assertEqual(offense.crit_chance_pct, 15.0)
        self.assertEqual(offense.crit_power_bonus_pct, 30.0)

    def test_enemy_offense_uses_engine_default_weapon_crit(self) -> None:
        offense = EnemyOffense.from_all_fields(
            {
                "combat_physcrit": 4.0,
                "stats.cun": 10.0,
                "stats.lck": 50.0,
            },
            "Test",
        )

        self.assertEqual(offense.crit_chance_pct, 5.0)

    def test_enemy_offense_prefers_precomputed_accuracy(self) -> None:
        offense = EnemyOffense.from_all_fields(
            {
                "combat.atk": 12.0,
                "combat_precomputed_accuracy": 47.0,
            },
            "Test",
        )

        self.assertEqual(offense.atk, 47.0)

    def test_enemy_offense_estimates_engine_accuracy(self) -> None:
        offense = EnemyOffense.from_all_fields(
            {
                "combat_atk": 10.0,
                "combat.atk": 12.0,
                "stats.dex": 30.0,
                "stats.lck": 55.0,
            },
            "Test",
        )

        self.assertEqual(offense.atk, 34.0)

    def test_enemy_offense_estimates_engine_apr(self) -> None:
        offense = EnemyOffense.from_all_fields(
            {
                "combat_apr": 4.0,
                "combat.apr": 3.0,
            },
            "Test",
        )

        self.assertEqual(offense.apr, 7.0)

    def test_enemy_offense_estimates_engine_melee_damage(self) -> None:
        offense = EnemyOffense.from_all_fields(
            {
                "combat.dam": 50.0,
                "combat_dam": 10.0,
                "stats.str": 30.0,
                "stats.mag": 20.0,
                "combat.dammod.str": 1.0,
                "combat.dammod.mag": 0.5,
            },
            "Test",
        )

        self.assertAlmostEqual(offense.dam, 39.1, places=1)

    def test_enemy_offense_reads_live_weapon_talent_multiplier(self) -> None:
        db = {
            "T_STUNNING_BLOW": TalentRecord(
                talent_id="T_STUNNING_BLOW",
                scaling_family="weapon",
                damage_low=1.0,
                damage_high=2.0,
                weapon_burst_low=1.0,
                weapon_burst_high=2.0,
                weapon_burst_hits=1,
            )
        }
        with patch("scoring.talent_weapon.get_talent_db_by_id", return_value=db):
            offense = EnemyOffense.from_all_fields(
                {
                    "combat.dam": 50.0,
                    "talents.T_STUNNING_BLOW": 5.0,
                },
                "Test",
            )

        self.assertEqual(offense.talent_max_weapon_mult, 2.0)
        self.assertEqual(offense.talent_burst_weapon_mult, 2.0)
        self.assertEqual(offense.talent_burst_weapon_hits, 1)

    def test_enemy_offense_applies_aux_weapon_talent_level(self) -> None:
        db = {
            "T_SHIELD_PUMMEL": TalentRecord(
                talent_id="T_SHIELD_PUMMEL",
                scaling_family="weapon",
                damage_low=1.0,
                damage_high=1.7,
                weapon_burst_low=2.2,
                weapon_burst_high=3.8,
                weapon_burst_hits=2,
                weapon_aux_talent_id="T_SHIELD_EXPERTISE",
            )
        }
        with patch("scoring.talent_weapon.get_talent_db_by_id", return_value=db):
            offense = EnemyOffense.from_all_fields(
                {
                    "combat.dam": 50.0,
                    "talents.T_SHIELD_PUMMEL": 5.0,
                    "talents.T_SHIELD_EXPERTISE": 5.0,
                },
                "Test",
            )

        self.assertAlmostEqual(offense.talent_max_weapon_mult, 1.0 + 0.7 * math.sqrt(7.5 / 5.0), places=3)
        self.assertAlmostEqual(offense.talent_burst_weapon_mult, 2.2 + 1.6 * math.sqrt(7.5 / 5.0), places=3)
        self.assertEqual(offense.talent_burst_weapon_hits, 2)

    def test_enemy_offense_skips_no_npc_use_weapon_talent_multiplier(self) -> None:
        db = {
            "T_STUNNING_BLOW": TalentRecord(
                talent_id="T_STUNNING_BLOW",
                scaling_family="weapon",
                damage_low=1.0,
                damage_high=3.0,
                weapon_burst_low=1.0,
                weapon_burst_high=3.0,
                weapon_burst_hits=1,
                npc_usable=False,
            )
        }
        with patch("scoring.talent_weapon.get_talent_db_by_id", return_value=db):
            offense = EnemyOffense.from_all_fields(
                {
                    "combat.dam": 50.0,
                    "talents.T_STUNNING_BLOW": 5.0,
                },
                "Test",
            )

        self.assertEqual(offense.talent_max_weapon_mult, 1.0)
        self.assertEqual(offense.talent_burst_weapon_mult, 1.0)
        self.assertEqual(offense.talent_burst_weapon_hits, 1)

    def test_enemy_offense_skips_passive_weapon_talent_multiplier(self) -> None:
        db = {
            "T_PASSIVE_STRIKE": TalentRecord(
                talent_id="T_PASSIVE_STRIKE",
                scaling_family="weapon",
                damage_low=1.0,
                damage_high=3.0,
                weapon_burst_low=1.0,
                weapon_burst_high=3.0,
                weapon_burst_hits=1,
                mode="passive",
            )
        }
        with patch("scoring.talent_weapon.get_talent_db_by_id", return_value=db):
            offense = EnemyOffense.from_all_fields(
                {
                    "combat.dam": 50.0,
                    "talents.T_PASSIVE_STRIKE": 5.0,
                },
                "Test",
            )

        self.assertEqual(offense.talent_max_weapon_mult, 1.0)
        self.assertEqual(offense.talent_burst_weapon_mult, 1.0)
        self.assertEqual(offense.talent_burst_weapon_hits, 1)

    def test_enemy_offense_skips_resource_blocked_weapon_talent_multiplier(self) -> None:
        db = {
            "T_STUNNING_BLOW": TalentRecord(
                talent_id="T_STUNNING_BLOW",
                scaling_family="weapon",
                damage_low=1.0,
                damage_high=3.0,
                weapon_burst_low=1.0,
                weapon_burst_high=3.0,
                weapon_burst_hits=1,
                resource_costs={"stamina": 20.0},
            )
        }
        with patch("scoring.talent_weapon.get_talent_db_by_id", return_value=db):
            offense = EnemyOffense.from_all_fields(
                {
                    "combat.dam": 50.0,
                    "stamina": 5.0,
                    "talents.T_STUNNING_BLOW": 5.0,
                },
                "Test",
            )

        self.assertEqual(offense.talent_max_weapon_mult, 1.0)
        self.assertEqual(offense.talent_burst_weapon_mult, 1.0)
        self.assertEqual(offense.talent_burst_weapon_hits, 1)

    def test_enemy_offense_skips_cooling_weapon_talent_multiplier(self) -> None:
        db = {
            "T_STUNNING_BLOW": TalentRecord(
                talent_id="T_STUNNING_BLOW",
                scaling_family="weapon",
                damage_low=1.0,
                damage_high=3.0,
                weapon_burst_low=1.0,
                weapon_burst_high=3.0,
                weapon_burst_hits=1,
            )
        }
        with patch("scoring.talent_weapon.get_talent_db_by_id", return_value=db):
            offense = EnemyOffense.from_all_fields(
                {
                    "combat.dam": 50.0,
                    "talents.stunning_blow": 5.0,
                    "talents_cd.STUNNING_BLOW": 2.0,
                },
                "Test",
            )

        self.assertEqual(offense.talent_max_weapon_mult, 1.0)
        self.assertEqual(offense.talent_burst_weapon_mult, 1.0)
        self.assertEqual(offense.talent_burst_weapon_hits, 1)

    def test_weapon_threat_respects_live_weapon_talent_range(self) -> None:
        db = {
            "T_STUNNING_BLOW": TalentRecord(
                talent_id="T_STUNNING_BLOW",
                scaling_family="weapon",
                damage_low=1.0,
                damage_high=3.0,
                weapon_burst_low=1.0,
                weapon_burst_high=3.0,
                weapon_burst_hits=1,
                requires_target=True,
                target_range=1.0,
            )
        }
        player = PlayerDefenses(max_life=100, defense=0, x=0, y=0)
        enemy = EnemyOffense(
            atk=100,
            dam=50,
            talents={"T_STUNNING_BLOW": 5.0},
            x=5,
            y=0,
            talent_max_weapon_mult=3.0,
            talent_burst_weapon_mult=3.0,
        )

        with patch("scoring.talent_weapon.get_talent_db_by_id", return_value=db):
            out_of_range = weapon_threat(enemy, player)
            enemy.x = 1
            in_range = weapon_threat(enemy, player)

        self.assertEqual(out_of_range.expected_damage, 50.0)
        self.assertEqual(out_of_range.burst_expected_damage, 50.0)
        self.assertEqual(in_range.expected_damage, 150.0)
        self.assertEqual(in_range.burst_expected_damage, 150.0)

    def test_weapon_threat_respects_live_archery_range(self) -> None:
        db = {
            "T_SKIRMISHER_KNEECAPPER": TalentRecord(
                talent_id="T_SKIRMISHER_KNEECAPPER",
                scaling_family="weapon",
                damage_low=1.0,
                damage_high=3.0,
                weapon_burst_low=1.0,
                weapon_burst_high=3.0,
                weapon_burst_hits=1,
                requires_target=True,
                target_range_source="archery",
            )
        }
        player = PlayerDefenses(max_life=100, defense=0, x=0, y=0)
        enemy = EnemyOffense(
            atk=100,
            dam=50,
            talents={"T_SKIRMISHER_KNEECAPPER": 5.0},
            weapon_range=6.0,
            x=8,
            y=0,
        )

        with patch("scoring.talent_weapon.get_talent_db_by_id", return_value=db):
            out_of_range = weapon_threat(enemy, player)
            enemy.weapon_range = 9.0
            in_range = weapon_threat(enemy, player)

        self.assertEqual(out_of_range.expected_damage, 50.0)
        self.assertEqual(in_range.expected_damage, 150.0)

    def test_talent_threat_respects_live_archery_range(self) -> None:
        player = PlayerDefenses(max_life=100, resists_cap={"all": 70}, x=0, y=0)
        powers = EnemyPowers(spellpower=100, talents={"T_ARROW_SPELL": 5}, weapon_range=6.0, x=8, y=0)
        db = {
            "T_ARROW_SPELL": TalentRecord(
                talent_id="T_ARROW_SPELL",
                damage_type="FIRE",
                scaling_family="spell",
                damage_low=10.0,
                damage_high=50.0,
                requires_target=True,
                target_range_source="archery",
            )
        }

        report = compute_talent_threat(powers, player, db=db)

        self.assertTrue(report.entries[0].is_out_of_range)
        self.assertEqual(report.entries[0].range_limit, 6.0)

    def test_weapon_threat_resolves_delegated_talent_range(self) -> None:
        db = {
            "T_THROWING_KNIVES": TalentRecord(talent_id="T_THROWING_KNIVES", target_range=10.0),
            "T_VENOMOUS_THROW": TalentRecord(
                talent_id="T_VENOMOUS_THROW",
                scaling_family="weapon",
                damage_low=1.0,
                damage_high=2.0,
                weapon_burst_low=1.0,
                weapon_burst_high=2.0,
                weapon_burst_hits=1,
                requires_target=True,
                target_range_source="talent_range:T_THROWING_KNIVES",
            ),
        }
        player = PlayerDefenses(max_life=100, defense=0, x=0, y=0)
        enemy = EnemyOffense(atk=100, dam=50, talents={"T_VENOMOUS_THROW": 5.0}, x=11, y=0)

        with patch("scoring.talent_weapon.get_talent_db_by_id", return_value=db):
            out_of_range = weapon_threat(enemy, player)
            enemy.x = 10
            in_range = weapon_threat(enemy, player)

        self.assertEqual(out_of_range.expected_damage, 50.0)
        self.assertEqual(in_range.expected_damage, 100.0)

    def test_talent_threat_resolves_delegated_helper_range(self) -> None:
        player = PlayerDefenses(max_life=100, resists_cap={"all": 70}, x=0, y=0)
        powers = EnemyPowers(spellpower=100, talents={"T_WARP_MINE_TOWARD": 5}, x=10, y=0)
        db = {
            "T_WARP_MINES": TalentRecord(
                talent_id="T_WARP_MINES",
                numeric_helpers={"getRange": 9.0},
            ),
            "T_WARP_MINE_TOWARD": TalentRecord(
                talent_id="T_WARP_MINE_TOWARD",
                damage_type="PHYSICAL",
                scaling_family="spell",
                damage_low=10.0,
                damage_high=50.0,
                requires_target=True,
                target_range_source="talent_helper:T_WARP_MINES:getRange",
            ),
        }

        report = compute_talent_threat(powers, player, db=db)

        self.assertTrue(report.entries[0].is_out_of_range)
        self.assertEqual(report.entries[0].range_limit, 9.0)

    def test_weapon_threat_surfaces_multi_hit_burst_kill(self) -> None:
        player = PlayerDefenses(max_life=100, armor=0, defense=0)
        enemy = EnemyOffense(
            atk=100,
            dam=40,
            crit_chance_pct=0,
            talent_max_weapon_mult=1.5,
            talent_burst_weapon_mult=3.0,
            talent_burst_weapon_hits=2,
        )

        report = weapon_threat(enemy, player)

        self.assertEqual(report.expected_damage, 60.0)
        self.assertEqual(report.burst_expected_damage, 120.0)
        self.assertFalse(report.can_one_shot)
        self.assertTrue(report.can_burst_kill)
        self.assertTrue(any("2-hit weapon burst" in note for note in report.notes))

    def test_weapon_threat_scales_fast_weapon_action_rate(self) -> None:
        player = PlayerDefenses(max_life=100, armor=0, defense=0)
        enemy = EnemyOffense(atk=100, dam=20, crit_chance_pct=0, physspeed=0.5)

        report = weapon_threat(enemy, player)

        self.assertEqual(report.expected_damage, 20.0)
        self.assertEqual(report.weapon_threat_pct, 40.0)
        self.assertTrue(any("Fast weapon action (2.0x rate)" in note for note in report.notes))

    def test_enemy_powers_reads_live_spell_power_and_talents(self) -> None:
        powers = enemy_powers_from_fields(
            {
                "combat_spellpower": 42.0,
                "combat_mindpower": 13.0,
                "combat_generic_power": 5.0,
                "combat_generic_crit": 2.0,
                "combat_dam": 10.0,
                "stats.str": 30.0,
                "stats.mag": 18.0,
                "stats.wil": 20.0,
                "stats.cun": 10.0,
                "stats.lck": 50.0,
                "combat.atk": 20.0,
                "combat_apr": 4.0,
                "combat.apr": 3.0,
                "combat_spellcrit": 7.0,
                "combat_mindcrit": 11.0,
                "combat_physcrit": 13.0,
                "combat.crit_power": 15.0,
                "combat_critical_power": 20.0,
                "inc_damage.FIRE": 25.0,
                "resists_pen.FIRE": 10.0,
                "talents.flame": 5.0,
                "talents_cd.flame": 1.2,
                "mana": 30.0,
            }
        )

        self.assertEqual(powers.spellpower, cm.rescale_combat_stats(42.0 + 5.0 + 18.0))
        self.assertEqual(powers.mindpower, cm.rescale_combat_stats(13.0 + 5.0 + 20.0 * 0.7 + 10.0 * 0.4))
        self.assertEqual(powers.physicalpower, cm.rescale_combat_stats(10.0 + 5.0 + 30.0))
        self.assertEqual(powers.spell_crit_pct, 10.0)
        self.assertEqual(powers.mind_crit_pct, 14.0)
        self.assertEqual(powers.physical_crit_pct, 16.0)
        self.assertEqual(powers.crit_power_bonus_pct, 20.0)
        self.assertEqual(powers.atk, 20.0)
        self.assertEqual(powers.apr, 7.0)
        self.assertEqual(powers.inc_damage, {"FIRE": 25.0})
        self.assertEqual(powers.resists_pen, {"FIRE": 10.0})
        self.assertEqual(powers.talents, {"T_FLAME": 5})
        self.assertEqual(powers.talents_cd, {"T_FLAME": 2})
        self.assertEqual(powers.resources, {"mana": 30.0})
        self.assertTrue(powers.has_resource_snapshot)

    def test_enemy_powers_respect_precomputed_engine_power_fields(self) -> None:
        powers = enemy_powers_from_fields(
            {
                "combat_spellpower": 200.0,
                "combat_mindpower": 200.0,
                "combat_dam": 200.0,
                "stats.str": 200.0,
                "stats.mag": 200.0,
                "stats.wil": 200.0,
                "stats.cun": 200.0,
                "combat_precomputed_spellpower": 50.0,
                "combat_precomputed_mindpower": 40.0,
                "combat_precomputed_physpower": 30.0,
            }
        )

        self.assertEqual(powers.spellpower, cm.rescale_combat_stats(50.0))
        self.assertEqual(powers.mindpower, cm.rescale_combat_stats(40.0))
        self.assertEqual(powers.physicalpower, cm.rescale_combat_stats(30.0))


if __name__ == "__main__":
    unittest.main()
