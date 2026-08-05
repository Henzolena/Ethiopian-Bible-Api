from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable
from urllib.parse import unquote, urlparse
from xml.etree import ElementTree


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = PROJECT_ROOT / "data" / "mezmur"
RAW_DIR = DATA_DIR / "raw"
CHECKPOINT_DIR = DATA_DIR / "checkpoints"

SOURCE_PRIORITY = {
    "mezmuroch": 0,
    "wiki": 1,
    "online": 2,
}

ETHIOPIC_NUMERAL_CHARS = "፩፪፫፬፭፮፯፰፱፲፳፴፵፶፷፸፹፺፻፼"
REPEAT_COUNT = rf"(?:\d+|[{ETHIOPIC_NUMERAL_CHARS}]+)"
WORD_CHARS = r"A-Za-z0-9_\u1200-\u137f"
ETHIOPIC_TEXT_RANGES = (
    ("\u1200", "\u135a"),
    ("\u1380", "\u1399"),
    ("\u2d80", "\u2ddf"),
    ("\uab00", "\uab2f"),
)

SKIP_ARTIST_NAMES = {
    "about",
    "bible",
    "browse",
    "choirs",
    "collections",
    "contact",
    "copyright",
    "help",
    "home",
    "main page",
    "privacy",
    "search",
    "sitemap",
    "submit",
    "terms",
}


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def clean_inline(value: object) -> str:
    text = "" if value is None else str(value)
    text = text.replace("\u00a0", " ").replace("\r", " ").replace("\t", " ")
    return " ".join(text.split())


def is_ethiopic_char(char: str) -> bool:
    return any(start <= char <= end for start, end in ETHIOPIC_TEXT_RANGES)


def count_ethiopic(value: object) -> int:
    return sum(1 for char in str(value or "") if is_ethiopic_char(char))


def count_latin_letters(value: object) -> int:
    return sum(1 for char in str(value or "") if ("A" <= char <= "Z") or ("a" <= char <= "z"))


def clean_ethiopic_text(value: object) -> str:
    """Return a display-safe Ethiopic label without Latin transliteration/noise."""
    text = clean_inline(value)
    if not text:
        return ""

    text = re.sub(r"\([^)]*\)", " ", text)
    text = text.replace("፡", " ").replace("፦", " ")

    kept: list[str] = []
    for char in text:
        if is_ethiopic_char(char) or char in ETHIOPIC_NUMERAL_CHARS:
            kept.append(char)
        elif char.isspace():
            kept.append(" ")
        else:
            kept.append(" ")

    cleaned = re.sub(r"\s+", " ", "".join(kept)).strip()
    if count_ethiopic(cleaned) < 2:
        return ""
    return cleaned


def infer_mezmur_language(
    *,
    title: object = "",
    lyrics: object = "",
    categories: Iterable[object] = (),
) -> str:
    """Classify catalogue songs for UI separation: Ethiopic-script songs vs English/Latin."""
    text = f"{clean_inline(title)}\n{clean_inline(lyrics)}"
    ethiopic_count = count_ethiopic(text)
    latin_count = count_latin_letters(text)

    if latin_count >= 100 and latin_count >= ethiopic_count * 2:
        return "en"
    if ethiopic_count >= 2:
        return "am"

    normalized_categories = {normalize_key(category) for category in categories}
    if normalized_categories & {"english", "እንግሊዝኛ"}:
        return "en"
    return "en" if latin_count else "am"


def clean_lyrics(value: object) -> str:
    text = "" if value is None else str(value)
    text = text.replace("\u00a0", " ").replace("\r\n", "\n").replace("\r", "\n")
    lines = [_clean_lyric_line(line) for line in text.split("\n")]

    paragraphs: list[str] = []
    current: list[str] = []
    for line in lines:
        if line:
            current.append(line)
        elif current:
            paragraphs.append("\n".join(current))
            current = []
    if current:
        paragraphs.append("\n".join(current))

    return "\n\n".join(paragraphs)


def _clean_lyric_line(line: object) -> str:
    text = clean_inline(line)
    if not text:
        return ""

    text = re.sub(r"[\[(]\s*(?:አዝማች|አዝ|chorus|refrain)\s*[])]", " ", text, flags=re.IGNORECASE)
    text = re.sub(
        r"^\s*(?:አዝማች|አዝ|chorus|refrain|verse|intro|bridge|outro)\s*[:፦፡.\-–—]*\s*",
        "",
        text,
        flags=re.IGNORECASE,
    )
    text = re.sub(rf"^\s*{REPEAT_COUNT}\s*[-.)፡።:፦]\s*", "", text)
    text = text.replace("፡", " ").replace("፦", " ")

    text = re.sub(rf"\(\s*(?:[xX×]\s*)?{REPEAT_COUNT}\s*(?:[xX×]|ጊዜ|ጊዜያት)?\s*\)", " ", text)
    text = re.sub(rf"(?<![{WORD_CHARS}])[xX×]\s*{REPEAT_COUNT}(?![{WORD_CHARS}])", " ", text)
    text = re.sub(rf"(?<![{WORD_CHARS}])(?:[xX×]\s*)?{REPEAT_COUNT}\s*(?:[xX×]|ጊዜ|ጊዜያት)(?![{WORD_CHARS}])", " ", text)
    text = re.sub(rf"/\s*{REPEAT_COUNT}(?![{WORD_CHARS}])", " ", text)
    text = re.sub(rf"(?<=[\u1200-\u137f])\s*(?:[xX×]\s*)?{REPEAT_COUNT}(?=\s*$)", " ", text)

    text = re.sub(r"[:፤;]+", " ", text)
    text = re.sub(r"[()\[\]{}]", " ", text)
    text = re.sub(r"[\"“”'`*_~•|/\\]+", " ", text)
    text = re.sub(r"\s*[!?።፣፥፧፨,]+\s*", " ", text)
    text = re.sub(r"\s*[-–—]+\s*", " ", text)
    text = re.sub(r"\s+", " ", text)
    if normalize_key(text) in {"አዝ", "አዝማች", "chorus", "refrain"}:
        return ""

    return text.strip()


