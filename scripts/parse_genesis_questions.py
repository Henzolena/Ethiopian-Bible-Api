"""
Parse Ted Hildebrandt's Genesis Multiple Choice Questions PDF (NIV-based)
and seed them into the quiz_questions table.

Source PDF: data/genesis_questions.pdf  (or provide path as CLI arg)
Questions set: ~1300+ questions across Genesis chapters 1–50
Author: Ted Hildebrandt (biblicalelearning.org)
Language: NIV

Answer key line format at end of each question:
  ANSWER:DIFFICULTY:_:Gn:CHAPTER    (e.g. "B:B:Gn:1" = Answer B, Beginner, Gen ch 1)
  or sometimes "A:I: :Gn:1"
  Difficulty codes: B=beginner, I=intermediate, A=advanced

Usage:
  python -m scripts.parse_genesis_questions [path/to/pdf]
"""
import re
import sys
import asyncio
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import pdfplumber
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker

from app.models import Base, Book, QuizQuestion

DATA_DIR = Path(__file__).parent.parent / "data"

import os
from urllib.parse import quote_plus

def _build_db_url() -> str:
    db_host = os.getenv("DB_HOST", "")
    if db_host:
        user     = quote_plus(os.getenv("DB_USER", ""))
        password = quote_plus(os.getenv("DB_PASSWORD", ""))
        port     = os.getenv("DB_PORT", "5432")
        name     = os.getenv("DB_NAME", "postgres")
        return f"postgresql+asyncpg://{user}:{password}@{db_host}:{port}/{name}"
    env_url = os.getenv("DATABASE_URL", "")
    return env_url if env_url else f"sqlite+aiosqlite:///{DATA_DIR / 'bible.db'}"

DB_URL = _build_db_url()
_IS_POSTGRES = DB_URL.startswith("postgresql")
_CONNECT_ARGS = {"ssl": "require"} if _IS_POSTGRES else {}

AUTHOR = "Ted Hildebrandt (biblicalelearning.org)"
LANGUAGE = "niv"

DIFFICULTY_MAP = {"B": "beginner", "I": "intermediate", "A": "advanced"}

# Regex for the answer-key line at the bottom of each question
# Matches: "B:B:Gn:1"  or  "A:I: :Gn:1"  or  "D:A:Gn:1"
ANSWER_KEY_RE = re.compile(
    r"^([ABCD])\s*:\s*([BIA])\s*:(?:\s*:)?\s*Gn\s*:\s*(\d+)\s*$",
    re.IGNORECASE,
)

# Chapter header: "Genesis 3 Multiple Choice Questions"
CHAPTER_HEADER_RE = re.compile(r"Genesis\s+(\d+)\s+Multiple\s+Choice\s+Questions", re.IGNORECASE)

# Option lines: "A. Some text"  or  "A. Some text continued"
OPTION_RE = re.compile(r"^([ABCD])\.\s+(.+)$")

# Question reference extractor: "(Gen. 1:1)" or "(Gen 1:2-5)"
VERSE_REF_RE = re.compile(r"\(Gen[.\s]+(\d+)\s*:\s*(\d+)(?:\s*[-–]\s*(\d+))?\)", re.IGNORECASE)


def _extract_all_text(pdf_path: Path) -> list[str]:
    """Return all lines from the PDF, stripped."""
    lines = []
    with pdfplumber.open(str(pdf_path)) as pdf:
        for page in pdf.pages:
            raw = page.extract_text(x_tolerance=3, y_tolerance=3)
            if raw:
                for line in raw.split("\n"):
                    line = line.strip()
                    if line:
                        lines.append(line)
    return lines


