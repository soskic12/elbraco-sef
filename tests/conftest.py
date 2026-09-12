from __future__ import annotations

import os
from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture(scope="session", autouse=True)
def _env(tmp_path_factory: pytest.TempPathFactory) -> None:
    """Test okruzenje: svoja SQLite baza i svoj storage, bez pravog SEF-a."""
    tmp = tmp_path_factory.mktemp("sefsync")
    os.environ.update(
        {
            "SEF_DISABLE_DOTENV": "1",
            "DB_URL": f"sqlite:///{(tmp / 'test.db').as_posix()}",
            "STORAGE_DIR": str(tmp / "data"),
            "SEF_ENV": "demo",
            "SEF_API_KEY": "test-key",
            "NOTIFY_EMAIL_ENABLED": "false",
            "PUSH_ENABLED": "false",
            "AUTO_ACCEPT": "off",
            "LOG_LEVEL": "WARNING",
        }
    )
    from sefsync.config import get_settings

    get_settings.cache_clear()


@pytest.fixture
def db():
    """Prazna baza za svaki test."""
    from sefsync import db as db_module
    from sefsync.models import Base

    engine = db_module.get_engine()
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    return db_module


@pytest.fixture
def faktura_xml() -> bytes:
    return (FIXTURES / "faktura_maloprodaja.xml").read_bytes()


@pytest.fixture
def odobrenje_xml() -> bytes:
    return (FIXTURES / "knjizno_odobrenje_omot.xml").read_bytes()
