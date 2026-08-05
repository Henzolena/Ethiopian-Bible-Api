import json
import re

import scrapy
from bs4 import BeautifulSoup

from scripts.mezmur_crawler.items import MezmurSourceItem
from scripts.mezmur_crawler.utils import (
    CHECKPOINT_DIR,
    RAW_DIR,
    clean_ethiopic_text,
    clean_inline,
    clean_lyrics,
    infer_mezmur_language,
    is_meta_artist,
    parse_sitemap_locations,
    path_from_url,
    sections_from_lyrics,
    slug_to_name,
)


class WikiMezmurSpider(scrapy.Spider):
    name = "wikimezmur"
    allowed_domains = ["wikimezmur.org", "www.wikimezmur.org"]
    start_urls = ["https://wikimezmur.org/sitemap.xml"]
    handle_httpstatus_list = [404]

    custom_settings = {
        "FEEDS": {
            str(RAW_DIR / "wikimezmur.jsonl"): {
                "format": "jsonlines",
                "encoding": "utf-8",
                "overwrite": False,
            }
        },
        "JOBDIR": str(CHECKPOINT_DIR / "wikimezmur"),
    }

    def parse(self, response):
        for loc in parse_sitemap_locations(response.text):
            normalized = loc.replace("http://www.wikimezmur.org", "https://wikimezmur.org")
            normalized = normalized.replace("http://wikimezmur.org", "https://wikimezmur.org")
            path = path_from_url(normalized)
            if not path.startswith("/am/"):
                continue
            if "?" in normalized or "/Special:" in normalized or "/index.php" in normalized:
                continue
            yield response.follow(normalized, callback=self.parse_page)

    def parse_page(self, response):
        if response.status != 200:
            return

        path = path_from_url(response.url)
        parts = [part for part in path.split("/") if part]

        if len(parts) in {2, 3}:
            yield from self._follow_discography_links(response, parts)

        categories = self._extract_categories(response.text)
        if "Lyrics" not in categories:
            return

        if len(parts) < 4:
            return

        heading_title, heading_artist = self._extract_heading_parts(response)
        artist = slug_to_name(parts[1])
        artist_am = (
            clean_ethiopic_text(response.meta.get("artist_label"))
            or clean_ethiopic_text(heading_artist)
            or clean_ethiopic_text(artist)
        )
        album = (
            clean_inline(response.meta.get("listed_album_title")) or slug_to_name(parts[2])
            if len(parts) >= 4 else ""
        )
        title = clean_inline(response.meta.get("listed_title")) or heading_title or slug_to_name(parts[-1])
        lyrics = self._extract_lyrics(response.text)

        if is_meta_artist(artist) or not title or not lyrics:
            return

        yield MezmurSourceItem(
            source="wiki",
            source_id=path,
            source_url=response.url,
            artist=artist,
            artist_am=artist_am,
            artist_slug=parts[1],
            title=title,
            title_am=clean_ethiopic_text(title),
            title_alt="",
            album=album,
            album_am=clean_ethiopic_text(album),
            album_slug=parts[2] if len(parts) >= 4 else "",
            language=infer_mezmur_language(title=title, lyrics=lyrics, categories=categories),
            lyrics=lyrics,
            sections=sections_from_lyrics(lyrics),
            metadata={"categories": categories},
        )

    def _follow_discography_links(self, response, current_parts: list[str]):
        if len(current_parts) < 2 or current_parts[0] != "am":
            return

        current_artist_slug = current_parts[1]
        current_artist_label = clean_inline(response.meta.get("artist_label")) or self._extract_heading(response)
        current_album_label = ""
        if len(current_parts) == 3:
            current_album_label = (
                clean_inline(response.meta.get("listed_album_title"))
                or clean_inline(response.meta.get("listed_title"))
                or self._extract_heading(response)
                or slug_to_name(current_parts[2])
            )
        album_labels_by_slug: dict[str, str] = {}
        seen: set[str] = set()

        for link in response.css("#mw-content-text a[href]"):
            href = link.attrib.get("href", "")
            linked_path = path_from_url(response.urljoin(href))
            parts = [part for part in linked_path.split("/") if part]
            if len(parts) < 3 or parts[0] != "am":
                continue
            if parts[1] != current_artist_slug:
                continue
            if any(":" in part for part in parts[1:]):
                continue
            if linked_path in seen:
                continue

            seen.add(linked_path)
            label = clean_inline(" ".join(link.css("::text").getall()))
            meta = {
                "artist_label": current_artist_label,
                "listed_title": label,
            }
            if len(parts) == 3:
                album_label = label or current_album_label or slug_to_name(parts[2])
                album_labels_by_slug[parts[2]] = album_label
                meta["listed_album_title"] = album_label
            elif len(parts) >= 4:
                album_label = album_labels_by_slug.get(parts[2]) or current_album_label or slug_to_name(parts[2])
                meta["listed_album_title"] = album_label

            yield response.follow(
                linked_path,
                callback=self.parse_page,
                meta=meta,
            )

    def _extract_categories(self, html: str) -> list[str]:
        match = re.search(r'"wgCategories":\s*(\[[^\]]*\])', html)
        if not match:
            return []
        try:
            loaded = json.loads(match.group(1))
        except json.JSONDecodeError:
            return []
        return [clean_inline(item) for item in loaded if clean_inline(item)]

    def _extract_heading(self, response) -> str:
        text = response.css("#firstHeading").xpath("string(.)").get()
        return clean_inline(text)

    def _extract_heading_parts(self, response) -> tuple[str, str]:
        heading = self._extract_heading(response)
        if " - " in heading:
            title, artist = heading.split(" - ", 1)
            return clean_inline(title), clean_inline(artist)
        return heading, ""

    def _extract_lyrics(self, html: str) -> str:
        soup = BeautifulSoup(html, "lxml")
        root = soup.select_one("#mw-content-text .mw-parser-output") or soup.select_one("#mw-content-text")
        if root is None:
            return ""

        for selector in ["div.noprint", ".mw-editsection", "script", "style", "sup.reference"]:
            for element in root.select(selector):
                element.decompose()

        poem = root.select_one(".poem")
        if poem is not None:
            for br in poem.find_all("br"):
                br.replace_with("<<<BR>>>")
            text = poem.get_text("")
            text = re.sub(r"\s*<<<BR>>>\s*", "\n", text)
            text = re.sub(r"\n{3,}", "\n\n", text)
            return clean_lyrics(text)

        table = root.find("table")
        if table is not None:
            return clean_lyrics(table.get_text("\n", strip=True).replace("፦\n", "፦ "))

        text = root.get_text("\n", strip=True)
        text = text.replace("Print Lyrics", "")
        return clean_lyrics(text)
