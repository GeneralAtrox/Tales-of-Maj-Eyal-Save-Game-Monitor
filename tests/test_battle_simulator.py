import unittest

from scoring.battle_simulator import BattleEnemySnapshot, BattleSimulatorState
from scoring import combat_math as cm
from scoring.enemy_threat import EnemyOffense, PlayerDefenses, weapon_threat


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
            )
        )

        state.set_player_scalar("armor", 30)
        state.set_player_damage_value("resists", "FIRE", 25)

        player = state.resolved_player()
        assert player is not None
        self.assertEqual(player.armor, 30)
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
        self.assertEqual(report.weapon_threat_pct, 216.0)
        self.assertFalse(report.can_one_shot)
        self.assertIn("Can remove ~90% HP per hit", report.notes)
        self.assertIn("Acts 2.0x per turn", report.notes)

    def test_hit_rate_ceil_matches_engine(self) -> None:
        self.assertEqual(cm.hit_rate(10.1, 10.0), 51.0)

    def test_enemy_offense_reads_live_damage_type(self) -> None:
        offense = EnemyOffense.from_all_fields({"combat.damtype": "fire"}, "Test")

        self.assertEqual(offense.damage_type, "FIRE")


if __name__ == "__main__":
    unittest.main()
