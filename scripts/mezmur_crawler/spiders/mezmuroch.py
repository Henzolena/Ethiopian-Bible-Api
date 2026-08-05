import scrapy

from scripts.mezmur_crawler.items import MezmurSourceItem
from scripts.mezmur_crawler.utils import (
    CHECKPOINT_DIR,
    RAW_DIR,
    clean_inline,
    clean_lyrics,
    extract_json_ld_objects,
    extract_mezmuroch_stanzas,
    find_json_ld_type,
    path_from_url,
    parse_sitemap_locations,
    sections_from_stanzas,
    slug_to_name,
)


class MezmurochSpider(scrapy.Spider):
    name = "mezmuroch"
    allowed_domains = ["mezmuroch.com", "www.mezmuroch.com"]
    start_urls = ["https://www.mezmuroch.com/sitemap.xml"]

    custom_settings = {
        "FEEDS": {
            str(RAW_DIR / "mezmuroch.jsonl"): {
                "format": "jsonlines",
                "encoding": "utf-8",
                "overwrite": False,
            }
        },
        "JOBDIR": str(CHECKPOINT_DIR / "mezmuroch"),
    }

    def parse(self, response):
        for loc in parse_sitemap_locations(response.text):
            path = path_from_url(loc)
            if path.startswith("/lyrics/"):
                yield response.follow(loc, callback=self.parse_song)

    def parse_song(self, response):
        path = path_from_url(response.url)
        parts = [part for part in path.split("/") if part]
        if len(parts) < 3:
            return

        json_ld = extract_json_ld_objects(response.text)
        music = find_json_ld_type(json_ld, "MusicComposition") or {}
        lyrics_obj = music.get("lyrics") if isinstance(music.get("lyrics"), dict) else {}
        composer = music.get("composer") if isinstance(music.get("composer"), dict) else {}
        album = music.get("includedIn") if isinstance(music.get("includedIn"), dict) else {}

        lyrics = clean_lyrics(lyrics_obj.get("text") or "")
        stanzas = self._dom_stanzas(response)
        if not stanzas and lyrics:
            stanzas = lyrics.split("\n\n")
        if not stanzas:
            stanzas = extract_mezmuroch_stanzas(response.text)

        title = clean_inline(music.get("name")) or slug_to_name(parts[-1])
        artist = clean_inline(composer.get("name")) or slug_to_name(parts[-2])
        sections = sections_from_stanzas(stanzas)

        if not title or not artist or not (lyrics or sections):
            return

        yield MezmurSourceItem(
            source="mezmuroch",
            source_id=path,
            source_url=response.url,
            artist=artist,
            artist_slug=parts[-2],
            title=title,
            title_alt=clean_inline(music.get("alternateName")),
            album=clean_inline(album.get("name")),
            album_slug="",
            language=clean_inline(music.get("inLanguage")) or "am",
            lyrics=lyrics or "\n\n".join(section["lyrics_am"] for section in sections),
            sections=sections,
            metadata={"json_ld": bool(music)},
        )

    def _dom_stanzas(self, response) -> list[str]:
        stanzas = []
        for group in response.css('[data-lyrics-container="true"] > div'):
            lines = [
                clean_inline(line)
                for line in group.css("p[data-gidx]::text").getall()
                if clean_inline(line)
            ]
            if lines:
                stanzas.append(clean_lyrics("\n".join(lines)))
        return stanzas
