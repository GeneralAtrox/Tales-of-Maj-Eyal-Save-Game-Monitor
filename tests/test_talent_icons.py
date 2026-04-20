import unittest

from game_data.prodigy_db import get_prodigy_db
from game_data.talent_icons import audit_unresolved_talent_icons, resolve_talent_icon_path


class TalentIconTests(unittest.TestCase):
    def test_master_of_disasters_resolves_to_real_icon(self) -> None:
        record = next(rec for rec in get_prodigy_db().values() if rec.name == "Master of Disasters")
        path = resolve_talent_icon_path(
            name=record.name,
            data_icon=record.icon,
            talent_id=record.talent_id,
        )
        self.assertTrue(path.exists())
        self.assertEqual(path.name, "master_of_disasters.png")

    def test_all_talent_and_prodigy_icons_resolve(self) -> None:
        missing = audit_unresolved_talent_icons()
        self.assertFalse(missing, "\n".join(f"{kind}: {name} -> {filename}" for kind, name, filename in missing))


if __name__ == "__main__":
    unittest.main()
