"""Database engine, session handling, and bringing the tables up to date (migrations, task 1.7)."""
import threading
from pathlib import Path

from sqlalchemy import create_engine, event, inspect, text
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from . import config


class Base(DeclarativeBase):
    pass


LOCK_WAIT_SECONDS = 15       # a statement blocked by another transaction for this long fails instead of hanging


def make_engine(url: str | None = None):
    """SQLite for zero-setup local runs, PostgreSQL (psycopg 3) for the real thing; the URL decides."""
    url = url or config.DATABASE_URL
    pool = {"pool_size": config.DB_POOL_SIZE, "max_overflow": config.DB_POOL_OVERFLOW}    # see config.py for why
    if url.startswith("postgresql"):
        return create_engine(
            url, **pool,
            pool_pre_ping=True,       # a connection the server dropped (restart, idle cut-off) is replaced, not used
            connect_args={"options": f"-c lock_timeout={LOCK_WAIT_SECONDS * 1000}"})
    if not url.startswith("sqlite"):
        return create_engine(url)
    engine = create_engine(url, **pool, connect_args={"check_same_thread": False, "timeout": LOCK_WAIT_SECONDS})

    @event.listens_for(engine, "connect")
    def _sqlite_pragmas(dbapi_conn, _record):
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA journal_mode=WAL")   # readers do not block the writer
        cur.execute("PRAGMA foreign_keys=ON")
        cur.close()
    return engine


engine = make_engine()
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)


def get_db():
    """FastAPI dependency: one database session per request."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


# --------------------------------------------------------------------------- tables and migrations
#
# The tables are described twice: app/models.py says what they are now, and migrations/versions/ holds the steps
# that take an existing database from one shape to the next (Alembic). tests/test_migrations.py fails if the two
# disagree. See "When you change the database tables" in CONTRIBUTING.md.

ALEMBIC_INI = Path(__file__).resolve().parent.parent / "alembic.ini"
FIRST_MIGRATION = "0001"     # the tables as they were when migrations were added
MIGRATION_LOCK = 559_001     # any fixed number: the PostgreSQL advisory lock that app instances take to migrate
_one_thread = threading.Lock()   # Alembic keeps its state in module globals, so one thread at a time in a process


def _alembic(connection, command_name: str, revision: str) -> None:
    from alembic import command
    from alembic.config import Config

    config_ = Config(str(ALEMBIC_INI))
    config_.attributes["connection"] = connection      # migrations/env.py uses this instead of opening its own
    getattr(command, command_name)(config_, revision)


def _bring_up_to_date(recreate: bool) -> None:
    from . import models  # noqa: F401  (registers the tables on Base)

    with _one_thread, engine.begin() as connection:
        if connection.dialect.name == "postgresql":
            # Several app instances may start together: the first migrates, the others wait here and then find
            # nothing left to do. Released when this transaction ends. (SQLite allows one writer anyway.)
            connection.execute(text("SELECT pg_advisory_xact_lock(:n)"), {"n": MIGRATION_LOCK})
        tables = set(inspect(connection).get_table_names())
        if recreate and connection.dialect.name == "sqlite" and "alembic_version" in tables:
            # SQLite does not hold other connections back while tables are dropped: for a few seconds they found
            # "no such table" and answered 500 (issue #59). So keep the tables and delete their rows, children first.
            # Other connections go on reading the old rows until this commits. Ids start at 1 again by themselves.
            _alembic(connection, "upgrade", "head")
            for table in reversed(Base.metadata.sorted_tables):
                connection.execute(table.delete())
            return
        if recreate or not tables - {"alembic_version"}:
            if recreate:
                Base.metadata.drop_all(connection)
            Base.metadata.create_all(connection)
            _alembic(connection, "stamp", "head")
            return
        if "alembic_version" not in tables:
            _alembic(connection, "stamp", FIRST_MIGRATION)
        _alembic(connection, "upgrade", "head")


def recreate_tables() -> None:
    """Empty the database for seed.reset, with every table as models.py describes it and ids starting at 1.

    PostgreSQL: drop every table and create it again in one transaction; the timed jobs and other requests wait for
    the new tables instead of finding none. SQLite cannot make them wait, so there the tables stay and only their
    rows are deleted. A SQLite file from before migrations is still dropped and created, as it may have old tables."""
    _bring_up_to_date(recreate=True)


def init_db() -> None:
    """Called when the app starts. An empty database gets its tables; an existing one is migrated to the newest
    version (`alembic upgrade head`), so deploying new code and restarting is enough.

    A database made before migrations existed has tables but no version record. It is taken to be at the first
    migration, which is true if it was created by the code just before migrations were added; an older file than
    that must be deleted and recreated, as the rule was until now."""
    _bring_up_to_date(recreate=False)
