"""冒烟测试：应用能起来、/health 通、CORS 头正确、启动时自动建表。

跑法::

    python -m pytest            # 或用 pytest.ini 里的配置
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.core.config import settings
from app.main import app


@pytest.fixture(scope="module")
def client() -> TestClient:
    """with 语句会触发 lifespan，从而真正执行 init_db 建表。"""
    with TestClient(app) as test_client:
        yield test_client


def test_health_returns_ok(client: TestClient) -> None:
    response = client.get("/health")
    assert response.status_code == 200

    body = response.json()
    assert body["status"] == "ok"
    assert body["database"] == "ok"
    assert body["version"] == settings.app_version


def test_root_returns_service_info(client: TestClient) -> None:
    response = client.get("/")
    assert response.status_code == 200

    body = response.json()
    assert body["service"] == settings.app_name
    assert body["docs"] == "/docs"
    assert body["health"] == "/health"


def test_openapi_docs_available(client: TestClient) -> None:
    """OpenAPI 文档能生成，说明应用装配没问题。"""
    assert client.get("/openapi.json").status_code == 200
    assert client.get("/docs").status_code == 200


def test_cors_preflight_allows_whitelisted_origin(client: TestClient) -> None:
    """白名单内的 Origin 要拿到 Access-Control-Allow-Origin。"""
    origin = settings.cors_origin_list[0]
    response = client.options(
        "/health",
        headers={
            "Origin": origin,
            "Access-Control-Request-Method": "GET",
        },
    )
    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == origin
    assert response.headers["access-control-allow-credentials"] == "true"


def test_cors_blocks_unknown_origin(client: TestClient) -> None:
    """不在白名单的 Origin 不能拿到放行头（浏览器据此拦截）。"""
    response = client.get("/health", headers={"Origin": "http://evil.example.com"})
    assert "access-control-allow-origin" not in response.headers


def test_startup_created_database_and_tables(client: TestClient) -> None:
    """启动时自动建表：库文件存在，三张核心表都在，种子数据可用。"""
    db_path = settings.sqlite_path
    assert isinstance(db_path, Path) and db_path.is_file()

    assert client.app.state.db_summary["tables"], "lifespan 未记录建表结果"

    conn = sqlite3.connect(db_path)
    try:
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
            )
        }
        assert {"recipes", "ingredients", "recipe_ingredients"} <= tables
        assert conn.execute("SELECT COUNT(*) FROM recipes").fetchone()[0] >= 2
        # 触发器（维护 updated_at）也随 schema.sql 建好了
        triggers = {
            row[0]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'trigger'")
        }
        assert triggers, "schema.sql 里的 updated_at 触发器没有建出来"
    finally:
        conn.close()


def test_engine_connections_enable_sqlite_pragmas(client: TestClient) -> None:
    """database.py 的 PRAGMA 钩子必须生效。

    PRAGMA 是**连接级**设置：裸 sqlite3.connect 默认 foreign_keys=OFF，
    所以只能从我们自己的 engine 连接上断言，否则测的就不是应用行为了。
    """
    from app.core.database import engine

    with engine.connect() as connection:
        assert connection.exec_driver_sql("PRAGMA foreign_keys").scalar() == 1
        assert str(connection.exec_driver_sql("PRAGMA journal_mode").scalar()).lower() == "wal"


def test_init_db_is_idempotent(client: TestClient) -> None:
    """重复建表不清数据，这是「启动自动建表」能安全执行的前提。"""
    from app.db.init_db import init_db

    conn = sqlite3.connect(settings.sqlite_path)
    try:
        before = conn.execute("SELECT COUNT(*) FROM recipes").fetchone()[0]
    finally:
        conn.close()

    summary = init_db()
    assert summary["created"] is False  # 库已存在，走复用分支

    conn = sqlite3.connect(settings.sqlite_path)
    try:
        after = conn.execute("SELECT COUNT(*) FROM recipes").fetchone()[0]
    finally:
        conn.close()

    assert after == before
