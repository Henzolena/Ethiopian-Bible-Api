from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from app.config import settings
from app.database import init_db
from app.routers import languages, books, verses, search, audio, coverage, quiz

app = FastAPI(
    title=settings.api_title,
    version=settings.api_version,
    description="""
## Ethiopian Bible API

A fast, unified REST API serving the Holy Bible in **Amharic** (አማርኛ),
**Oromo** (Afaan Oromoo), **Tigrigna** (ትግርኛ), **English KJV**, and **English NIV**.

### Language Codes
| Code  | Language | Native Name   |
|-------|----------|---------------|
| `am`  | Amharic  | አማርኛ           |
| `or`  | Oromo    | Afaan Oromoo  |
| `ti`  | Tigrigna | ትግርኛ           |
| `en`  | English  | English KJV   |
| `niv` | English  | English NIV   |

### Book Identifiers
Use a book **number** (1–66) or **abbreviation** (GEN, EXO, MAT…).

### Quick Examples
- `GET /api/v1/am/books/JHN/3/16` → John 3:16 in Amharic
- `GET /api/v1/or/books/1/1` → Genesis chapter 1 in Oromo
- `GET /api/v1/ti/random` → Random verse in Tigrigna
- `GET /api/v1/en/search?q=faith` → Search "faith" in English KJV
- `GET /api/v1/am/passage?ref=John+3:16` → Reference lookup
    """,
    docs_url="/docs",
    redoc_url="/redoc",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.on_event("startup")
async def on_startup():
    await init_db()


@app.get("/", tags=["Info"])
async def root():
    return JSONResponse({
        "name": "Ethiopian Bible API",
        "version": settings.api_version,
        "languages": {
            "am":  "Amharic (አማርኛ)",
            "or":  "Oromo (Afaan Oromoo)",
            "ti":  "Tigrigna (ትግርኛ)",
            "en":  "English (KJV)",
            "niv": "English (NIV)",
        },
        "docs": "/docs",
        "endpoints": {
            "languages": "/api/v1/languages",
            "books": "/api/v1/{lang}/books",
            "testament": "/api/v1/{lang}/testament/OT|NT",
            "chapter": "/api/v1/{lang}/books/{book}/{chapter}",
            "verse": "/api/v1/{lang}/books/{book}/{chapter}/{verse}",
            "random": "/api/v1/{lang}/random",
            "votd": "/api/v1/{lang}/votd",
            "passage": "/api/v1/{lang}/passage?ref=John+3:16",
            "search": "/api/v1/{lang}/search?q=term",
            "compare": "/api/v1/compare/{book}/{chapter}/{verse}",
            "audio": "/api/v1/{lang}/audio/{book}/{chapter}",
            "audio_info": "/api/v1/{lang}/audio/{book}/{chapter}/info",
            "coverage": "/api/v1/coverage",
            "coverage_lang": "/api/v1/coverage/{lang}",
            "quiz_chapter": "/api/v1/quiz/{lang}/books/{book}/{chapter}",
            "quiz_verse": "/api/v1/quiz/{lang}/books/{book}/{chapter}/{verse}",
            "quiz_random": "/api/v1/quiz/random",
            "quiz_generate": "POST /api/v1/quiz/generate",
            "quiz_answer": "POST /api/v1/quiz/answer",
            "quiz_stats": "/api/v1/quiz/stats",
        },
    })


@app.get("/health", tags=["Info"])
async def health():
    return {"status": "ok"}


PREFIX = "/api/v1"
app.include_router(languages.router, prefix=PREFIX)
app.include_router(books.router, prefix=PREFIX)
app.include_router(verses.router, prefix=PREFIX)
app.include_router(search.router, prefix=PREFIX)
app.include_router(audio.router, prefix=PREFIX)
app.include_router(coverage.router, prefix=PREFIX)
app.include_router(quiz.router, prefix=PREFIX)
