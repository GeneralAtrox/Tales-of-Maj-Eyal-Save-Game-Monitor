from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Final

from backups import create_backup, ensure_baseline_backup, get_latest_save_mtime
from models import AppConfig, CharacterConfig
from parsers import parse_desc_lua, vault_name_matches
from runtime_output import console_print
from te4_client import discover_profile_id, get_vault_ids_from_profile, schedule_scrying_sync

CONFIG_FILE: Final[Path] = Path("config.json")


def load_config(config_path: Path = CONFIG_FILE) -> AppConfig:
    if not config_path.exists():
        return AppConfig()

    return AppConfig.from_dict(json.loads(config_path.read_text(encoding="utf-8")))


def save_config(config: AppConfig, config_path: Path = CONFIG_FILE) -> None:
    config_path.write_text(json.dumps(config.to_dict(), indent=4), encoding="utf-8")


def find_characters_missing_sheets(config: AppConfig) -> list[CharacterConfig]:
    config.character_sheets_root.mkdir(exist_ok=True)
    missing_sheets: list[CharacterConfig] = []
    for char in config.characters:
        if not char.vault_id:
            continue
        sheet_path = config.character_sheets_root / f"data_{char.folder_name}.json"
        if not sheet_path.exists():
            missing_sheets.append(char)
    return missing_sheets


def auto_discover_characters(
    save_root: Path,
    existing_chars: list[CharacterConfig],
    config: AppConfig,
) -> tuple[list[CharacterConfig], bool, list[CharacterConfig]]:
    existing_folders = {char.folder_name for char in existing_chars}
    needs_saving = False
    new_added: list[CharacterConfig] = []

    if not save_root.is_dir():
        print(f"[!] Save root does not exist: {save_root}")
        return existing_chars, False, []

    local_alive: list[CharacterConfig] = []
    for save_dir in save_root.iterdir():
        desc_path = save_dir / "desc.lua"
        if not save_dir.is_dir() or not desc_path.exists():
            continue

        lua_data = parse_desc_lua(desc_path)
        short_name = lua_data.get("short_name")
        name = lua_data.get("name")
        if not short_name or not name:
            print(f"[!] Skipping {save_dir.name}: desc.lua is missing short_name or name.")
            continue
        if short_name not in existing_folders and lua_data.get("loadable", True):
            local_alive.append(CharacterConfig(folder_name=short_name, name=name))

    if not local_alive:
        if not existing_chars:
            print("No new characters found")
        return existing_chars, False, []

    roster = get_vault_ids_from_profile(config.profile_id) if config.profile_id else {}
    if not config.profile_id:
        profile_id, roster = discover_profile_id(local_alive)
        if profile_id:
            config.profile_id = profile_id
            print(f"    -> Profile ID saved: {profile_id}")
        else:
            manual_profile_id = input("    Enter your TE4 profile ID manually (leave blank to skip sync): ").strip()
            if manual_profile_id:
                config.profile_id = manual_profile_id
                roster = get_vault_ids_from_profile(config.profile_id)
                print(f"    -> Profile ID saved: {config.profile_id}")

    for char in local_alive:
        print(f"[*] Processing: {char.name}")
        matches = [
            (vault_id, display_name)
            for vault_id, display_name in roster.items()
            if vault_name_matches(display_name, char.name)
        ]

        if len(matches) == 1:
            char.vault_id = matches[0][0]
            print(f"    -> Match found! {matches[0][1]} saved.")
        else:
            char.vault_id = input(
                f"    Vault ID not found or multiple matches. Paste manually for {char.name}: "
            ).strip()

        existing_chars.append(char)
        new_added.append(char)
        needs_saving = True

    return existing_chars, needs_saving, new_added


def initialize_system(config_path: Path = CONFIG_FILE) -> AppConfig:
    config = load_config(config_path)
    config.characters, needs_save, new_chars = auto_discover_characters(config.save_root, config.characters, config)
    if needs_save:
        save_config(config, config_path)
        print(f"[*] Configuration updated and saved to {config_path}.")

    config.backup_root.mkdir(exist_ok=True)
    for char in config.characters:
        ensure_baseline_backup(char, config)

    sync_targets = {char.folder_name: char for char in new_chars}
    for char in find_characters_missing_sheets(config):
        sync_targets.setdefault(char.folder_name, char)
        print(f"[*] Character sheet missing for {char.name}; scheduling vault sync.")

    for char in sync_targets.values():
        schedule_scrying_sync(char, config)
    return config


def perform_update(char: CharacterConfig, config: AppConfig) -> None:
    time.sleep(2)
    try:
        create_backup(char, config)
        print(f"[{time.strftime('%H:%M:%S')}] {char.name} timeline anchored.")
        schedule_scrying_sync(char, config, delay=15)
    except OSError as exc:
        print(f" > Anchor error: {exc}")


def monitor_saves(config: AppConfig) -> None:
    mtimes = {
        char.folder_name: get_latest_save_mtime(config.save_root / char.folder_name)
        if (config.save_root / char.folder_name).exists()
        else 0.0
        for char in config.characters
    }

    console_print("--- Temporal Anchor Protocol Online ---\nMonitoring saves")
    while True:
        time.sleep(5)
        for char in config.characters:
            save_path = config.save_root / char.folder_name
            if not save_path.exists():
                continue
            current_mtime = get_latest_save_mtime(save_path)
            if current_mtime > mtimes[char.folder_name]:
                perform_update(char, config)
                mtimes[char.folder_name] = current_mtime


def main() -> None:
    config = initialize_system()
    monitor_saves(config)
