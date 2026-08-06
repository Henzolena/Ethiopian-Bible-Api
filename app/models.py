from sqlalchemy import Column, Integer, String, Text, Boolean, UniqueConstraint, ForeignKey, Index, DateTime, LargeBinary
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func
from app.database import Base


class Language(Base):
    __tablename__ = "languages"

    id = Column(Integer, primary_key=True)
    code = Column(String(10), unique=True, nullable=False)   # am, or, ti, en
    name = Column(String(50), nullable=False)                # Amharic, Oromo, ...
    native_name = Column(String(100), nullable=False)        # አማርኛ, Afaan Oromoo, ...
    direction = Column(String(3), default="ltr")             # ltr / rtl

    book_names = relationship("BookName", back_populates="language")
    verses = relationship("Verse", back_populates="language")


class Book(Base):
    __tablename__ = "books"

    id = Column(Integer, primary_key=True)
    number = Column(Integer, unique=True, nullable=False)    # 1–66
    english_name = Column(String(100), nullable=False)
    abbreviation = Column(String(10), nullable=False)        # GEN, EXO, ...
    testament = Column(String(3), nullable=False)            # OT / NT
    chapter_count = Column(Integer, nullable=False)

    names = relationship("BookName", back_populates="book")
    verses = relationship("Verse", back_populates="book")


class BookName(Base):
    __tablename__ = "book_names"

    id = Column(Integer, primary_key=True)
    book_id = Column(Integer, ForeignKey("books.id"), nullable=False)
    language_id = Column(Integer, ForeignKey("languages.id"), nullable=False)
    name = Column(String(200), nullable=False)

    book = relationship("Book", back_populates="names")
    language = relationship("Language", back_populates="book_names")

    __table_args__ = (UniqueConstraint("book_id", "language_id"),)


class Verse(Base):
    __tablename__ = "verses"

    id = Column(Integer, primary_key=True)
    book_id = Column(Integer, ForeignKey("books.id"), nullable=False)
    language_id = Column(Integer, ForeignKey("languages.id"), nullable=False)
    chapter = Column(Integer, nullable=False)
    verse = Column(Integer, nullable=False)
    text = Column(Text, nullable=False)

    book = relationship("Book", back_populates="verses")
    language = relationship("Language", back_populates="verses")

    __table_args__ = (
        UniqueConstraint("book_id", "language_id", "chapter", "verse"),
        Index("ix_verse_lookup", "book_id", "language_id", "chapter", "verse"),
        Index("ix_verse_lang", "language_id"),
    )


class QuizQuestion(Base):
    """
    A multiple-choice Bible trivia question tied to a specific book/chapter/verse range.

    Sources:
      source='static'       — pre-loaded from curated PDF (e.g. Ted Hildebrandt's Genesis set)
      source='ai_generated' — created on-demand by Gemini AI
    """
    __tablename__ = "quiz_questions"

    id = Column(Integer, primary_key=True, autoincrement=True)
    # group_id links the SAME question across all language translations.
    # All 4 language versions of question N share the same group_id UUID.
    group_id      = Column(String(36), nullable=True, index=True)

    # Location
    book_id       = Column(Integer, ForeignKey("books.id"), nullable=False)
    language_code = Column(String(10), nullable=False)   # niv, en, am, or, ti
    chapter       = Column(Integer, nullable=False)
    verse_start   = Column(Integer, nullable=True)       # None = whole-chapter question
    verse_end     = Column(Integer, nullable=True)       # same as verse_start for single verse

    # Content
    question      = Column(Text, nullable=False)
    option_a      = Column(Text, nullable=False)
    option_b      = Column(Text, nullable=False)
    option_c      = Column(Text, nullable=False)
    option_d      = Column(Text, nullable=False)
    correct_answer = Column(String(1), nullable=False)   # "A" / "B" / "C" / "D"
    explanation   = Column(Text, nullable=True)          # why the answer is correct

    # Metadata
    difficulty    = Column(String(20), nullable=False, default="beginner")
    # beginner / intermediate / advanced
    source        = Column(String(20), nullable=False, default="static")
    # static / ai_generated
    author        = Column(String(200), nullable=True)   # e.g. "Ted Hildebrandt"
    is_verified   = Column(Boolean, nullable=False, default=True)
    created_at    = Column(DateTime(timezone=True), server_default=func.now())

    book = relationship("Book")

    __table_args__ = (
        Index("ix_quiz_book_chapter", "book_id", "chapter"),
        Index("ix_quiz_lang",         "language_code"),
        Index("ix_quiz_difficulty",   "difficulty"),
        Index("ix_quiz_source",       "source"),
    )


