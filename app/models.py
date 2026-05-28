from sqlalchemy import Column, Integer, String, Text, Boolean, UniqueConstraint, ForeignKey, Index, DateTime
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
