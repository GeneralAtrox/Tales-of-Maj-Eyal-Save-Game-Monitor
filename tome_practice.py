from __future__ import annotations

import json
import re
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

from game_data.boss_templates import BossActorRef, get_boss_template, load_boss_actor_refs
from game_data.lua_extractor import find_tome_team

_REPO_ROOT = Path(__file__).resolve().parent
_ADDON_SOURCE_DIR = _REPO_ROOT / "assets" / "tome-codex-practice-runner"
_ADDON_DIR_NAME = "tome-codex-practice-runner"
_ADDON_SHORT_NAME = "codex-practice-runner"
_BOOT_ADDON_SOURCE_DIR = _REPO_ROOT / "assets" / "boot-codex-practice-loader"
_BOOT_ADDON_DIR_NAME = "boot-codex-practice-loader"
_BOOT_ADDON_SHORT_NAME = "codex-practice-boot-loader"
_PRACTICE_LAUNCHER_NAME = "t-engine-codex-practice.exe"
_PRACTICE_RUNTIME_ROOT = Path(tempfile.gettempdir()) / "codex-tome-practice"
_PRACTICE_WINDOW_SIZE = "1280x720 Windowed"
_DEFAULT_TURN_CAP = 200
_RESULT_TIMEOUT_SECONDS = 300
_QUICK_ESTIMATE_UNDER_RATIO = 0.9
_DAMAGE_TYPE_LABELS = {
    "physical": "PHYSICAL",
    "arcane": "ARCANE",
    "fire": "FIRE",
    "cold": "COLD",
    "lightning": "LIGHTNING",
    "acid": "ACID",
    "nature": "NATURE",
    "blight": "BLIGHT",
    "light": "LIGHT",
    "darkness": "DARKNESS",
    "mind": "MIND",
    "temporal": "TEMPORAL",
    "steam": "STEAM",
}


class PracticeLaunchError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class PracticeLaunchInfo:
    clone_name: str
    clone_path: Path
    practice_user_root: Path
    scenario_path: Path
    result_path: Path
    launcher_path: Path
    used_shared_launcher: bool
    template_key: str
    template_label: str


@dataclass(frozen=True, slots=True)
class PracticeDamageEvent:
    turn: int
    source: str
    source_role: str
    target: str
    target_role: str
    amount: float
    damage_type: str
    message: str


@dataclass(frozen=True, slots=True)
class AutoPracticeResult:
    launch: PracticeLaunchInfo
    status: str
    winner: str = ""
    turns: int = 0
    reason: str = ""
    detail: str = ""
    damage_events: tuple[PracticeDamageEvent, ...] = ()


def summarize_damage_calibration(
    damage_events: tuple[PracticeDamageEvent, ...],
    *,
    quick_expected_damage: float | None = None,
    quick_damage_type: str = "",
    limit: int = 3,
) -> tuple[str, ...]:
    """Return compact engine-vs-quick-estimate lines for the simulator UI."""
    incoming_hits = sorted(
        (
            event
            for event in damage_events
            if event.target_role == "player" and event.amount > 0
        ),
        key=lambda event: event.amount,
        reverse=True,
    )
    if not incoming_hits:
        if damage_events:
            return (f"Damage events: {len(damage_events)} recorded",)
        return ()

    max_hit = incoming_hits[0]
    max_type = f" {max_hit.damage_type}" if max_hit.damage_type else ""
    lines = [f"Engine max incoming hit: {max_hit.amount:.1f}{max_type} from {max_hit.source or 'unknown'}"]
    by_type = _incoming_damage_by_type(incoming_hits)
    if by_type:
        lines.append("Engine incoming by type: " + ", ".join(by_type))
    if quick_expected_damage is not None and max_hit.amount > 0:
        ratio = quick_expected_damage / max_hit.amount
        lines.append(f"Quick estimate: {quick_expected_damage:.1f} ({ratio:.2f}x engine max)")
        if ratio < _QUICK_ESTIMATE_UNDER_RATIO:
            shortfall = (1.0 - ratio) * 100.0
            lines.append(f"Warning: quick estimate is {shortfall:.0f}% below the engine max hit")
        if max_hit.damage_type and quick_damage_type:
            normalized_quick_type = quick_damage_type.strip().upper()
            if max_hit.damage_type != normalized_quick_type:
                lines.append(f"Damage type mismatch: engine {max_hit.damage_type}, quick {normalized_quick_type}")

    lines.append("Top incoming hits:")
    for event in incoming_hits[: max(1, limit)]:
        message = f" - {event.message}" if event.message else ""
        dtype = f" {event.damage_type}" if event.damage_type else ""
        lines.append(f"  T{event.turn}: {event.amount:.1f}{dtype} from {event.source or 'unknown'}{message}")
    return tuple(lines)


