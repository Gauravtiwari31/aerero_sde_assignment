"""
Pytest Configuration & Shared Fixtures
======================================
Each test session runs against a throwaway SQLite database and a throwaway
upload directory, so tests never touch development data and can run in any
order.

The ``get_db`` dependency is overridden rather than monkeypatching the engine,
which keeps production wiring untouched and exercises the real dependency
injection path.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.database import Base, get_db
from app.main import app


@pytest.fixture(scope="session")
def event_loop():
    """Provide a single event loop for the whole session.

    Session-scoped async fixtures need a loop that outlives individual tests;
    pytest-asyncio's default function-scoped loop would be closed too early.
    """
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()


@pytest_asyncio.fixture(scope="session")
async def test_engine(tmp_path_factory):
    """Create a file-backed SQLite engine for the session.

    A file (rather than ``:memory:``) is used because the processing pipeline
    opens its own connection from a worker thread, which an in-memory database
    would not share.
    """
    db_dir = tmp_path_factory.mktemp("db")
    url = f"sqlite+aiosqlite:///{(db_dir / 'test.db').as_posix()}"

    engine = create_async_engine(url, future=True)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    yield engine

    await engine.dispose()


@pytest_asyncio.fixture
async def session_factory(test_engine):
    """Return a session factory bound to the test engine."""
    return async_sessionmaker(bind=test_engine, expire_on_commit=False)


@pytest_asyncio.fixture
async def client(test_engine, session_factory, tmp_path, monkeypatch):
    """Yield an HTTP client wired to the app with test dependencies.

    Both the request-scoped session and the pipeline's own session factory are
    redirected to the test engine, so background processing writes where the
    assertions read.
    """
    import app.geospatial as geospatial
    from app.config import get_settings

    settings = get_settings()
    upload_dir = tmp_path / "uploads"
    upload_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(settings, "upload_dir", str(upload_dir))

    # The pipeline builds its own session because it runs detached from the
    # request; point it at the same test database.
    monkeypatch.setattr(geospatial, "async_session_factory", session_factory)

    async def override_get_db():
        async with session_factory() as session:
            try:
                yield session
                await session.commit()
            except Exception:
                await session.rollback()
                raise

    app.dependency_overrides[get_db] = override_get_db

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as ac:
        yield ac

    app.dependency_overrides.clear()


@pytest.fixture
def upload_and_wait(client):
    """Return a helper that uploads a file and waits for processing to finish.

    Background tasks in Starlette's test transport run after the response is
    produced, so the helper polls the status endpoint rather than assuming the
    result is ready immediately.
    """

    async def _upload(filename: str, content: bytes, *, expect_status: int = 202):
        files = {"file": (filename, content, "application/octet-stream")}
        response = await client.post("/api/files/", files=files)
        assert response.status_code == expect_status, response.text
        if expect_status != 202:
            return response, None

        file_id = response.json()["id"]

        for _ in range(100):
            info = await client.get(f"/api/files/{file_id}/")
            if info.json()["status"] in ("COMPLETED", "FAILED"):
                return response, info.json()
            await asyncio.sleep(0.05)

        raise AssertionError(f"File {file_id} did not finish processing in time")

    return _upload
