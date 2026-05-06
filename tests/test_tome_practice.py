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
    _write_practice_resolution_cfg,
    _write_scenario_file,
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
                practice_user_root, practice_engine_root, practice_module_root, practice_save_root = tome_practice._prepare_practice_home(
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
