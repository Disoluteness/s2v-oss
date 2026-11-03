from collections.abc import Generator

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from apps.api.app.config import settings


class Base(DeclarativeBase):
    pass


def enforce_sqlite_foreign_keys(engine: Engine) -> Engine:
    """Make SQLite check ``FOREIGN KEY`` the way MySQL/InnoDB does.

    SQLite ignores foreign keys unless the pragma is set per connection, so an
    invalid reference silently passes locally and only fails once the same code
    runs against MySQL (IntegrityError 1452 -> HTTP 500). Both the application
    engine and the test engines go through this helper so the two environments
    agree.
    """
    if engine.dialect.name != "sqlite":
        return engine

    @event.listens_for(engine, "connect")
    def _set_sqlite_foreign_keys(dbapi_connection, _connection_record):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    return engine


engine = enforce_sqlite_foreign_keys(
    create_engine(settings.database_url, pool_pre_ping=True)
)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)


def get_session() -> Generator[Session, None, None]:
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()