def _parse_questions(lines: list[str]) -> list[dict]:
    """
    State-machine parser over all PDF lines.
    Returns list of raw question dicts.
    """
    questions = []
    current_chapter = None

    i = 0
    while i < len(lines):
        line = lines[i]

        # ── Chapter header ──────────────────────────────────────────────
        m = CHAPTER_HEADER_RE.search(line)
        if m:
            current_chapter = int(m.group(1))
            i += 1
            continue

        # ── Skip page numbers (lone digits) ─────────────────────────────
        if line.isdigit():
            i += 1
            continue

        # ── Skip the title page lines ────────────────────────────────────
        if "Genesis Multiple Choice" in line or "biblicalelearning" in line or "Ted Hildebrandt" in line:
            i += 1
            continue

        # ── Question number line  (e.g. "1." or "23.") ──────────────────
        q_num_match = re.match(r"^(\d+)\.\s+(.+)$", line)
        if q_num_match and current_chapter is not None:
            q_num = int(q_num_match.group(1))
            q_text = q_num_match.group(2)

            # Collect continuation lines until we hit option A
            i += 1
            while i < len(lines) and not OPTION_RE.match(lines[i]) and not ANSWER_KEY_RE.match(lines[i]):
                next_line = lines[i]
                if next_line.isdigit():
                    break
                if CHAPTER_HEADER_RE.search(next_line):
                    break
                q_text += " " + next_line
                i += 1

            # Collect options A–D
            options = {}
            for expected in ("A", "B", "C", "D"):
                if i < len(lines) and OPTION_RE.match(lines[i]):
                    om = OPTION_RE.match(lines[i])
                    opt_letter = om.group(1).upper()
                    opt_text = om.group(2)
                    i += 1
                    # Continuation lines for this option
                    while i < len(lines):
                        nxt = lines[i]
                        if OPTION_RE.match(nxt) or ANSWER_KEY_RE.match(nxt) or nxt.isdigit():
                            break
                        if CHAPTER_HEADER_RE.search(nxt):
                            break
                        # Next question number line
                        if re.match(r"^\d+\.\s+", nxt):
                            break
                        opt_text += " " + nxt
                        i += 1
                    options[opt_letter] = opt_text.strip()

            # Answer key line
            answer = difficulty_code = None
            if i < len(lines) and ANSWER_KEY_RE.match(lines[i]):
                ak = ANSWER_KEY_RE.match(lines[i])
                answer = ak.group(1).upper()
                difficulty_code = ak.group(2).upper()
                i += 1
            else:
                # Skip malformed question
                continue

            # Extract verse reference from question text
            vref = VERSE_REF_RE.search(q_text)
            verse_start = int(vref.group(2)) if vref else None
            verse_end   = int(vref.group(3)) if (vref and vref.group(3)) else verse_start

            if len(options) < 4 or not answer:
                continue  # skip incomplete

            questions.append({
                "chapter":      current_chapter,
                "q_num":        q_num,
                "question":     q_text.strip(),
                "option_a":     options.get("A", ""),
                "option_b":     options.get("B", ""),
                "option_c":     options.get("C", ""),
                "option_d":     options.get("D", ""),
                "correct_answer": answer,
                "difficulty":   DIFFICULTY_MAP.get(difficulty_code, "beginner"),
                "verse_start":  verse_start,
                "verse_end":    verse_end,
            })
            continue

        i += 1

    return questions


async def seed(pdf_path: Path):
    lines = _extract_all_text(pdf_path)
    parsed = _parse_questions(lines)
    print(f"[quiz] Parsed {len(parsed)} questions from PDF")

    engine = create_async_engine(DB_URL, echo=False, connect_args=_CONNECT_ARGS)
    Session = async_sessionmaker(engine, expire_on_commit=False)

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    async with Session() as db:
        # Get GEN book_id
        result = await db.execute(select(Book).where(Book.abbreviation == "GEN"))
        gen_book = result.scalar_one_or_none()
        if not gen_book:
            print("[quiz] ERROR: GEN book not found — run seed_database.py first")
            return

        # Check existing
        existing = (await db.execute(
            text("SELECT COUNT(*) FROM quiz_questions WHERE book_id = :bid AND language_code = :lang"),
            {"bid": gen_book.id, "lang": LANGUAGE},
        )).scalar()
        if existing > 0:
            print(f"[quiz] Genesis already has {existing} questions — skipping (use --force to re-seed)")
            return

        batch = []
        for q in parsed:
            batch.append(QuizQuestion(
                book_id        = gen_book.id,
                language_code  = LANGUAGE,
                chapter        = q["chapter"],
                verse_start    = q["verse_start"],
                verse_end      = q["verse_end"],
                question       = q["question"],
                option_a       = q["option_a"],
                option_b       = q["option_b"],
                option_c       = q["option_c"],
                option_d       = q["option_d"],
                correct_answer = q["correct_answer"],
                difficulty     = q["difficulty"],
                source         = "static",
                author         = AUTHOR,
                is_verified    = True,
            ))

        db.add_all(batch)
        await db.commit()
        print(f"[quiz] Seeded {len(batch)} Genesis questions (NIV) ✓")

        # Stats breakdown
        by_chapter: dict[int, int] = {}
        for q in parsed:
            by_chapter[q["chapter"]] = by_chapter.get(q["chapter"], 0) + 1
        print(f"[quiz] Chapters covered: {len(by_chapter)} ({min(by_chapter)} – {max(by_chapter)})")
        by_diff: dict[str, int] = {}
        for q in parsed:
            by_diff[q["difficulty"]] = by_diff.get(q["difficulty"], 0) + 1
        for d, c in sorted(by_diff.items()):
            print(f"       {d}: {c}")

    await engine.dispose()


def main():
    force = "--force" in sys.argv
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    pdf_path = Path(args[0]) if args else DATA_DIR / "genesis_questions.pdf"

    if not pdf_path.exists():
        # Try the Downloads folder as fallback
        fallback = Path.home() / "Downloads" / "01_GenesisMCQuestions.pdf"
        if fallback.exists():
            pdf_path = fallback
        else:
            print(f"[quiz] PDF not found at {pdf_path}")
            print("  Pass path as argument:  python -m scripts.parse_genesis_questions /path/to/file.pdf")
            sys.exit(1)

    if force:
        asyncio.run(_drop_and_seed(pdf_path))
    else:
        asyncio.run(seed(pdf_path))


async def _drop_and_seed(pdf_path: Path):
    engine = create_async_engine(DB_URL, echo=False, connect_args=_CONNECT_ARGS)
    async with engine.begin() as conn:
        await conn.execute(text("DELETE FROM quiz_questions WHERE language_code = 'niv' AND book_id IN (SELECT id FROM books WHERE abbreviation = 'GEN')"))
    await engine.dispose()
    await seed(pdf_path)


if __name__ == "__main__":
    main()