# ─────────────────────────────────────────────────────────────────────────────
# MEZMUR MODULE — Ethiopian Christian Songs
# ─────────────────────────────────────────────────────────────────────────────

class MezmurArtist(Base):
    """Singer / artist (ዘማሪ).
    Sources: 'mezmuroch', 'online', 'wiki', 'both', or 'multiple'.
    """
    __tablename__ = "mezmur_artists"

    id               = Column(Integer, primary_key=True, index=True)
    name             = Column(String(300), nullable=False)
    name_am          = Column(String(300), nullable=True)
    name_normalized  = Column(String(300), nullable=False)   # lowercase, spaces, for dedup
    source           = Column(String(10),  nullable=False)   # online | wiki | both
    online_encoded   = Column(String(600))                   # URL-encoded name for singer_songs.php
    wiki_path        = Column(String(600))                   # /am/Artist_Name path segment
    song_count       = Column(Integer, default=0)

    albums = relationship("MezmurAlbum", back_populates="artist", cascade="all, delete-orphan")
    songs  = relationship("MezmurSong",  back_populates="artist", cascade="all, delete-orphan")

    __table_args__ = (
        UniqueConstraint("name_normalized", name="uq_mezmur_artist_name"),
        Index("ix_mezmur_artist_norm", "name_normalized"),
        Index("ix_mezmur_artist_source", "source"),
    )


class MezmurAlbum(Base):
    """Album from wikimezmur.org (3-level hierarchy Artist → Album → Track).
    onlinemezmur.com does not expose albums; those songs appear directly under artist.
    """
    __tablename__ = "mezmur_albums"

    id          = Column(Integer, primary_key=True, index=True)
    artist_id   = Column(Integer, ForeignKey("mezmur_artists.id", ondelete="CASCADE"), nullable=False)
    title       = Column(String(400), nullable=False)
    title_am    = Column(String(400), nullable=True)
    wiki_slug   = Column(String(600))   # e.g. "Memheru_Vol3" (2nd path segment)
    track_count = Column(Integer, default=0)

    artist = relationship("MezmurArtist", back_populates="albums")
    tracks = relationship("MezmurSong",   back_populates="album")

    __table_args__ = (
        UniqueConstraint("artist_id", "wiki_slug", name="uq_mezmur_album"),
        Index("ix_mezmur_album_artist", "artist_id"),
    )


class MezmurSong(Base):
    """Individual song / track (ዜማ).
    Structured lyrics stored as a JSON array in lyrics_json:
      [{"type":"verse","label":"ቁጥር ፩","sort_order":0,
        "lyrics_am":"...","lyrics_en":"..."},
       {"type":"chorus","label":"ኮረስ","sort_order":1,
        "lyrics_am":"...","lyrics_en":"..."}]
    arrangement is a comma-separated key list: "v1,c,v2,c,b,c"
    """
    __tablename__ = "mezmur_songs"

    id          = Column(Integer, primary_key=True, index=True)
    artist_id   = Column(Integer, ForeignKey("mezmur_artists.id", ondelete="CASCADE"), nullable=False)
    album_id    = Column(Integer, ForeignKey("mezmur_albums.id",  ondelete="SET NULL"), nullable=True)
    title       = Column(String(400), nullable=False)
    title_am    = Column(String(400), nullable=True)
    language    = Column(String(10), nullable=False, default="am")  # am | en
    source      = Column(String(10),  nullable=False)   # mezmuroch | online | wiki
    source_id   = Column(String(600), nullable=False)   # song_id or wiki full path
    lyrics_json = Column(Text, nullable=True)           # structured JSON (see above)
    arrangement = Column(String(500), nullable=True)    # "v1,c,v2,c"
    has_lyrics  = Column(Boolean, default=False)        # True once lyrics fetched

    artist = relationship("MezmurArtist", back_populates="songs")
    album  = relationship("MezmurAlbum",  back_populates="tracks")

    __table_args__ = (
        UniqueConstraint("source", "source_id", name="uq_mezmur_song"),
        Index("ix_mezmur_song_artist",  "artist_id"),
        Index("ix_mezmur_song_album",   "album_id"),
        Index("ix_mezmur_song_language", "language"),
        Index("ix_mezmur_song_source",  "source", "source_id"),
        Index("ix_mezmur_song_lyrics",  "has_lyrics"),
    )


