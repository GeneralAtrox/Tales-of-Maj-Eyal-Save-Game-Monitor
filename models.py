from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Final

DEFAULT_SAVE_ROOT: Final[Path] = Path.home() / "T-Engine" / "4.0" / "tome" / "save"


@dataclass(slots=True)
class CharacterConfig:
    folder_name: str
    name: str
    vault_id: str = ""

    @classmethod
    def from_dict(cls, data: dict[str, object]) -> CharacterConfig:
        return cls(
            folder_name=str(data.get("folder_name", "")),
            name=str(data.get("name", "")),
            vault_id=str(data.get("vault_id", "")),
        )

    def to_dict(self) -> dict[str, str]:
        return {
            "folder_name": self.folder_name,
            "name": self.name,
            "vault_id": self.vault_id,
        }


@dataclass(slots=True)
class AppConfig:
    save_root: Path = DEFAULT_SAVE_ROOT
    backup_limit: int = 3
    profile_id: str = ""
    characters: list[CharacterConfig] = field(default_factory=list)

    @property
    def backup_root(self) -> Path:
        return self.save_root / "Backups"

    @property
    def character_sheets_root(self) -> Path:
        return self.save_root / "CharacterSheets"

    @property
    def builds_root(self) -> Path:
        return self.save_root / "Builds"

    @classmethod
    def from_dict(cls, data: dict[str, object]) -> AppConfig:
        raw_chars = data.get("characters", [])
        return cls(
            save_root=Path(str(data.get("save_root", DEFAULT_SAVE_ROOT))),
            backup_limit=int(data.get("backup_limit", 3)),
            profile_id=str(data.get("profile_id", "")),
            characters=[CharacterConfig.from_dict(item) for item in raw_chars if isinstance(item, dict)],
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "save_root": self.save_root.as_posix(),
            "backup_limit": self.backup_limit,
            "profile_id": self.profile_id,
            "characters": [char.to_dict() for char in self.characters],
        }
