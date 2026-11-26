from functools import lru_cache

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.core.config import settings


@lru_cache
def engine():
    return create_engine(
        settings().database_url.get_secret_value(),
        pool_pre_ping=True,
        connect_args={"connect_timeout": 3},
    )


def session():
    with Session(engine(), expire_on_commit=False) as db:
        yield db

