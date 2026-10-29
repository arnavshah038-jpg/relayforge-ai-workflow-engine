from sqlalchemy import create_engine, event
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from app.config import settings


class Base(DeclarativeBase):
    pass


_is_sqlite = settings.database_url.startswith("sqlite")

engine = create_engine(
    settings.database_url,
    connect_args={"check_same_thread": False, "timeout": 10} if _is_sqlite else {},
    pool_pre_ping=True,
)

# expire_on_commit is off because the runner commits a lot and we don't
# want every attribute access after a commit to hit the database again
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)


@event.listens_for(engine, "connect")
def _enable_fk(dbapi_conn, _record):
    if _is_sqlite:
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA foreign_keys=ON")
        cur.close()


def get_db():
    with SessionLocal() as db:
        yield db
