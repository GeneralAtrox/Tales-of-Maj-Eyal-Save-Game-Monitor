import tempfile
import unittest
import zipfile
from pathlib import Path

from tools.tome_mcp_server import TomeContentStore, ToolError, call_tool


class TomeMcpServerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.source_root = root / "source"
        self.install_root = root / "TalesMajEyal"

        combat_path = self.source_root / "game" / "modules" / "tome" / "class" / "interface" / "Combat.lua"
        combat_path.parent.mkdir(parents=True, exist_ok=True)
        combat_path.write_text(
            "local _M = {}\n"
            "function _M:attackTargetWith(target, weapon, damtype, mult)\n"
            "  return self:combatDamage(weapon) * mult\n"
            "end\n"
            "newTalent{\n"
            '  name = "Shield Pummel",\n'
            '  id = "T_SHIELD_PUMMEL",\n'
            "}\n",
            encoding="utf-8",
        )

        module_init = self.source_root / "game" / "modules" / "tome" / "init.lua"
        module_init.parent.mkdir(parents=True, exist_ok=True)
        module_init.write_text("version = {1,7,6}\n", encoding="utf-8")

        engine_version = self.source_root / "game" / "engines" / "default" / "engine" / "version.lua"
        engine_version.parent.mkdir(parents=True, exist_ok=True)
        engine_version.write_text('engine.version = {1,7,6,"te4",17}\n', encoding="utf-8")

        modules_dir = self.install_root / "game" / "modules"
        engines_dir = self.install_root / "game" / "engines"
        modules_dir.mkdir(parents=True, exist_ok=True)
        engines_dir.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(modules_dir / "tome.team", "w") as zf:
            zf.writestr("mod/init.lua", "version = {1,7,6}\n")
            zf.writestr("mod/class/interface/Combat.lua", combat_path.read_text(encoding="utf-8"))
        with zipfile.ZipFile(engines_dir / "te4-1.7.6.teae", "w") as zf:
            zf.writestr("engine/version.lua", 'engine.version = {1,7,6,"te4",17}\n')
            zf.writestr("engine/DamageType.lua", "function _M:get(type) return self.list[type] end\n")

        (self.install_root / "te4_log.txt").write_text("boot\nCombat ready\n", encoding="utf-8")
        self.store = TomeContentStore(source_root=self.source_root, install_root=self.install_root)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_inventory_reports_matching_source_and_archive_versions(self) -> None:
        inventory = self.store.inventory()

        self.assertEqual(inventory["source_versions"]["module"], "1.7.6")
        self.assertEqual(inventory["archive_versions"]["engine"], "1.7.6")
        self.assertEqual(inventory["version_warning"], "")

    def test_search_source_finds_source_and_archive_hits(self) -> None:
        hits = self.store.search("attackTargetWith", scope="combat", max_results=10)
        paths = {hit.path for hit in hits}

        self.assertIn("source:game/modules/tome/class/interface/Combat.lua", paths)
        self.assertIn("install:tome.team:mod/class/interface/Combat.lua", paths)

    def test_read_source_uses_line_numbers_and_blocks_traversal(self) -> None:
        text = self.store.read_document(
            "source:game/modules/tome/class/interface/Combat.lua",
            start_line=2,
            line_count=2,
        )

        self.assertIn("2: function _M:attackTargetWith", text)
        with self.assertRaises(ToolError):
            self.store.read_document("source:../outside.lua")

    def test_find_lua_definitions_returns_talent_blocks(self) -> None:
        results = self.store.find_lua_definitions("Shield Pummel", kind="talent", max_results=5)

        self.assertTrue(results)
        self.assertEqual(results[0]["symbol"], "T_SHIELD_PUMMEL")

    def test_read_game_log_filters_lines(self) -> None:
        text = self.store.read_game_log(log_name="te4", lines=20, filter_text="combat")

        self.assertIn("Combat ready", text)
        self.assertNotIn("boot", text)

    def test_call_tool_shapes_mcp_content_response(self) -> None:
        response = call_tool(
            self.store,
            "search_source",
            {"query": "combatDamage", "scope": "combat", "max_results": 2},
        )

        self.assertIn("content", response)
        self.assertIn("combatDamage", response["content"][0]["text"])


if __name__ == "__main__":
    unittest.main()
