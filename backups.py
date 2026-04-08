from __future__ import annotations

import shutil
from datetime import datetime
from pathlib import Path

from models import AppConfig, CharacterConfig


def create_backup(char: CharacterConfig, config: AppConfig) -> None:
    backup_dir = config.backup_root / char.folder_name
    backup_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_path = backup_dir / f"backup_{timestamp}"
    suffix = 1
    while backup_path.exists():
        backup_path = backup_dir / f"backup_{timestamp}_{suffix}"
        suffix += 1

    shutil.copytree(config.save_root / char.folder_name, backup_path)
    backups = sorted((path for path in backup_dir.iterdir() if path.is_dir()), key=lambda path: path.stat().st_mtime)
    while len(backups) > config.backup_limit:
        shutil.rmtree(backups.pop(0))


def ensure_baseline_backup(char: CharacterConfig, config: AppConfig) -> None:
    save_path = config.save_root / char.folder_name
    if not save_path.exists():
        return

    backup_dir = config.backup_root / char.folder_name
    if backup_dir.is_dir() and any(path.is_dir() for path in backup_dir.iterdir()):
        return

    try:
        create_backup(char, config)
        print(f"[{datetime.now().strftime('%H:%M:%S')}] {char.name} baseline anchored.")
    except OSError as exc:
        print(f" > Baseline anchor error: {exc}")


def get_latest_save_mtime(path: Path) -> float:
    latest_mtime = 0.0
    for root, _, file_names in path.walk():
        for file_name in file_names:
            latest_mtime = max(latest_mtime, (root / file_name).stat().st_mtime)
    return latest_mtime
