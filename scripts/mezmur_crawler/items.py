import scrapy


class MezmurSourceItem(scrapy.Item):
    source = scrapy.Field()
    source_id = scrapy.Field()
    source_url = scrapy.Field()
    artist = scrapy.Field()
    artist_am = scrapy.Field()
    artist_slug = scrapy.Field()
    title = scrapy.Field()
    title_am = scrapy.Field()
    title_alt = scrapy.Field()
    album = scrapy.Field()
    album_am = scrapy.Field()
    album_slug = scrapy.Field()
    language = scrapy.Field()
    lyrics = scrapy.Field()
    sections = scrapy.Field()
    metadata = scrapy.Field()
