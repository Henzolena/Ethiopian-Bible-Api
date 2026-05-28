from sqlalchemy import Column, Integer, String, Text, UniqueConstraint, ForeignKey, Index
from sqlalchemy.orm import relationship
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
