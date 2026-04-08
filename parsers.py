from __future__ import annotations

import re
import sys
from pathlib import Path
from typing import Final, TypedDict


class DescLuaDetails(TypedDict, total=False):
    short_name: str
    name: str
    loadable: bool


class DeathSummary(TypedDict):
    Target_Death: list[str]
    Total_Death_Count: str


type TalentFieldValue = str | list[str]
type TalentRecord = str | dict[str, TalentFieldValue]
type AgentData = dict[str, object]

HEADING_TAGS: Final[tuple[str, ...]] = ("h2", "h3", "h4")
IGNORED_SECTION_TITLES: Final[frozenset[str]] = frozenset({"Features:"})
SIMPLE_KEY_VALUE_SECTIONS: Final[frozenset[str]] = frozenset({
    "Primary Stats",
    "Resources",
    "Speed",
    "Vision",
    "Offense: Mainhand",
    "Offense: Offhand",
    "Offense: Spell",
    "Offense: Mind",
    "Offense: Damage Bonus",
    "Offense: Damage Penetration",
    "Defense: Base",
    "Defense: Resistances",
    "Defense: Immunities",
})
UI_CLUTTER_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"(This item will automatically be transmogrified|Crafted by a master|Infused by psionic forces|Powered by arcane forces)\s*"
)
CHARACTER_NAME_SUFFIX_PATTERN: Final[re.Pattern[str]] = re.compile(r"\s+by\S+$")
SHORT_NAME_PATTERN: Final[re.Pattern[str]] = re.compile(r'(?m)^\s*short_name\b\s*=\s*["\']([^"\']+)["\']')
NAME_PATTERN: Final[re.Pattern[str]] = re.compile(r'(?m)^\s*name\b\s*=\s*["\']([^"\']+)["\']')
LOADABLE_PATTERN: Final[re.Pattern[str]] = re.compile(r'(?m)^\s*loadable\b\s*=\s*(true|false)\b')
TALENT_NAME_PATTERN: Final[re.Pattern[str]] = re.compile(r"([A-Z][A-Za-z' -]+)$")
TALENT_PATTERNS: Final[dict[str, re.Pattern[str]]] = {
    "Range": re.compile(r"Range:\s*(.*?)(?=\s+(?:Cooldown|Travel Speed|Usage Speed|Is:|Description:|$))"),
    "Cooldown": re.compile(r"Cooldown:\s*(.*?)(?=\s+(?:Travel Speed|Usage Speed|Is:|Description:|$))"),
    "Travel Speed": re.compile(r"Travel Speed:\s*(.*?)(?=\s+(?:Usage Speed|Is:|Description:|$))"),
    "Usage Speed": re.compile(r"Usage Speed:\s*(.*?)(?=\s+(?:Is:|Description:|$))"),
}
TALENT_STAT_PER_TURN_PATTERN: Final[re.Pattern[str]] = re.compile(r"([+-]?\d+(?:\.\d+)?\s+[A-Za-z][A-Za-z ]*/turn)")
TALENT_STAT_BONUS_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"(?:(?:your|to|perform a|gain|gaining)\s+)?([A-Za-z][A-Za-z '()-]+?)\s+by\s+([+-]?\d+(?:\.\d+)?%?)",
    flags=re.IGNORECASE,
)
TALENT_SCALE_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"(?:increase|improve|scale)(?:s|d)?\s+(?:with|based on)\s+(?:your\s+)?([A-Z][A-Za-z]+(?:\s+and\s+[A-Z][A-Za-z]+)*)",
)
TALENT_DURATION_PATTERN: Final[re.Pattern[str]] = re.compile(r"(?:for|lasts?|duration of)\s+(\d+\s+turns?)", flags=re.IGNORECASE)
ITEM_NAME_PATTERN: Final[re.Pattern[str]] = re.compile(r"^(?:[^|]+\s+\|\s+)?(?P<name>.+?)\s+\d+(?:\.\d+)?\s+Encumbrance\.")
ITEM_TIER_PATTERN: Final[re.Pattern[str]] = re.compile(r"\btier\s+\d+\b")
ITEM_TAG_PATTERN: Final[re.Pattern[str]] = re.compile(r"\[[^\]]+\]")
ITEM_ENCUMBRANCE_PATTERN: Final[re.Pattern[str]] = re.compile(r"\s+\d+(?:\.\d+)?\s+Encumbrance\.")
ITEM_NAME_TRAILING_NOISE_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"\s+(?:Powered by unknown forces|Powered by arcane forces|Infused by psionic forces|Infused by nature)\b.*$"
)
ITEM_REQUIREMENTS_PATTERN: Final[re.Pattern[str]] = re.compile(r"\s+Requires:\s+.*$")
ITEM_FLAVOR_START_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"\s+(?:"
    r"These boots feel|Touching them|A normal brass lantern|A pointy cloth hat|A large helmet|A cloth coat|A cloth vestment|"
    r"A belt that goes around your waist|Light gloves which|Magical wands are made|Magical runes may be inscribed|"
    r"Rings make your fingers look great|Amulets make your neck look great|Staves designed for wielders of magic|"
    r"This ordinary blade|This simple appearance belies|This strange creature seems|Try to not die|"
    r"\"?An innocuous bauble|Ventilation and bad vision can be a problem|It is spacious enough to be worn"
    r").*$"
)
INSCRIPTION_NAME_PATTERN: Final[re.Pattern[str]] = re.compile(r"((?:Rune|Infusion|Taint|Torque): [^.]+)$")
INSCRIPTION_DESCRIPTION_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"Description:\s*(.*?)(?=\s+(?:Rune|Infusion|Taint|Torque):\s|$)"
)
ACTIVATION_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"It can be used to (.*?)(?=\s+Activation puts|\s+Range:|\s+Cooldown:|\s+Travel Speed:|\s+Usage Speed:|\s+Description:|$)"
)
ACTIVATION_COOLDOWN_PATTERN: Final[re.Pattern[str]] = re.compile(r"Activation puts .*? cooldown for \d+\s+turns?\.", flags=re.IGNORECASE)
ITEM_DESCRIPTION_PATTERN: Final[re.Pattern[str]] = re.compile(r"Description:\s*(.*)$")
ITEM_SECTION_MARKER_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"\s*(?:When wielded/worn|When inscribed on your body|When carried):\s*"
)
EFFECT_TYPE_PATTERN: Final[re.Pattern[str]] = re.compile(r"^(.*?)\s+\|\s+(.*)$")
ITEM_FIELD_LABELS: Final[tuple[str, ...]] = (
    "Accuracy bonus",
    "Accuracy",
    "Activation costs",
    "Armour Hardiness",
    "Armour Penetration",
    "Armour penetration",
    "Armour",
    "Attack speed",
    "Base power",
    "Blindness immunity",
    "Capacity",
    "Changes damage",
    "Changes resistances penetration",
    "Changes resistances",
    "Changes stats",
    "Confusion immunity",
    "Crit. chance",
    "Critical mult.",
    "Cut immunity",
    "Damage (Melee)",
    "Damage (Ranged)",
    "Damage against",
    "Damage Shield penetration",
    "Damage type",
    "Damage when hit (Melee)",
    "Defense after a teleport",
    "Defense",
    "Disarm immunity",
    "Disease immunity",
    "Effects when hit in melee",
    "Fatigue",
    "Firing range",
    "Grants spell-crit equal to half of your Shadow Power.",
    "Healing mod.",
    "Infravision radius",
    "Knockback immunity",
    "Latent Damage Type",
    "Life regen",
    "Light radius",
    "Mana each turn",
    "Mana when firing critical spell",
    "Mastery",
    "Maximum encumbrance",
    "Maximum hate",
    "Maximum life",
    "Maximum mana",
    "Maximum stamina",
    "Maximum vim",
    "Maximum wards",
    "Mental crit. chance",
    "Mental save",
    "Mindpower",
    "Movement speed",
    "New effects duration reduction after a teleport",
    "On hit",
    "On shield block",
    "On weapon crit",
    "Only die when reaching",
    "Physical crit. chance",
    "Physical power",
    "Physical save",
    "Pinning immunity",
    "Psi when hit",
    "Range",
    "Ranged Defense",
    "Reduced damage from",
    "Reduces incoming crit damage",
    "Reduces paradox anomalies(equivalent to willpower)",
    "Resist all after a teleport",
    "Shadow Power",
    "Size category",
    "Spell crit. chance",
    "Spell save",
    "Spellpower on spell critical (stacks up to 3 times)",
    "Spellpower",
    "Stamina each turn",
    "Stealth bonus",
    "Stun/Freeze immunity",
    "Talent cooldown",
    "Talent granted",
    "Talent on hit(spell)",
    "Talent mastery",
    "Talents granted",
    "Teleport immunity",
    "Uses stat",
    "Uses stats",
    "Vim when firing critical spell",
    "When attacking in melee",
    "When carried",
    "When hits",
    "When used as an alchemist bomb",
)
ITEM_LABELED_SEGMENT_PATTERN: Final[re.Pattern[str]] = re.compile(
    rf"(?<!\S)({'|'.join(re.escape(label) for label in sorted(ITEM_FIELD_LABELS, key=len, reverse=True))}):\s*"
)


