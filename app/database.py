from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine, async_sessionmaker
from sqlalchemy.orm import DeclarativeBase
from app.config import settings

_db_url = settings.get_database_url()

# Railway internal PostgreSQL (.railway.internal) — no SSL needed
# Supabase / external PostgreSQL — SSL required
# SQLite — no extra args
def _get_connect_args(url: str) -> dict:
    if not url.startswith("postgresql"):
        return {}
    if ".railway.internal" in url:
        return {}   # internal Railway network, no SSL
    return {"ssl": "require", "prepared_statement_cache_size": 0}

_connect_args = _get_connect_args(_db_url)

engine = create_async_engine(_db_url, echo=False, connect_args=_connect_args)
AsyncSessionLocal = async_sessionmaker(engine, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


async def get_db() -> AsyncSession:
    async with AsyncSessionLocal() as session:
        yield session


async def init_db():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
