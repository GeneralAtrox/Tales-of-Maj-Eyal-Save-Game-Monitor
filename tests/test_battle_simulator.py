import unittest

from scoring.battle_simulator import BattleEnemySnapshot, BattleSimulatorState
from scoring.enemy_threat import EnemyOffense, PlayerDefenses


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


if __name__ == "__main__":
    unittest.main()