# ─────────────────────────────────────────────────────────────────────────────
# FONT MODULE — Ethiopic font catalogue for presentation clients
# ─────────────────────────────────────────────────────────────────────────────

class EthiopicFont(Base):
    """Self-hosted font file plus metadata for Bible/presentation clients.

    The binary is stored in the database so Railway can serve a stable public
    font API without needing a separate object-storage bucket.
    """
    __tablename__ = "ethiopic_fonts"

    id             = Column(Integer, primary_key=True, index=True)
    slug           = Column(String(180), nullable=False, unique=True)
    family_name    = Column(String(240), nullable=False)
    display_name   = Column(String(300), nullable=False)
    style_name     = Column(String(120), nullable=False, default="Regular")
    weight         = Column(Integer, nullable=False, default=400)
    is_italic      = Column(Boolean, nullable=False, default=False)
    format         = Column(String(12), nullable=False)
    mime_type      = Column(String(80), nullable=False)
    file_name      = Column(String(300), nullable=False)
    file_size      = Column(Integer, nullable=False)
    sha256         = Column(String(64), nullable=False, index=True)
    data           = Column(LargeBinary, nullable=False)
    supports_ethiopic = Column(Boolean, nullable=False, default=True)
    license_name   = Column(Text, nullable=True)
    license_url    = Column(Text, nullable=True)
    source         = Column(Text, nullable=False, default="local-font-folder")
    is_active      = Column(Boolean, nullable=False, default=True)
    created_at     = Column(DateTime(timezone=True), server_default=func.now())
    updated_at     = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    __table_args__ = (
        Index("ix_ethiopic_font_family", "family_name"),
        Index("ix_ethiopic_font_active", "is_active"),
        Index("ix_ethiopic_font_weight", "weight"),
    )


class StudyGuideCache(Base):
    """A generated study guide, keyed by passage + language + contract version.

    Guides are expensive and were regenerated on every request. Measured on
    Psalm 23:1-6: 9.8s in English but 51.4s in Amharic — Ethiopic script costs far
    more tokens per word, and a guide is generated with max_tokens=8192. At 51s a
    single successful attempt already consumed half the iOS client's 120s budget,
    so any retry surfaced to the user as "network issue" after a long wait.

    Caching makes a repeat request for the same passage effectively free, which is
    the common case: a study leader opening the same passage in several languages,
    or a group revisiting it.

    contract_version is part of the key on purpose — changing the content contract
    in app/ai must not keep serving guides produced under the old rules.
    """
    __tablename__ = "study_guide_cache"

    id = Column(Integer, primary_key=True, autoincrement=True)

    book_id          = Column(Integer, ForeignKey("books.id"), nullable=False)
    language_code    = Column(String(10), nullable=False)
    chapter          = Column(Integer, nullable=False)
    verse_start      = Column(Integer, nullable=True)
    verse_end        = Column(Integer, nullable=True)

    # The full guide payload, exactly as returned to the client.
    guide_json       = Column(Text, nullable=False)
    contract_version = Column(String(20), nullable=False)

    created_at       = Column(DateTime(timezone=True), server_default=func.now())

    book = relationship("Book")

    __table_args__ = (
        UniqueConstraint(
            "book_id", "language_code", "chapter", "verse_start", "verse_end",
            "contract_version",
            name="uq_study_guide_cache_passage",
        ),
        Index(
            "ix_study_guide_cache_lookup",
            "book_id", "language_code", "chapter", "verse_start", "verse_end",
        ),
    )
