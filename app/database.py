from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine, async_sessionmaker
from sqlalchemy.orm import DeclarativeBase
from app.config import settings

_db_url = settings.get_database_url()

# Supabase (PostgreSQL) requires SSL + prepared_statement_cache_size=0 for
# PgBouncer Transaction Mode (port 6543). SQLite needs no extra args.
_connect_args = (
    {"ssl": "require", "prepared_statement_cache_size": 0}
    if _db_url.startswith("postgresql")
    else {}
)

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