def get_beautiful_soup():
    try:
        from bs4 import BeautifulSoup
    except ModuleNotFoundError as exc:
        raise RuntimeError(
            "beautifulsoup4 is required for TE4 scraping. Install it with `py -3.14 -m pip install beautifulsoup4`."
        ) from exc
    return BeautifulSoup


def parse_desc_lua(desc_path: Path) -> DescLuaDetails:
    """Extract character metadata from a ToME desc.lua file."""
    details: DescLuaDetails = {"loadable": True}
    try:
        content = desc_path.read_text(encoding="utf-8")
    except OSError as exc:
        print(f"[!] Could not parse {desc_path}: {exc}", file=sys.stderr)
        return details

    if short_match := SHORT_NAME_PATTERN.search(content):
        details["short_name"] = short_match.group(1)
    if name_match := NAME_PATTERN.search(content):
        details["name"] = name_match.group(1)
    if loadable_match := LOADABLE_PATTERN.search(content):
        details["loadable"] = loadable_match.group(1) == "true"
    return details


def extract_optimized_data(html_content: str) -> AgentData:
    """Extract a compact AI-facing JSON view from a TE4 vault page."""
    BeautifulSoup = get_beautiful_soup()
    soup = BeautifulSoup(html_content, "html.parser")
    agent_data: AgentData = {}

    character_title = ""
    if title_tag := soup.find("h2"):
        raw_name = " ".join(title_tag.get_text(strip=True).split())
        character_title = CHARACTER_NAME_SUFFIX_PATTERN.sub("", raw_name)

    for section in soup.find_all(HEADING_TAGS):
        section_title = section.get_text(strip=True)
        if section_title == character_title or section_title in IGNORED_SECTION_TITLES:
            continue

        content_list = _collect_section_content(section)
        if not content_list:
            continue

        if section_title == "Character":
            agent_data["Character"] = _parse_character_section(content_list)
        elif section_title in SIMPLE_KEY_VALUE_SECTIONS:
            agent_data[section_title] = _parse_simple_key_value_section(content_list)
        elif section_title.startswith("Inscriptions"):
            agent_data[section_title] = _parse_inscriptions_section(content_list)
        elif "Talents" in section_title:
            agent_data[section_title] = _parse_talent_section(content_list)
        elif section_title == "Effects":
            agent_data[section_title] = _parse_effects_section(content_list)
        elif section_title == "Quests":
            agent_data[section_title] = _parse_quests_section(content_list)
        elif section_title in {"Equipment", "Inventory"}:
            agent_data[section_title] = _parse_item_section(content_list, include_slot=(section_title == "Equipment"))
        else:
            agent_data[section_title] = content_list

    return agent_data


