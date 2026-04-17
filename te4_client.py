from __future__ import annotations

import datetime
import json
import re
import threading
import urllib.parse
from typing import TYPE_CHECKING, Any, Final

from parsers import extract_optimized_data, get_beautiful_soup, vault_name_matches

if TYPE_CHECKING:
    from models import AppConfig, CharacterConfig


REQUEST_TIMEOUT: Final[tuple[int, int]] = (5, 20)
PROFILE_ID_PATTERN: Final[re.Pattern[str]] = re.compile(r'href="/user/(\d+)/characters"')
CHARACTER_LINK_PATTERN: Final[re.Pattern[str]] = re.compile(r"/characters/\d+/tome/[a-fA-F0-9\-]{36}")
VAULT_ID_PATTERN: Final[re.Pattern[str]] = re.compile(r"/characters/\d+/tome/([a-fA-F0-9\-]{36})")

_SYNC_TIMERS: dict[str, threading.Timer] = {}
_SYNC_TIMERS_LOCK = threading.Lock()
_REQUESTS_SESSION: Any | None = None


def get_requests_module() -> Any:
    try:
        import requests
    except ModuleNotFoundError as exc:
        raise RuntimeError(
            "requests is required for TE4 syncing. Install it with `py -3.14 -m pip install requests`."
        ) from exc
    return requests


def get_requests_session() -> tuple[Any, Any]:
    global _REQUESTS_SESSION
    requests = get_requests_module()
    if _REQUESTS_SESSION is None:
        session = requests.Session()
        session.headers.update({"User-Agent": "TOME-SaveMonitor/1.0"})
        _REQUESTS_SESSION = session
    return requests, _REQUESTS_SESSION


def get_profile_ids_from_char_name(char_name: str) -> list[str]:
    """Search the vault for candidate owner profile IDs."""
    try:
        requests, session = get_requests_session()
        query = urllib.parse.quote_plus(char_name)
        response = session.get(f"https://te4.org/characters-vault?tag_name={query}", timeout=REQUEST_TIMEOUT)
        response.raise_for_status()
        return list(dict.fromkeys(PROFILE_ID_PATTERN.findall(response.text)))
    except RuntimeError as exc:
        print(f"    -> Profile search failed: {exc}")
    except requests.RequestException as exc:
        print(f"    -> Profile search failed: {exc}")
    return []


def get_vault_ids_from_profile(profile_id: str) -> dict[str, str]:
    """Scrape a TE4 profile page for living vault character IDs."""
    alive_chars: dict[str, str] = {}
    try:
        requests, session = get_requests_session()
        BeautifulSoup = get_beautiful_soup()
        response = session.get(f"https://te4.org/user/{profile_id}/characters", timeout=REQUEST_TIMEOUT)
        response.raise_for_status()
        soup = BeautifulSoup(response.text, "html.parser")
        for link in soup.find_all("a", href=CHARACTER_LINK_PATTERN):
            if "#FF0000" in link.get("style", ""):
                continue
            if match := VAULT_ID_PATTERN.search(link["href"]):
                alive_chars[match.group(1)] = link.get_text(strip=True)
    except RuntimeError as exc:
        print(f"    -> Could not load TE4 roster for profile {profile_id}: {exc}")
    except requests.RequestException as exc:
        print(f"    -> Could not load TE4 roster for profile {profile_id}: {exc}")
    return alive_chars


def discover_profile_id(local_chars: list[CharacterConfig]) -> tuple[str, dict[str, str]]:
    """Validate an inferred TE4 profile ID against the locally discovered characters."""
    if not local_chars:
        return "", {}

    print(f"[*] Discovering owner Profile ID using: {local_chars[0].name}")
    candidate_ids = get_profile_ids_from_char_name(local_chars[0].name)
    if not candidate_ids:
        print("    -> No candidate TE4 profiles found.")
        return "", {}

    ranked_candidates: list[tuple[int, str, dict[str, str]]] = []
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


def sync_scrying_mirror(char_info: CharacterConfig, config: AppConfig, *, has_transmo: bool = True) -> None:
    if not char_info.vault_id or not config.profile_id:
        return

    print(f" > Syncing {char_info.name} with Te4 Vault...")
    try:
        requests, session = get_requests_session()
        response = session.get(
            f"https://te4.org/characters/{config.profile_id}/tome/{char_info.vault_id}",
            timeout=REQUEST_TIMEOUT,
        )
        response.raise_for_status()
        vault_url = f"https://te4.org/characters/{config.profile_id}/tome/{char_info.vault_id}"
        data = {
            "_meta": {
                "generated_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                "vault_url": vault_url,
                "schema_version": "1",
            },
            **extract_optimized_data(response.text, has_transmo=has_transmo),
        }
        config.character_sheets_root.mkdir(exist_ok=True)
        out_path = config.character_sheets_root / f"data_{char_info.folder_name}.json"
        out_path.write_text(json.dumps(data, indent=4), encoding="utf-8")
        print(" > Scrying mirror updated successfully.")
    except RuntimeError as exc:
        print(f" > Sync error: {exc}")
    except requests.RequestException as exc:
        print(f" > Sync error: {exc}")
    except OSError as exc:
        print(f" > Sync error: {exc}")


def schedule_scrying_sync(char_info: CharacterConfig, config: AppConfig, delay: float = 0, *, has_transmo: bool = True) -> None:
    """Schedule a debounced vault sync so backup monitoring stays responsive."""
    if not char_info.vault_id or not config.profile_id:
        return

    timer: threading.Timer | None = None

    def run_sync() -> None:
        try:
            sync_scrying_mirror(char_info, config, has_transmo=has_transmo)
        finally:
            with _SYNC_TIMERS_LOCK:
                if _SYNC_TIMERS.get(char_info.folder_name) is timer:
                    _SYNC_TIMERS.pop(char_info.folder_name, None)

    timer = threading.Timer(delay, run_sync)
    timer.daemon = True
    with _SYNC_TIMERS_LOCK:
        if existing_timer := _SYNC_TIMERS.get(char_info.folder_name):
            existing_timer.cancel()
        _SYNC_TIMERS[char_info.folder_name] = timer
    timer.start()
