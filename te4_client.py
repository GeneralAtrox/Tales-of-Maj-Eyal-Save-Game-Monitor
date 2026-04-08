import json
import os
import re
import threading
import urllib.parse

from parsers import extract_optimized_data, get_beautiful_soup, vault_name_matches


REQUEST_TIMEOUT = (5, 20)

_SYNC_TIMERS = {}
_SYNC_TIMERS_LOCK = threading.Lock()


def get_requests_module():
    try:
        import requests
    except ModuleNotFoundError as exc:
        raise RuntimeError(
            "requests is required for TE4 syncing. Install it with `py -3 -m pip install requests`."
        ) from exc
    return requests


def get_profile_ids_from_char_name(char_name):
    """Searches the vault for candidate owner Profile IDs."""
    try:
        requests = get_requests_module()
        query = urllib.parse.quote_plus(char_name)
        url = f"https://te4.org/characters-vault?tag_name={query}"
        response = requests.get(url, timeout=REQUEST_TIMEOUT)
        if response.status_code == 200:
            candidate_ids = []
            for profile_id in re.findall(r'href="/user/(\d+)/characters"', response.text):
                if profile_id not in candidate_ids:
                    candidate_ids.append(profile_id)
            return candidate_ids
    except RuntimeError as exc:
        print(f"    -> Profile search failed: {exc}")
    except Exception as exc:
        print(f"    -> Profile search failed: {exc}")
    return []


def get_vault_ids_from_profile(profile_id):
    """Scrapes the user's character page for all living Vault IDs."""
    alive_chars = {}
    try:
        requests = get_requests_module()
        BeautifulSoup = get_beautiful_soup()
        url = f"https://te4.org/user/{profile_id}/characters"
        response = requests.get(url, timeout=REQUEST_TIMEOUT)
        if response.status_code == 200:
            soup = BeautifulSoup(response.text, 'html.parser')
            char_links = soup.find_all('a', href=re.compile(r'/characters/\d+/tome/[a-fA-F0-9\-]{36}'))
            for link in char_links:
                if '#FF0000' in link.get('style', ''):
                    continue
                match = re.search(r'/characters/\d+/tome/([a-fA-F0-9\-]{36})', link['href'])
                if match:
                    alive_chars[match.group(1)] = link.get_text(strip=True)
    except Exception as exc:
        print(f"    -> Could not load TE4 roster for profile {profile_id}: {exc}")
    return alive_chars


def discover_profile_id(local_chars):
    """Validates an inferred TE4 profile ID against the locally discovered characters."""
    if not local_chars:
        return "", {}

    print(f"[*] Discovering owner Profile ID using: {local_chars[0].name}")
    candidate_ids = get_profile_ids_from_char_name(local_chars[0].name)
    if not candidate_ids:
        print("    -> No candidate TE4 profiles found.")
        return "", {}

    ranked_candidates = []
    for profile_id in candidate_ids:
        roster = get_vault_ids_from_profile(profile_id)
        match_count = sum(
            1
            for char in local_chars
            if any(vault_name_matches(display_name, char.name) for display_name in roster.values())
        )
        if match_count:
            ranked_candidates.append((match_count, profile_id, roster))

    if not ranked_candidates:
        print("    -> Could not validate any TE4 profile candidates against local saves.")
        return "", {}

    ranked_candidates.sort(key=lambda item: item[0], reverse=True)
    best_match_count, best_profile_id, best_roster = ranked_candidates[0]
    best_matches = [item for item in ranked_candidates if item[0] == best_match_count]
    if best_match_count < len(local_chars) or len(best_matches) > 1:
        print("    -> Automatic profile discovery was ambiguous.")
        return "", {}

    print(f"    -> Profile ID verified: {best_profile_id}")
    return best_profile_id, best_roster


def sync_scrying_mirror(char_info, config):
    if not char_info.vault_id or not config.profile_id:
        return

    print(f" > Syncing {char_info.name} with Te4 Vault...")
    try:
        requests = get_requests_module()
        response = requests.get(
            f"https://te4.org/characters/{config.profile_id}/tome/{char_info.vault_id}",
            timeout=REQUEST_TIMEOUT
        )
        if response.status_code == 200:
            data = extract_optimized_data(response.text)
            out_dir = os.path.join(config.save_root, "CharacterSheets")
            os.makedirs(out_dir, exist_ok=True)
            out_path = os.path.join(out_dir, f"data_{char_info.folder_name}.json")
            with open(out_path, 'w', encoding='utf-8') as file_handle:
                json.dump(data, file_handle, indent=4)
            print(" > Scrying mirror updated successfully.")
        else:
            print(f" > Sync failed with status {response.status_code}.")
    except Exception as exc:
        print(f" > Sync error: {exc}")


def schedule_scrying_sync(char_info, config, delay=0):
    """Schedules a debounced vault sync so backup monitoring stays responsive."""
    if not char_info.vault_id or not config.profile_id:
        return

    timer = None

    def run_sync():
        try:
            sync_scrying_mirror(char_info, config)
        finally:
            with _SYNC_TIMERS_LOCK:
                if _SYNC_TIMERS.get(char_info.folder_name) is timer:
                    _SYNC_TIMERS.pop(char_info.folder_name, None)

    timer = threading.Timer(delay, run_sync)
    timer.daemon = True
    with _SYNC_TIMERS_LOCK:
        existing_timer = _SYNC_TIMERS.get(char_info.folder_name)
        if existing_timer is not None:
            existing_timer.cancel()
        _SYNC_TIMERS[char_info.folder_name] = timer
    timer.start()
