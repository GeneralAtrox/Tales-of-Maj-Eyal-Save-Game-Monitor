import json
import os
import time

from backups import create_backup, ensure_baseline_backup, get_latest_save_mtime
from models import AppConfig, CharacterConfig
from parsers import parse_desc_lua, vault_name_matches
from te4_client import discover_profile_id, get_vault_ids_from_profile, schedule_scrying_sync


CONFIG_FILE = 'config.json'


def load_config(config_path=CONFIG_FILE):
    if not os.path.exists(config_path):
        return AppConfig()

    with open(config_path, 'r', encoding='utf-8') as file_handle:
        return AppConfig.from_dict(json.load(file_handle))


def save_config(config, config_path=CONFIG_FILE):
    with open(config_path, 'w', encoding='utf-8') as file_handle:
        json.dump(config.to_dict(), file_handle, indent=4)


def auto_discover_characters(save_root, existing_chars, config):
    existing_folders = {char.folder_name: char for char in existing_chars}
    needs_saving = False
    new_added = []

    if not os.path.isdir(save_root):
        print(f"[!] Save root does not exist: {save_root}")
        return existing_chars, False, []

    local_alive = []
    for folder in os.listdir(save_root):
        path = os.path.join(save_root, folder)
        desc = os.path.join(path, 'desc.lua')
        if not os.path.isdir(path) or not os.path.exists(desc):
            continue

        lua_data = parse_desc_lua(desc)
        short_name = lua_data.get('short_name')
        name = lua_data.get('name')
        if not short_name or not name:
            print(f"[!] Skipping {folder}: desc.lua is missing short_name or name.")
            continue
        if short_name not in existing_folders and lua_data.get('loadable'):
            local_alive.append(CharacterConfig(folder_name=short_name, name=name))

    if not local_alive:
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
        matches = [{'vid': vid, 'name': name} for vid, name in roster.items() if vault_name_matches(name, char.name)]

        if len(matches) == 1:
            print(f"    -> Match found! {matches[0]['name']} saved.")
            char.vault_id = matches[0]['vid']
        else:
            char.vault_id = input(f"    Vault ID not found or multiple matches. Paste manually for {char.name}: ").strip()

        existing_chars.append(char)
        new_added.append(char)
        needs_saving = True

    return existing_chars, needs_saving, new_added


def initialize_system(config_path=CONFIG_FILE):
    config = load_config(config_path)
    config.characters, needs_save, new_chars = auto_discover_characters(config.save_root, config.characters, config)
    if needs_save:
        save_config(config, config_path)
        print(f"[*] Configuration updated and saved to {config_path}.")

    os.makedirs(os.path.join(config.save_root, "Backups"), exist_ok=True)
    for char in config.characters:
        ensure_baseline_backup(char, config)
    for char in new_chars:
        schedule_scrying_sync(char, config)
    return config


def perform_update(char, config):
    time.sleep(2)
    try:
        create_backup(char, config)
        print(f"[{time.strftime('%H:%M:%S')}] {char.name} timeline anchored.")
        schedule_scrying_sync(char, config, delay=15)
    except Exception as exc:
        print(f" > Anchor error: {exc}")


def monitor_saves(config):
    mtimes = {}
    for char in config.characters:
        path = os.path.join(config.save_root, char.folder_name)
        mtimes[char.folder_name] = get_latest_save_mtime(path) if os.path.exists(path) else 0

    print("--- Temporal Anchor Protocol Online ---\nMonitoring saves")
    while True:
        time.sleep(5)
        for char in config.characters:
            path = os.path.join(config.save_root, char.folder_name)
            if not os.path.exists(path):
                continue
            cur_mtime = get_latest_save_mtime(path)
            if cur_mtime > mtimes[char.folder_name]:
                perform_update(char, config)
                mtimes[char.folder_name] = cur_mtime


def main():
    config = initialize_system()
    monitor_saves(config)
