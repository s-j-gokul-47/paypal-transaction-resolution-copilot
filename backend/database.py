"""
database.py - SQLAlchemy database setup for PayPal Transaction Resolution Copilot
"""
import os
from sqlalchemy import create_engine, event
from sqlalchemy.orm import declarative_base, sessionmaker, Session
from typing import Generator

DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./copilot.db")

# SQLite-specific: enable WAL mode and foreign keys
connect_args = {}
if DATABASE_URL.startswith("sqlite"):
    connect_args = {"check_same_thread": False}

engine = create_engine(
    DATABASE_URL,
    connect_args=connect_args,
    echo=False,          # Set to True for SQL query logging during dev
    pool_pre_ping=True,
)

# Enable SQLite foreign key enforcement and WAL mode
if DATABASE_URL.startswith("sqlite"):
    @event.listens_for(engine, "connect")
    def set_sqlite_pragma(dbapi_connection, connection_record):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.close()

SessionLocal = sessionmaker(
    autocommit=False,
    autoflush=False,
    bind=engine,
)

Base = declarative_base()


def get_db() -> Generator[Session, None, None]:
    """
    FastAPI dependency that yields a database session and ensures it is
    closed after the request completes, even if an exception is raised.
    """
    db: Session = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def init_db() -> None:
    """
    Create all tables defined in the SQLAlchemy metadata and apply lightweight
    additive column migrations for existing SQLite databases.
    """
    import models  # noqa: F401
    Base.metadata.create_all(bind=engine)

    # Ensure new columns on webhook_events exist on pre-existing SQLite DBs
    if DATABASE_URL.startswith("sqlite"):
        with engine.begin() as conn:
            cols = {
                row[1]
                for row in conn.exec_driver_sql("PRAGMA table_info(webhook_events)").fetchall()
            }
            if cols:
                if "processing_status" not in cols:
                    conn.exec_driver_sql(
                        "ALTER TABLE webhook_events ADD COLUMN processing_status VARCHAR NOT NULL DEFAULT 'PROCESSED'"
                    )
                if "error_message" not in cols:
                    conn.exec_driver_sql(
                        "ALTER TABLE webhook_events ADD COLUMN error_message TEXT"
                    )
