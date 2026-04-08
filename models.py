import os
from dataclasses import dataclass, field


DEFAULT_SAVE_ROOT = os.path.join(os.path.expanduser('~'), 'T-Engine', '4.0', 'tome', 'save').replace('\\', '/')


@dataclass
class CharacterConfig:
    folder_name: str
    name: str
    vault_id: str = ""

    @classmethod
    def from_dict(cls, data):
        return cls(
            folder_name=str(data.get("folder_name", "")),
            name=str(data.get("name", "")),
            vault_id=str(data.get("vault_id", "")),
        )

    def to_dict(self):
        return {
            "folder_name": self.folder_name,
            "name": self.name,
            "vault_id": self.vault_id,
        }


@dataclass
class AppConfig:
    save_root: str = DEFAULT_SAVE_ROOT
    backup_limit: int = 3
    profile_id: str = ""
    characters: list[CharacterConfig] = field(default_factory=list)

    @classmethod
    def from_dict(cls, data):
        raw_chars = data.get("characters", [])
        return cls(
            save_root=str(data.get("save_root", DEFAULT_SAVE_ROOT)),
            backup_limit=int(data.get("backup_limit", 3)),
            profile_id=str(data.get("profile_id", "")),
            characters=[CharacterConfig.from_dict(item) for item in raw_chars if isinstance(item, dict)],
        )

    def to_dict(self):
        return {
            "save_root": self.save_root,
            "backup_limit": self.backup_limit,
            "profile_id": self.profile_id,
            "characters": [char.to_dict() for char in self.characters],
        }
