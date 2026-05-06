import tempfile
import unittest
from pathlib import Path

import tome_practice
from game_data.boss_templates import BossActorRef
from tome_practice import (
    PracticeLaunchInfo,
    _build_launch_command,
    _build_extra_script,
    _format_addons_line,
    _patch_clone_desc,
    _read_result_file,
    _write_practice_resolution_cfg,
    _write_scenario_file,
    summarize_damage_calibration,
)


class TomePracticeTests(unittest.TestCase):
    def test_format_addons_line_appends_practice_addon_once(self) -> None:
        text = "addons = {'orcs', 'cults'}\n"
        line = _format_addons_line(text)
        self.assertEqual(line, "addons = {'orcs', 'cults', 'codex-practice-runner'}")
        self.assertEqual(_format_addons_line(line), line)

    def test_patch_clone_desc_updates_short_name_and_addons(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            desc_path = Path(tmp_dir) / "desc.lua"
            desc_path.write_text(
                'module = "tome"\n'
                "addons = {'orcs'}\n"
                'short_name = "original_name"\n',
                encoding="utf-8",
            )

            _patch_clone_desc(desc_path, "clone_name")
            text = desc_path.read_text(encoding="utf-8")

        self.assertIn('short_name = "clone_name"', text)
        self.assertIn("codex-practice-runner", text)

    def test_write_scenario_file_serializes_actor_refs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            scenario_path = Path(tmp_dir) / "scenario.lua"
            result_path = Path(tmp_dir) / "result.json"
            _write_scenario_file(
                scenario_path=scenario_path,
                result_path=result_path,
                mode="auto",
                template_key="urkis::tempest-peak",
                template_label="Urkis, the High Tempest, Tempest Peak, 17+",
                actor_refs=(
                    BossActorRef("/data/zones/tempest-peak/npcs.lua", "Urkis, the High Tempest", "URKIS"),
                ),
            )
            text = scenario_path.read_text(encoding="utf-8")

        self.assertIn("turn_cap = 200", text)
        self.assertIn("Urkis, the High Tempest", text)
        self.assertIn("/data/zones/tempest-peak/npcs.lua", text)
        self.assertIn(str(result_path), text)

    def test_build_extra_script_uses_lua_long_strings(self) -> None:
        scenario = Path(r"C:\Temp\codex-practice\scenario.lua")
        result = Path(r"C:\Temp\codex-practice\result.json")
        script = _build_extra_script(scenario, result)
        self.assertIn("set_addons={'codex-practice-boot-loader'}", script)
        self.assertIn("codex_boot_module='tome'", script)
        self.assertIn("codex_boot_save_name=[[scenario]]", script)
        self.assertIn("codex_boot_forward_info=[=[", script)
        self.assertIn("codex_practice_scenario_path=[[", script)
        self.assertIn("codex_practice_result_path=[[", script)

    def test_write_practice_resolution_cfg_forces_windowed_mode(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            resolution_cfg = Path(tmp_dir) / "settings" / "resolution.cfg"
            _write_practice_resolution_cfg(resolution_cfg)
            text = resolution_cfg.read_text(encoding="utf-8")

        self.assertEqual(text, "window.size = '1280x720 Windowed'\n")

    def test_build_launch_command_uses_isolated_home_and_no_web(self) -> None:
        launch = PracticeLaunchInfo(
            clone_name="codex_practice_test",
            clone_path=Path(r"C:\Temp\practice\save\codex_practice_test"),
            practice_user_root=Path(r"C:\Temp\practice-home"),
            scenario_path=Path(r"C:\Temp\practice\scenario.lua"),
            result_path=Path(r"C:\Temp\practice\result.json"),
            launcher_path=Path(r"C:\Games\ToME\t-engine-codex-practice.exe"),
            used_shared_launcher=False,
            template_key="prox-the-mighty::trollmire",
            template_label="Prox the Mighty, Trollmire, 7+",
        )

        command = _build_launch_command(launch)

        self.assertEqual(command[0], str(launch.launcher_path))
        self.assertEqual(command[1:4], ["--home", str(launch.practice_user_root), "--no-web"])
        self.assertIn("-Mboot", command)
        self.assertIn("-uboot", command)
        self.assertTrue(any(part.startswith("-Eset_addons=") for part in command))

    def test_read_result_file_parses_damage_events(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            result_path = Path(tmp_dir) / "result.json"
            result_path.write_text(
                "{\n"
                '  "status": "Simulation complete.",\n'
                '  "winner": "enemy",\n'
                '  "turns": 3,\n'
                '  "reason": "The player died.",\n'
                '  "detail": "Urkis",\n'
                '  "damage_events": [\n'
                '    {"turn": 2, "source": "Urkis", "source_role": "enemy", '
                '"target": "Player", "target_role": "player", "amount": 123.5, "message": "124 lightning"}\n'
                "  ]\n"
                "}\n",
                encoding="utf-8",
            )
            launch = PracticeLaunchInfo(
                clone_name="codex_practice_test",
                clone_path=Path(tmp_dir) / "save",
                practice_user_root=Path(tmp_dir),
                scenario_path=Path(tmp_dir) / "scenario.lua",
                result_path=result_path,
                launcher_path=Path(r"C:\Games\ToME\t-engine-codex-practice.exe"),
                used_shared_launcher=False,
                template_key="urkis::tempest-peak",
                template_label="Urkis, the High Tempest, Tempest Peak, 17+",
            )

            result = _read_result_file(launch)

        assert result is not None
        self.assertEqual(result.turns, 3)
        self.assertEqual(len(result.damage_events), 1)
        self.assertEqual(result.damage_events[0].source_role, "enemy")
        self.assertEqual(result.damage_events[0].target_role, "player")
        self.assertEqual(result.damage_events[0].amount, 123.5)
        self.assertEqual(result.damage_events[0].damage_type, "LIGHTNING")

    def test_damage_calibration_summary_flags_underestimate_and_top_hits(self) -> None:
        events = (
            tome_practice.PracticeDamageEvent(
                turn=1,
                source="Minor hit",
                source_role="enemy",
                target="Player",
                target_role="player",
                amount=40.0,
                damage_type="PHYSICAL",
                message="40 physical",
            ),
            tome_practice.PracticeDamageEvent(
                turn=2,
                source="Urkis",
                source_role="enemy",
                target="Player",
                target_role="player",
                amount=120.0,
                damage_type="LIGHTNING",
                message="120 lightning",
            ),
        )

        lines = summarize_damage_calibration(
            events,
            quick_expected_damage=90.0,
            quick_damage_type="PHYSICAL",
            limit=2,
        )

        self.assertEqual(lines[0], "Engine max incoming hit: 120.0 LIGHTNING from Urkis")
        self.assertEqual(lines[1], "Engine incoming by type: LIGHTNING 120.0, PHYSICAL 40.0")
        self.assertIn("0.75x engine max", lines[2])
        self.assertIn("below the engine max hit", lines[3])
        self.assertEqual(lines[4], "Damage type mismatch: engine LIGHTNING, quick PHYSICAL")
        self.assertEqual(lines[5], "Top incoming hits:")
        self.assertIn("Urkis", lines[6])

    def test_damage_calibration_uses_peak_for_underestimate_warning(self) -> None:
        events = (
            tome_practice.PracticeDamageEvent(
                turn=1,
                source="Critter",
                source_role="enemy",
                target="Player",
                target_role="player",
                amount=100.0,
                damage_type="PHYSICAL",
                message="100 physical",
            ),
        )

        lines = summarize_damage_calibration(
            events,
            quick_expected_damage=60.0,
            quick_peak_damage=110.0,
            quick_damage_type="PHYSICAL",
        )

        self.assertIn("Quick estimate: 60.0 (0.60x engine max)", lines)
        self.assertIn("Quick peak: 110.0 (1.10x engine max)", lines)
        self.assertFalse(any(line.startswith("Warning:") for line in lines))

    def test_damage_calibration_accepts_multiple_quick_damage_types(self) -> None:
        events = (
            tome_practice.PracticeDamageEvent(
                turn=1,
                source="Caster",
                source_role="enemy",
                target="Player",
                target_role="player",
                amount=100.0,
                damage_type="FIRE",
                message="100 fire",
            ),
        )

        lines = summarize_damage_calibration(
            events,
            quick_expected_damage=100.0,
            quick_damage_types=("PHYSICAL", "FIRE"),
        )

        self.assertFalse(any(line.startswith("Damage type mismatch:") for line in lines))

    def test_prepare_practice_home_merges_addons_into_engine_root(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            save_root = tmp_path / "T-Engine" / "4.0" / "tome" / "save"
            (tmp_path / "T-Engine" / "4.0" / "settings").mkdir(parents=True, exist_ok=True)
            (tmp_path / "T-Engine" / "4.0" / "addons" / "engine-addon").mkdir(parents=True, exist_ok=True)
            (tmp_path / "T-Engine" / "4.0" / "boot" / "addons" / "boot-addon").mkdir(parents=True, exist_ok=True)
            (tmp_path / "T-Engine" / "4.0" / "tome" / "addons" / "module-addon").mkdir(parents=True, exist_ok=True)
            save_root.mkdir(parents=True, exist_ok=True)

            original_runtime_root = tome_practice._PRACTICE_RUNTIME_ROOT
            tome_practice._PRACTICE_RUNTIME_ROOT = tmp_path / "runtime"
            try:
                (
                    practice_user_root,
                    practice_engine_root,
                    practice_module_root,
                    practice_save_root,
                ) = tome_practice._prepare_practice_home(
                    save_root=save_root,
                    clone_name="codex_practice_test",
                )
            finally:
                tome_practice._PRACTICE_RUNTIME_ROOT = original_runtime_root

            self.assertEqual(practice_module_root, practice_engine_root / "tome")
            self.assertEqual(practice_save_root, practice_module_root / "save")
            self.assertTrue((practice_engine_root / "addons" / "engine-addon").is_dir())
            self.assertTrue((practice_engine_root / "addons" / "boot-addon").is_dir())
            self.assertTrue((practice_engine_root / "addons" / "module-addon").is_dir())
            self.assertFalse((practice_engine_root / "boot" / "addons" / "boot-addon").exists())
            self.assertTrue((practice_user_root / "T-Engine" / "4.0" / "settings" / "resolution.cfg").is_file())


if __name__ == "__main__":
    unittest.main()
