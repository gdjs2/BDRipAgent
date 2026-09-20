from functools import lru_cache

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from shared.config import get_settings


class Base(DeclarativeBase):
    pass


@lru_cache
def engine():
    url = get_settings().database_url
    return create_engine(
        url, pool_pre_ping=True, connect_args={"check_same_thread": False} if url.startswith("sqlite") else {}
    )


def session():
    return sessionmaker(engine(), expire_on_commit=False)()


def get_db():
    with session() as db:
        yield db