def vault_name_matches(remote_name: str, local_name: str) -> bool:
    """Match a vault display name against a local character name with a word boundary."""
    normalized_remote = " ".join(remote_name.split())
    normalized_local = " ".join(local_name.split())
    return bool(re.match(rf"^{re.escape(normalized_local)}(?:\b|$)", normalized_remote, flags=re.IGNORECASE))


def _collect_section_content(section) -> list[str]:
    content_list: list[str] = []
    node = section.find_next_sibling()
    while node and node.name not in HEADING_TAGS:
        if node.name == "table":
            content_list.extend(_extract_table_rows(node))
        elif node.name == "ul":
            content_list.extend(" ".join(item.get_text(separator=" ", strip=True).split()) for item in node.find_all("li"))
        node = node.find_next_sibling()
    return content_list


def _extract_table_rows(table) -> list[str]:
    rows: list[str] = []
    for table_row in table.find_all("tr"):
        cells = table_row.find_all(["th", "td"])
        if not cells:
            continue
        row = " | ".join(" ".join(cell.get_text(separator=" ", strip=True).split()) for cell in cells)
        rows.append(UI_CLUTTER_PATTERN.sub("", row))
    return rows


def _parse_character_section(content_list: list[str]) -> dict[str, str | DeathSummary]:
    parsed: dict[str, str | DeathSummary] = {}
    for row in content_list:
        if " | " not in row:
            continue
        key, value = (part.strip() for part in row.split(" | ", 1))
        if key == "Lifes / Deaths":
            death_matches = re.findall(r"Killed by ([^/]+)", value)
            target_death = [death.strip() for death in death_matches if "Spellblaze Crystal at level 3" in death]
            summary_count = value.split("/")[-1].strip() if "/" in value else ""
            parsed[key] = {
                "Target_Death": target_death,
                "Total_Death_Count": summary_count,
            }
        elif key not in {"Addons", "Features:"}:
            parsed[key] = value
    return parsed


