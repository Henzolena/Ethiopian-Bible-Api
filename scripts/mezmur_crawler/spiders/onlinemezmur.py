from urllib.parse import parse_qs, urlparse

import scrapy
from bs4 import BeautifulSoup

from scripts.mezmur_crawler.items import MezmurSourceItem
from scripts.mezmur_crawler.utils import (
    CHECKPOINT_DIR,
    RAW_DIR,
    clean_inline,
    clean_lyrics,
    sections_from_stanzas,
    split_online_title_artist,
)


class OnlineMezmurSpider(scrapy.Spider):
    name = "onlinemezmur"
    allowed_domains = ["onlinemezmur.com", "www.onlinemezmur.com"]
    start_urls = ["https://onlinemezmur.com/search_song_names.php?search=&page=0"]

    custom_settings = {
        "CONCURRENT_REQUESTS_PER_DOMAIN": 1,
        "DOWNLOAD_DELAY": 20.0,
        "RANDOMIZE_DOWNLOAD_DELAY": False,
        "AUTOTHROTTLE_ENABLED": False,
        "FEEDS": {
            str(RAW_DIR / "onlinemezmur.jsonl"): {
                "format": "jsonlines",
                "encoding": "utf-8",
                "overwrite": False,
            }
        },
        "JOBDIR": str(CHECKPOINT_DIR / "onlinemezmur"),
    }

    def parse(self, response):
        for link in response.css("a[href*='song_lyrics.php'][href*='song_id=']"):
            href = link.attrib.get("href", "")
            label = clean_inline(" ".join(link.css("::text").getall()))
            title, artist = split_online_title_artist(label)
            song_id = self._song_id(href)
            if not title or not song_id:
                continue
            yield response.follow(
                href,
                callback=self.parse_song,
                meta={
                    "artist": artist,
                    "title": title,
                    "song_id": song_id,
                    "raw_label": label,
                },
            )

        for href in response.css("a[href*='search_song_names.php'][href*='page=']::attr(href)").getall():
            yield response.follow(href, callback=self.parse)

    def parse_artist_songs(self, response):
        fallback_artist = response.meta.get("artist", "")

        for link in response.css("a[href*='song_lyrics.php'][href*='song_id=']"):
            href = link.attrib.get("href", "")
            label = clean_inline(" ".join(link.css("::text").getall()))
            title, artist = split_online_title_artist(label, fallback_artist=fallback_artist)
            song_id = self._song_id(href)
            if not title or not song_id:
                continue
            yield response.follow(
                href,
                callback=self.parse_song,
                meta={
                    "artist": artist,
                    "title": title,
                    "song_id": song_id,
                    "raw_label": label,
                },
            )

        for href in response.css("a[href*='singer_songs.php'][href*='page=']::attr(href)").getall():
            yield response.follow(href, callback=self.parse_artist_songs, meta={"artist": fallback_artist})

    def parse_song(self, response):
        soup = BeautifulSoup(response.text, "lxml")
        stanzas = [
            clean_lyrics(element.get_text("\n", strip=True))
            for element in soup.select(".versedisplay2")
        ]
        stanzas = [stanza for stanza in stanzas if stanza]
        if not stanzas:
            return

        song_id = response.meta.get("song_id") or self._song_id(response.url)
        title = clean_inline(response.meta.get("title"))
        artist = clean_inline(response.meta.get("artist")) or "Unknown Artist"
        if not song_id or not title or not artist:
            return

        yield MezmurSourceItem(
            source="online",
            source_id=str(song_id),
            source_url=response.url,
            artist=artist,
            artist_slug="",
            title=title,
            title_alt="",
            album="",
            album_slug="",
            language="am",
            lyrics="\n\n".join(stanzas),
            sections=sections_from_stanzas(stanzas),
            metadata={
                "raw_label": response.meta.get("raw_label", ""),
                "artist_missing": artist == "Unknown Artist",
            },
        )

    def _song_id(self, url: str) -> str:
        parsed = urlparse(url)
        values = parse_qs(parsed.query).get("song_id")
        return clean_inline(values[0]) if values else ""