def normalize_key(value: object) -> str:
    text = clean_inline(value).lower()
    text = unquote(text)
    text = text.replace("_", " ").replace("-", " ").replace("፡", " ")
    text = re.sub(r"\([^)]*\)", " ", text)
    text = re.sub(r"[^\w\u1200-\u137f]+", " ", text, flags=re.UNICODE)
    return " ".join(text.split())


def slug_to_name(value: str) -> str:
    text = unquote(value.rsplit("/", 1)[-1])
    text = text.replace("_", " ").replace("-", " ")
    return clean_inline(text)


def is_meta_artist(name: str) -> bool:
    key = normalize_key(name)
    if not key or key in SKIP_ARTIST_NAMES:
        return True
    if ":" in name:
        return True
    words = key.split()
    if len(words) > 7 and all(ord(char) < 128 for char in key):
        return True
    return False


def parse_sitemap_locations(xml_text: str) -> list[str]:
    root = ElementTree.fromstring(xml_text.encode("utf-8"))
    namespace = {"sm": "http://www.sitemaps.org/schemas/sitemap/0.9"}
    locations = [el.text for el in root.findall(".//sm:loc", namespace)]
    if not locations:
        locations = [el.text for el in root.findall(".//loc")]
    return [loc.strip() for loc in locations if loc and loc.strip()]


def path_from_url(url: str) -> str:
    return urlparse(url).path or "/"


def split_online_title_artist(label: str, fallback_artist: str = "") -> tuple[str, str]:
    text = clean_inline(label)
    if "---" in text:
        title, artist = text.split("---", 1)
        return clean_inline(title), clean_inline(artist)
    return text, clean_inline(fallback_artist)


def source_record_key(record: dict) -> tuple[str, str]:
    return str(record.get("source") or ""), str(record.get("source_id") or "")


def lyrics_hash(lyrics: str) -> str:
    text = normalize_key(lyrics)
    return hashlib.sha256(text.encode("utf-8")).hexdigest() if text else ""


def sections_from_stanzas(stanzas: Iterable[object]) -> list[dict]:
    cleaned = [clean_lyrics(stanza) for stanza in stanzas]
    cleaned = [stanza for stanza in cleaned if stanza]
    sections: list[dict] = []
    verse_number = 1

    for index, stanza in enumerate(cleaned):
        section_type = "chorus" if looks_like_chorus(stanza, cleaned, index) else "verse"
        if section_type == "chorus":
            label = "አዝ"
            key = "c"
        else:
            label = f"Verse {verse_number}"
            key = f"v{verse_number}"
            verse_number += 1

        sections.append(
            {
                "type": section_type,
                "label": label,
                "key": key,
                "sort_order": index,
                "lyrics_am": stanza,
                "lyrics_en": "",
            }
        )

    return sections


def sections_from_lyrics(lyrics: str) -> list[dict]:
    return sections_from_stanzas(clean_lyrics(lyrics).split("\n\n"))


def lyrics_from_sections(sections: Iterable[dict]) -> str:
    paragraphs = []
    for section in sections:
        text = clean_lyrics(section.get("lyrics_am") or section.get("text") or "")
        if text:
            paragraphs.append(text)
    return "\n\n".join(paragraphs)


def looks_like_chorus(stanza: str, all_stanzas: list[str], index: int) -> bool:
    first_line = clean_inline(stanza.split("\n", 1)[0])
    if first_line.startswith(("አዝ", "Chorus", "chorus")):
        return True

    normalized = normalize_key(stanza)
    if not normalized:
        return False
    matches = sum(1 for other in all_stanzas if normalize_key(other) == normalized)
    return matches > 1 and index > 0


def extract_json_ld_objects(html: str) -> list[dict]:
    objects: list[dict] = []
    for match in re.finditer(
        r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
        html,
        flags=re.IGNORECASE | re.DOTALL,
    ):
        raw = match.group(1).strip()
        if not raw:
            continue
        try:
            loaded = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if isinstance(loaded, list):
            objects.extend(obj for obj in loaded if isinstance(obj, dict))
        elif isinstance(loaded, dict):
            graph = loaded.get("@graph")
            if isinstance(graph, list):
                objects.extend(obj for obj in graph if isinstance(obj, dict))
            else:
                objects.append(loaded)
    return objects


def find_json_ld_type(objects: Iterable[dict], type_name: str) -> dict | None:
    for obj in objects:
        obj_type = obj.get("@type")
        if obj_type == type_name or (isinstance(obj_type, list) and type_name in obj_type):
            return obj
    return None


def extract_mezmuroch_stanzas(html: str) -> list[str]:
    marker_index = html.find("stanzas")
    if marker_index < 0:
        return []

    start = html.find("[", marker_index)
    end = html.find("]", start)
    if start < 0 or end < 0:
        return []

    fragment = html[start : end + 1]
    candidates = [
        fragment,
        fragment.replace('\\"', '"'),
        fragment.replace("\\\\n", "\\n").replace('\\"', '"'),
    ]

    for candidate in candidates:
        try:
            values = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(values, list):
            return [clean_lyrics(value) for value in values if clean_lyrics(value)]
    return []


def make_arrangement(sections: list[dict]) -> str:
    keys = [clean_inline(section.get("key") or "") for section in sections]
    return ",".join(key for key in keys if key)