def _parse_simple_key_value_section(content_list: list[str]) -> dict[str, str]:
    parsed: dict[str, str] = {}
    for row in content_list:
        if " | " not in row:
            continue
        key, value = (part.strip() for part in row.split(" | ", 1))
        parsed[key] = value
    return parsed


def _parse_inscriptions_section(content_list: list[str]) -> dict[str, str]:
    parsed: dict[str, str] = {}
    for row in content_list:
        key = "Unknown Inscription"
        if name_match := INSCRIPTION_NAME_PATTERN.search(row):
            key = name_match.group(1).strip()

        details: list[str] = []
        for label, pattern in TALENT_PATTERNS.items():
            if matches := pattern.findall(row):
                value: TalentFieldValue = matches if len(matches) > 1 else matches[0]
                if label == "Turn Duration" and isinstance(value, str):
                    value = f"{value} turns"
                details.append(f"{label}: {value}")

        if description_match := INSCRIPTION_DESCRIPTION_PATTERN.search(row):
            description = " ".join(description_match.group(1).split())
            if ". " in description:
                description = description.split(". ", 1)[0].rstrip(".") + "."
            details.append(description)

        parsed[key] = " | ".join(details) if details else row
    return parsed


def _parse_talent_section(content_list: list[str]) -> dict[str, TalentRecord]:
    parsed: dict[str, TalentRecord] = {}
    for row in content_list:
        if " | " not in row:
            continue

        raw_desc, level = (part.strip() for part in row.split(" | ", 1))
        if "Description:" not in raw_desc and re.fullmatch(r"\d+\.\d+", level):
            parsed[raw_desc] = level
            continue

        talent_name = _extract_talent_name(raw_desc)
        description = _extract_talent_description(raw_desc, talent_name)

        details: dict[str, TalentFieldValue] = {"Level": level}
        for label, pattern in TALENT_PATTERNS.items():
            if matches := pattern.findall(raw_desc):
                details[label] = matches[0].strip()

        if stat_per_turn := _extract_talent_stat_per_turn(description):
            details["Stats per turn"] = stat_per_turn
        if stat_bonuses := _extract_talent_stat_bonuses(description):
            details["Stats"] = stat_bonuses
        if scales_with := _extract_talent_scaling(description):
            details["Scales With"] = scales_with
        if turn_duration := _extract_talent_turn_duration(description):
            details["Turn Duration"] = turn_duration

        parsed[talent_name] = details if len(details) > 1 else level
    return parsed


def _parse_effects_section(content_list: list[str]) -> dict[str, str | list[str]]:
    parsed: dict[str, str | list[str]] = {}
    for row in content_list:
        if not (match := EFFECT_TYPE_PATTERN.match(row)):
            continue
        effect_type = match.group(1).strip()
        effect_value = match.group(2).strip()
        if effect_type not in parsed:
            parsed[effect_type] = effect_value
        elif isinstance(parsed[effect_type], list):
            parsed[effect_type].append(effect_value)
        else:
            parsed[effect_type] = [parsed[effect_type], effect_value]
    return parsed


