import os
import shutil
from datetime import datetime


def create_backup(char, config):
    bak_dir = os.path.join(config.save_root, "Backups", char.folder_name)
    os.makedirs(bak_dir, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_path = os.path.join(bak_dir, f"backup_{timestamp}")
    suffix = 1
    while os.path.exists(backup_path):
        backup_path = os.path.join(bak_dir, f"backup_{timestamp}_{suffix}")
        suffix += 1

    shutil.copytree(os.path.join(config.save_root, char.folder_name), backup_path)
    backups = sorted(
        [os.path.join(bak_dir, item) for item in os.listdir(bak_dir) if os.path.isdir(os.path.join(bak_dir, item))],
        key=os.path.getmtime,
    )
    while len(backups) > config.backup_limit:
        shutil.rmtree(backups.pop(0))


def ensure_baseline_backup(char, config):
    save_path = os.path.join(config.save_root, char.folder_name)
    if not os.path.exists(save_path):
        return

    bak_dir = os.path.join(config.save_root, "Backups", char.folder_name)
    existing_backups = []
    if os.path.isdir(bak_dir):
        existing_backups = [item for item in os.listdir(bak_dir) if os.path.isdir(os.path.join(bak_dir, item))]
    if existing_backups:
        return

    try:
        create_backup(char, config)
        print(f"[{datetime.now().strftime('%H:%M:%S')}] {char.name} baseline anchored.")
    except Exception as exc:
        print(f" > Baseline anchor error: {exc}")


def get_latest_save_mtime(path):
    return max([os.path.getmtime(os.path.join(root, file_name)) for root, _, files in os.walk(path) for file_name in files] or [0])
