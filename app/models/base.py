from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    """Mapping only. The schema is owned by Flyway (database/migrations) - never create_all()."""