def _parse_quests_section(content_list: list[str]) -> list[str]:
    parsed: list[str] = []
    for row in content_list:
        if " | " not in row:
            parsed.append(row)
            continue
        description, status = row.rsplit(" | ", 1)
        fragments = [fragment.strip(" *") for fragment in re.split(r"\.\s+|!\s+|\s+\*\s+", description) if fragment.strip(" *")]
        title = fragments[-1] if fragments else description.strip()
        parsed.append(f"{title} | {status.strip()}")
    return parsed


def _parse_item_section(content_list: list[str], *, include_slot: bool) -> list[str]:
    return [_format_item_entry(item, include_slot=include_slot) for item in content_list]


def _format_item_entry(entry: str, *, include_slot: bool) -> str:
    slot = None
    body = entry
    if include_slot and " | " in entry:
        slot, body = (part.strip() for part in entry.split(" | ", 1))

    body = " ".join(ITEM_FLAVOR_START_PATTERN.sub("", body).split())
    item_name = _extract_item_name(body)
    body = _strip_trailing_item_name(body, item_name)

    parts: list[str] = [slot] if slot else []
    parts.append(item_name)
    if metadata := _extract_item_metadata(body):
        parts.append(metadata)
    parts.extend(_extract_item_segments(body))
    return " | ".join(part for part in parts if part)


def _extract_item_name(body: str) -> str:
    if item_name_match := ITEM_NAME_PATTERN.search(body):
        item_name = item_name_match.group("name").strip()
    else:
        item_name = body.split(" | ", 1)[0].strip()
    return _clean_item_name(item_name)


def _strip_trailing_item_name(body: str, item_name: str) -> str:
    if body.endswith(item_name):
        return body.removesuffix(item_name).rstrip(" .")
    return body


def _extract_item_metadata(body: str) -> str:
    tier = ITEM_TIER_PATTERN.search(body)
    tags = ITEM_TAG_PATTERN.findall(body)
    return " ".join(part for part in [tier.group(0) if tier else "", *tags] if part)


def _extract_item_segments(body: str) -> list[str]:
    mechanics = body
    if encumbrance_match := ITEM_ENCUMBRANCE_PATTERN.search(mechanics):
        mechanics = mechanics[encumbrance_match.end():].strip()
    mechanics = re.sub(r"^(?:\[[^\]]+\]\s*)*(?:Type:[^;]+;\s*)?(?:tier\s+\d+\b)?\s*", "", mechanics).strip()
    mechanics = ITEM_SECTION_MARKER_PATTERN.sub(" ", mechanics)
    mechanics = mechanics.replace("It must be held with both hands.", " ").strip()

    description_summary = _extract_item_description_summary(mechanics)
    activation_summary = _extract_item_activation_summary(mechanics)
    mechanics = ACTIVATION_PATTERN.sub("", mechanics)
    mechanics = ACTIVATION_COOLDOWN_PATTERN.sub("", mechanics)
    mechanics = ITEM_DESCRIPTION_PATTERN.sub("", mechanics)
    mechanics = " ".join(mechanics.split())

    segments: list[str] = []
    for label, value in _extract_item_labeled_segments(mechanics):
        if label in {
            "Type",
            "Description",
            "Use mode",
            "Is",
            "Effective talent level",
            "Power cost",
            "Range",
            "Cooldown",
            "Travel Speed",
            "Usage Speed",
        }:
            continue
        if label == "Talent granted" or label == "Talents granted":
            label = "Grants"
        if cleaned_value := _clean_item_segment_value(value):
            segments.append(f"{label}: {cleaned_value}")

    if activation_summary and "inscribe your skin" not in activation_summary.lower():
        segments.append(f"Activates: {activation_summary}")
    elif description_summary:
        segments.append(description_summary)
    return _dedupe_preserving_order(segments)


def _extract_item_labeled_segments(text: str) -> list[tuple[str, str]]:
    matches = list(ITEM_LABELED_SEGMENT_PATTERN.finditer(text))
    segments: list[tuple[str, str]] = []
    for index, match in enumerate(matches):
        label = match.group(1).strip()
        value_start = match.end()
        value_end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        value = text[value_start:value_end].strip(" .|")
        if value.startswith("* "):
            value = value[2:]
        if value:
            segments.append((label, value))
    return segments


