import re
import sys


def get_beautiful_soup():
    try:
        from bs4 import BeautifulSoup
    except ModuleNotFoundError as exc:
        raise RuntimeError(
            "beautifulsoup4 is required for TE4 scraping. Install it with `py -3 -m pip install beautifulsoup4`."
        ) from exc
    return BeautifulSoup


def parse_desc_lua(desc_path):
    """Extracts character metadata and checks if they are alive (loadable)."""
    details = {'loadable': True}
    try:
        with open(desc_path, 'r', encoding='utf-8') as file_handle:
            content = file_handle.read()
    except Exception as exc:
        print(f"[!] Could not parse {desc_path}: {exc}", file=sys.stderr)
        return details

    short_match = re.search(r'(?m)^\s*short_name\b\s*=\s*["\']([^"\']+)["\']', content)
    name_match = re.search(r'(?m)^\s*name\b\s*=\s*["\']([^"\']+)["\']', content)
    loadable_match = re.search(r'(?m)^\s*loadable\b\s*=\s*(true|false)\b', content)

    if short_match:
        details['short_name'] = short_match.group(1)
    if name_match:
        details['name'] = name_match.group(1)
    if loadable_match:
        details['loadable'] = (loadable_match.group(1) == 'true')
    return details


def extract_optimized_data(html_content):
    """Advanced Scraper: Extracts tactical data and specific death logs for AI analysis."""
    BeautifulSoup = get_beautiful_soup()
    soup = BeautifulSoup(html_content, 'html.parser')
    agent_data = {}

    title_tag = soup.find('h2')
    if title_tag:
        agent_data['Character_Name'] = " ".join(title_tag.get_text(strip=True).split())

    for section in soup.find_all(['h2', 'h3', 'h4']):
        section_title = section.get_text(strip=True)
        if section_title == agent_data.get('Character_Name'):
            continue

        node = section.find_next_sibling()
        content_list = []
        while node and node.name not in ['h2', 'h3', 'h4']:
            if node.name == 'table':
                for tr in node.find_all('tr'):
                    cells = tr.find_all(['th', 'td'])
                    if cells:
                        row = " | ".join([" ".join(cell.get_text(separator=" ", strip=True).split()) for cell in cells])
                        row = re.sub(
                            r'(This item will automatically be transmogrified|Crafted by a master|Infused by psionic forces|Powered by arcane forces)\s*',
                            '',
                            row
                        )
                        content_list.append(row)
            elif node.name == 'ul':
                for li in node.find_all('li'):
                    content_list.append(" ".join(li.get_text(separator=" ", strip=True).split()))
            node = node.find_next_sibling()

        if not content_list:
            continue

        if section_title == "Character":
            filtered_data = {}
            for row in content_list:
                if " | " not in row:
                    continue
                key, value = [part.strip() for part in row.split(" | ", 1)]
                if key == "Lifes / Deaths":
                    death_matches = re.findall(r'Killed by ([^/]+)', value)
                    target_death = [death.strip() for death in death_matches if "Spellblaze Crystal at level 3" in death]
                    summary_count = value.split('/')[-1].strip() if '/' in value else ""
                    filtered_data[key] = {"Target_Death": target_death, "Total_Death_Count": summary_count}
                elif key not in ["Addons", "Features:"]:
                    filtered_data[key] = value
            agent_data["Core_Info"] = filtered_data
        elif "Talents" in section_title:
            category_data = {}
            current_category = "General"
            for row in content_list:
                if " | " not in row:
                    current_category = row.strip()
                    category_data[current_category] = {}
                    continue

                raw_desc, level = [part.strip() for part in row.split(" | ", 1)]
                name_search = re.search(r'([A-Z][a-zA-Z\s\']+)$', raw_desc.split("Description:")[0])
                talent_name = name_search.group(1).strip() if name_search else "Unknown Talent"

                details = {"Level": level}
                for key, pattern in {
                    "Range": r'Range:\s*([\w/]+)',
                    "Cooldown": r'Cooldown:\s*(\d+)',
                    "Travel_Speed": r'Travel Speed:\s*(\w+)',
                    "Usage_Speed": r'Usage Speed:\s*([^(]+\([^)]+\))',
                    "Per_Turn": r'\+([0-9.]+\s+[a-zA-Z]+/turn)',
                    "Stats": r'\+([0-9.]+\s+[a-zA-Z]+(?!\/turn))',
                    "Scales_With": r'(?:scales|increases|improves) with (?:your )?([\w]+)',
                    "Duration": r'(\d+)\s+turns',
                }.items():
                    matches = re.findall(pattern, raw_desc)
                    if matches:
                        details[key] = matches if len(matches) > 1 else matches[0]
                category_data[current_category][talent_name] = details
            agent_data[section_title] = category_data
        else:
            agent_data[section_title] = content_list

    return agent_data


def vault_name_matches(remote_name, local_name):
    """Matches a vault display name against a local character name with a word boundary."""
    normalized_remote = " ".join(remote_name.split())
    normalized_local = " ".join(local_name.split())
    return bool(re.match(rf'^{re.escape(normalized_local)}(?:\b|$)', normalized_remote, flags=re.IGNORECASE))
