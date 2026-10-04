import os
import uuid

os.environ.setdefault("SEO_EMBEDDED_WORKER", "0")  # test không chạy worker nhúng của app

import psycopg
import pytest

from pipeline.config import load_config
from pipeline.db import connect, dsn, migrate


@pytest.fixture(scope="session")
def db_url():
    """Tạo database tạm cho cả phiên test; bỏ qua test DB nếu không kết nối được Postgres."""
    admin = dsn()
    try:
        c = psycopg.connect(admin, autocommit=True, connect_timeout=3)
    except psycopg.OperationalError:
        pytest.skip("không có Postgres (docker compose up -d)")
    name = f"seo_test_{uuid.uuid4().hex[:8]}"
    c.execute(f'CREATE DATABASE "{name}"')
    url = admin.rsplit("/", 1)[0] + f"/{name}"
    with connect(url, autocommit=True) as m:
        migrate(m)
    yield url
    c.execute(f'DROP DATABASE "{name}" WITH (FORCE)')
    c.close()


@pytest.fixture
def conn(db_url):
    with connect(db_url, autocommit=True) as c:
        tables = [r["tablename"] for r in c.execute(
            "SELECT tablename FROM pg_tables WHERE schemaname = 'public' AND tablename <> 'schema_migrations'")]
        c.execute(f"TRUNCATE {', '.join(tables)} CASCADE")
        yield c


@pytest.fixture
def config():
    """Test cũ kiểm tra máy trạng thái như spec (NEEDS_HUMAN, gate shadow): tắt autopilot và đưa gate về shadow.
    Test autopilot dùng fixture auto_config."""
    cfg = load_config()
    cfg["automation"] = {**cfg["automation"], "enabled": False}
    cfg["gates"]["mode"] = {g: ("auto" if g == "title" else "shadow") for g in cfg["gates"]["mode"]}
    return cfg


@pytest.fixture
def auto_config():
    return load_config()


@pytest.fixture
def blob_dir(tmp_path):
    os.environ["SEO_BLOB_DIR"] = str(tmp_path / "blobs")
    return tmp_path / "blobs"