def _extract_talent_name(raw_desc: str) -> str:
    if "Description:" in raw_desc:
        description = raw_desc.split("Description:", 1)[1].strip()
        tail_fragment = re.split(r"(?<=[.!?])\s+", description)[-1].strip()
        if tail_fragment and (name_search := TALENT_NAME_PATTERN.fullmatch(tail_fragment)):
            return name_search.group(1).strip()
        if tail_fragment and (fallback_search := re.search(r"([A-Z][A-Za-z']+(?:\s+[A-Z][A-Za-z']+)?)$", tail_fragment)):
            return fallback_search.group(1).strip()
    if name_search := TALENT_NAME_PATTERN.search(raw_desc):
        return name_search.group(1).strip()
    return "Unknown Talent"


def _extract_talent_description(raw_desc: str, talent_name: str) -> str:
    if "Description:" not in raw_desc:
        return ""
    description = raw_desc.split("Description:", 1)[1].strip()
    if talent_name != "Unknown Talent" and description.endswith(talent_name):
        description = description.removesuffix(talent_name).rstrip(" .")
    return " ".join(description.split())


def _extract_talent_stat_per_turn(description: str) -> str | list[str] | None:
    matches = _dedupe_preserving_order(TALENT_STAT_PER_TURN_PATTERN.findall(description))
    if not matches:
        return None
    return matches[0] if len(matches) == 1 else matches


def _extract_talent_stat_bonuses(description: str) -> str | list[str] | None:
    matches = []
    for stat_name, value in TALENT_STAT_BONUS_PATTERN.findall(description):
        normalized_name = " ".join(stat_name.split()).removeprefix("your ").strip(" ,.")
        if not normalized_name or normalized_name.lower().endswith("damage"):
            continue
        matches.append(f"{normalized_name}: {value}")
    unique_matches = _dedupe_preserving_order(matches)
    if not unique_matches:
        return None
    return unique_matches[0] if len(unique_matches) == 1 else unique_matches


def _extract_talent_scaling(description: str) -> str | list[str] | None:
    matches = _dedupe_preserving_order(match.strip() for match in TALENT_SCALE_PATTERN.findall(description))
    if not matches:
        return None
    return matches[0] if len(matches) == 1 else matches


def _extract_talent_turn_duration(description: str) -> str | list[str] | None:
    matches = _dedupe_preserving_order(match.strip() for match in TALENT_DURATION_PATTERN.findall(description))
    if not matches:
        return None
    return matches[0]


def _clean_item_name(item_name: str) -> str:
    item_name = ITEM_NAME_TRAILING_NOISE_PATTERN.sub("", item_name)
    item_name = ITEM_REQUIREMENTS_PATTERN.sub("", item_name)
    return " ".join(item_name.split())


def _extract_item_activation_summary(mechanics: str) -> str:
    if not (activation_match := ACTIVATION_PATTERN.search(mechanics)):
        return ""
    activation = " ".join(activation_match.group(1).split())
    if talent_match := re.search(r"activate talent ([^.(:]+)", activation, flags=re.IGNORECASE):
        return talent_match.group(1).strip()
    activation = re.sub(r"\s*\(costing.*?\)", "", activation)
    activation = re.sub(r"\s*:\s*.*$", "", activation)
    return activation.rstrip(".")


def _extract_item_description_summary(mechanics: str) -> str:
    if not (description_match := ITEM_DESCRIPTION_PATTERN.search(mechanics)):
        return ""
    description = ITEM_FLAVOR_START_PATTERN.sub("", description_match.group(1)).strip()
    if not description:
        return ""
    sentence_match = re.split(r"(?<=[.!?])\s+", description, maxsplit=1)
    return sentence_match[0].strip()


def _clean_item_segment_value(value: str) -> str:
    value = " ".join(value.split())
    value = ITEM_FLAVOR_START_PATTERN.sub("", value).strip()
    value = value.removeprefix("When wielded/worn: ").removeprefix("When inscribed on your body: ").strip()
    value = re.sub(r"\s+Learn an unarmed attack talent.*$", "", value)
    value = re.sub(r"\s+Grants spell-crit equal to half of your Shadow Power\.?.*$", "", value)
    value = re.sub(r"\s+No rogue blades shall incapacitate.*$", "", value)
    value = re.sub(r"\s+Transfers a bleed, poison, or wound.*$", "", value)
    return value.strip(" .")


def _dedupe_preserving_order(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        result.append(value)
    return result
