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


if __name__ == "__main__":
    unittest.main()
