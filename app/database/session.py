from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy.orm import Session, sessionmaker

from app.database.connection import get_engine


@contextmanager
def session_scope(role: str = "api") -> Iterator[Session]:
    """Short-lived session: commits on success, rolls back on error, always closes."""
    session = sessionmaker(bind=get_engine(role), expire_on_commit=False)()
    try:
        yield session
        session.commit()
    except BaseException:
        session.rollback()
        raise
    finally:
        session.close()