def _incoming_damage_by_type(events: list[PracticeDamageEvent], limit: int = 4) -> tuple[str, ...]:
    max_by_type: dict[str, float] = {}
    for event in events:
        damage_type = event.damage_type or "UNKNOWN"
        max_by_type[damage_type] = max(max_by_type.get(damage_type, 0.0), event.amount)
    ordered = sorted(max_by_type.items(), key=lambda item: item[1], reverse=True)
    return tuple(f"{damage_type} {amount:.1f}" for damage_type, amount in ordered[: max(1, limit)])


def launch_manual_practice(
    *,
    save_root: Path,
    folder_name: str,
    template_key: str,
) -> PracticeLaunchInfo:
    launch = prepare_practice_launch(
        save_root=save_root,
        folder_name=folder_name,
        template_key=template_key,
        mode="manual",
    )
    _launch_tome(launch)
    return launch


def run_auto_practice(
    *,
    save_root: Path,
    folder_name: str,
    template_key: str,
    timeout_seconds: int = _RESULT_TIMEOUT_SECONDS,
) -> AutoPracticeResult:
    launch = prepare_practice_launch(
        save_root=save_root,
        folder_name=folder_name,
        template_key=template_key,
        mode="auto",
    )
    process = _launch_tome(launch)

    deadline = time.monotonic() + max(30, timeout_seconds)
    while time.monotonic() < deadline:
        result = _read_result_file(launch)
        if result is not None:
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                pass
            return result
        if process.poll() is not None:
            break
        time.sleep(0.5)

    result = _read_result_file(launch)
    if result is not None:
        return result

    status = "Simulation exited without a result file."
    if process.poll() is None:
        status = f"Simulation timed out after {timeout_seconds}s."
        try:
            process.kill()
        except OSError:
            pass
    return AutoPracticeResult(launch=launch, status=status)


def prepare_practice_launch(
    *,
    save_root: Path,
    folder_name: str,
    template_key: str,
    mode: str,
) -> PracticeLaunchInfo:
    template = get_boss_template(template_key)
    if template is None:
        raise PracticeLaunchError(f"Unknown boss template: {template_key}")
    actor_refs = load_boss_actor_refs(template_key)
    if not actor_refs:
        raise PracticeLaunchError(f"Could not resolve a game actor for {template.name}.")

    save_root = save_root.expanduser().resolve()
    source_save = save_root / folder_name
    if not source_save.is_dir():
        raise PracticeLaunchError(f"Save folder not found: {source_save}")

    clone_name = _build_clone_name(folder_name)
    tome_root = _resolve_tome_root()
    launcher_path, used_shared_launcher = _resolve_launcher(tome_root)
    practice_user_root, practice_engine_root, practice_module_root, practice_save_root = _prepare_practice_home(
        save_root=save_root,
        clone_name=clone_name,
    )
    addon_dir = practice_engine_root / "addons" / _ADDON_DIR_NAME
    addon_dir.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(_ADDON_SOURCE_DIR, addon_dir, dirs_exist_ok=True)
    boot_addon_dir = practice_engine_root / "addons" / _BOOT_ADDON_DIR_NAME
    shutil.copytree(_BOOT_ADDON_SOURCE_DIR, boot_addon_dir, dirs_exist_ok=True)

    practice_root = practice_module_root / "codex-practice"
    scenarios_dir = practice_root / "scenarios"
    results_dir = practice_root / "results"
    scenarios_dir.mkdir(parents=True, exist_ok=True)
    results_dir.mkdir(parents=True, exist_ok=True)

    clone_path = practice_save_root / clone_name
    shutil.copytree(source_save, clone_path)
    _patch_clone_desc(clone_path / "desc.lua", clone_name)

    scenario_path = scenarios_dir / f"{clone_name}.lua"
    result_path = results_dir / f"{clone_name}.json"
    _write_scenario_file(
        scenario_path=scenario_path,
        result_path=result_path,
        mode=mode,
        template_key=template_key,
        template_label=template.display_label,
        actor_refs=actor_refs,
    )
    return PracticeLaunchInfo(
        clone_name=clone_name,
        clone_path=clone_path,
        practice_user_root=practice_user_root,
        scenario_path=scenario_path,
        result_path=result_path,
        launcher_path=launcher_path,
        used_shared_launcher=used_shared_launcher,
        template_key=template_key,
        template_label=template.display_label,
    )


def _resolve_tome_root() -> Path:
    tome_team = find_tome_team()
    if tome_team is None:
        raise PracticeLaunchError("Could not locate the ToME install.")
    tome_root = tome_team.parent.parent.parent
    executable = tome_root / "t-engine.exe"
    if not executable.is_file():
        raise PracticeLaunchError(f"Could not locate t-engine.exe at {executable}")
    return tome_root


def _resolve_launcher(tome_root: Path) -> tuple[Path, bool]:
    shared_launcher = tome_root / "t-engine.exe"
    practice_launcher = tome_root / _PRACTICE_LAUNCHER_NAME
    try:
        if (not practice_launcher.exists()) or (
            shared_launcher.stat().st_mtime > practice_launcher.stat().st_mtime
        ):
            shutil.copy2(shared_launcher, practice_launcher)
        return practice_launcher, False
    except OSError:
        return shared_launcher, True


def _launch_tome(launch: PracticeLaunchInfo) -> subprocess.Popen[str]:
    command = _build_launch_command(launch)
    return subprocess.Popen(
        command,
        cwd=str(launch.launcher_path.parent),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        text=True,
    )


def _build_launch_command(launch: PracticeLaunchInfo) -> list[str]:
    extra_script = _build_extra_script(launch.scenario_path, launch.result_path)
    return [
        str(launch.launcher_path),
        "--home",
        str(launch.practice_user_root),
        "--no-web",
        "-Mboot",
        "-uboot",
        f"-E{extra_script}",
    ]


def _build_extra_script(scenario_path: Path, result_path: Path) -> str:
    forward_info = (
        f"codex_practice_scenario_path={_lua_long_string(str(scenario_path))}; "
        f"codex_practice_result_path={_lua_long_string(str(result_path))};"
    )
    return (
        f"set_addons={{'{_BOOT_ADDON_SHORT_NAME}'}}; "
        "codex_boot_module='tome'; "
        f"codex_boot_save_name={_lua_long_string(scenario_path.stem)}; "
        f"codex_boot_forward_info={_lua_long_string(forward_info)}; "
        f"codex_practice_result_path={_lua_long_string(str(result_path))};"
    )


def _module_root(save_root: Path) -> Path:
    return save_root.expanduser().resolve().parent


def _engine_root(save_root: Path) -> Path:
    return _module_root(save_root).parent


def _build_clone_name(folder_name: str) -> str:
    stem = re.sub(r"[^a-z0-9_]+", "_", folder_name.casefold()).strip("_") or "character"
    return f"codex_practice_{stem}_{int(time.time())}"


def _prepare_practice_home(*, save_root: Path, clone_name: str) -> tuple[Path, Path, Path, Path]:
    source_engine_root = _engine_root(save_root)
    source_module_root = _module_root(save_root)

    practice_user_root = _PRACTICE_RUNTIME_ROOT / clone_name
    if practice_user_root.exists():
        shutil.rmtree(practice_user_root)

    practice_engine_root = practice_user_root / source_engine_root.parent.name / source_engine_root.name
    practice_module_root = practice_engine_root / source_module_root.name
    practice_save_root = practice_module_root / "save"

    _copy_tree_if_exists(source_engine_root / "settings", practice_engine_root / "settings")
    _copy_tree_if_exists(source_engine_root / "addons", practice_engine_root / "addons")
    _copy_tree_if_exists(source_engine_root / "boot" / "addons", practice_engine_root / "addons")
    _copy_tree_if_exists(source_module_root / "addons", practice_engine_root / "addons")
    _write_practice_resolution_cfg(practice_engine_root / "settings" / "resolution.cfg")
    return practice_user_root, practice_engine_root, practice_module_root, practice_save_root


def _copy_tree_if_exists(source: Path, destination: Path) -> None:
    if not source.is_dir():
        return
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(source, destination, dirs_exist_ok=True)


def _write_practice_resolution_cfg(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"window.size = '{_PRACTICE_WINDOW_SIZE}'\n", encoding="utf-8")


def _patch_clone_desc(desc_path: Path, clone_name: str) -> None:
    try:
        text = desc_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise PracticeLaunchError(f"Could not read {desc_path}: {exc}") from exc

    text = _replace_or_append_line(text, "short_name", f'short_name = "{clone_name}"')
    text = _replace_or_append_line(text, "addons", _format_addons_line(text))

    try:
        desc_path.write_text(text, encoding="utf-8")
    except OSError as exc:
        raise PracticeLaunchError(f"Could not update {desc_path}: {exc}") from exc


def _replace_or_append_line(text: str, field_name: str, replacement: str) -> str:
    pattern = re.compile(rf"^{re.escape(field_name)}\s*=.*$", re.MULTILINE)
    if pattern.search(text):
        return pattern.sub(replacement, text, count=1)
    suffix = "" if text.endswith("\n") else "\n"
    return f"{text}{suffix}{replacement}\n"


def _format_addons_line(text: str) -> str:
    addons: list[str] = []
    match = re.search(r"^addons\s*=\s*\{(.*)\}\s*$", text, re.MULTILINE)
    if match is not None:
        addons.extend(re.findall(r"'([^']+)'", match.group(1)))
    if _ADDON_SHORT_NAME not in addons:
        addons.append(_ADDON_SHORT_NAME)
    formatted = ", ".join(f"'{addon}'" for addon in addons)
    return f"addons = {{{formatted}}}"


def _write_scenario_file(
    *,
    scenario_path: Path,
    result_path: Path,
    mode: str,
    template_key: str,
    template_label: str,
    actor_refs: tuple[BossActorRef, ...],
) -> None:
    positions = _enemy_positions(len(actor_refs))
    actor_lines = []
    for index, actor in enumerate(actor_refs):
        x, y = positions[index]
        actor_lines.append(
            "    {"
            f"name = {_lua_long_string(actor.name)}, "
            f"define_as = {_lua_long_string(actor.define_as)}, "
            f"source_path = {_lua_long_string(actor.source_path)}, "
            f"x = {x}, y = {y}"
            "},"
        )
    body = "\n".join(actor_lines)
    text = (
        "return {\n"
        f"  mode = {_lua_long_string(mode)},\n"
        f"  template_key = {_lua_long_string(template_key)},\n"
        f"  template_label = {_lua_long_string(template_label)},\n"
        f"  result_path = {_lua_long_string(str(result_path))},\n"
        f"  turn_cap = {_DEFAULT_TURN_CAP},\n"
        "  player = { x = 4, y = 8 },\n"
        "  actors = {\n"
        f"{body}\n"
        "  },\n"
        "}\n"
    )
    scenario_path.write_text(text, encoding="utf-8")


def _enemy_positions(count: int) -> list[tuple[int, int]]:
    defaults = [(11, 8), (11, 6), (11, 10), (10, 8)]
    if count <= len(defaults):
        return defaults[:count]
    positions = list(defaults)
    while len(positions) < count:
        offset = len(positions) - len(defaults) + 1
        positions.append((11 + (offset % 2), 8 + offset))
    return positions


def _lua_long_string(value: str) -> str:
    normalized = value.replace("\r\n", "\n").replace("\r", "\n")
    for pad_count in range(6):
        equals = "=" * pad_count
        close = f"]{equals}]"
        if close not in normalized:
            return f"[{equals}[{normalized}]{equals}]"
    escaped = normalized.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _read_result_file(launch: PracticeLaunchInfo) -> AutoPracticeResult | None:
    if not launch.result_path.is_file():
        return None
    try:
        data = json.loads(launch.result_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return AutoPracticeResult(launch=launch, status="Simulation produced an unreadable result file.")
    return AutoPracticeResult(
        launch=launch,
        status=str(data.get("status") or ""),
        winner=str(data.get("winner") or ""),
        turns=int(data.get("turns") or 0),
        reason=str(data.get("reason") or ""),
        detail=str(data.get("detail") or ""),
        damage_events=_parse_damage_events(data.get("damage_events")),
    )


def _parse_damage_events(raw_events: object) -> tuple[PracticeDamageEvent, ...]:
    if not isinstance(raw_events, list):
        return ()
    events: list[PracticeDamageEvent] = []
    for raw in raw_events:
        if not isinstance(raw, dict):
            continue
        events.append(
            PracticeDamageEvent(
                turn=int(raw.get("turn") or 0),
                source=str(raw.get("source") or ""),
                source_role=str(raw.get("source_role") or ""),
                target=str(raw.get("target") or ""),
                target_role=str(raw.get("target_role") or ""),
                amount=float(raw.get("amount") or 0.0),
                damage_type=_damage_type_from_event(raw),
                message=str(raw.get("message") or ""),
            )
        )
    return tuple(events)


def _damage_type_from_event(raw: dict[str, object]) -> str:
    explicit = str(raw.get("damage_type") or "").strip().upper()
    if explicit:
        return explicit
    return _damage_type_from_message(str(raw.get("message") or ""))


def _damage_type_from_message(message: str) -> str:
    normalized = message.casefold()
    for label, damage_type in sorted(_DAMAGE_TYPE_LABELS.items(), key=lambda item: len(item[0]), reverse=True):
        if re.search(rf"\b{re.escape(label)}\b", normalized):
            return damage_type
    return ""
