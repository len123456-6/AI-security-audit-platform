"""数据库连接层：engine / Session / 依赖注入 / 连通性检查。

为什么 SQLite 要额外做三件事：
  1. `check_same_thread=False`
     FastAPI 用线程池执行同步路由，连接会跨线程复用，默认的线程校验会直接报错。
  2. `PRAGMA foreign_keys = ON`
     SQLite 默认**不**校验外键，必须在每条连接上显式打开，否则 ON DELETE CASCADE 形同虚设。
  3. `PRAGMA journal_mode = WAL`
     读写并发更友好（写不阻塞读），对「一边写食谱一边有人刷列表」的场景很关键。

注：建表不在这里做，见 `app/db/init_db.py`（schema.sql 才是建表事实来源）。
"""

from __future__ import annotations

import logging
from collections.abc import Generator
from typing import Any

from sqlalchemy import create_engine, event, text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import settings

logger = logging.getLogger(__name__)


def _build_engine() -> Engine:
    """按配置创建 Engine；SQLite 走专用参数与 PRAGMA。"""
    connect_args: dict[str, Any] = {}
    if settings.is_sqlite:
        settings.ensure_data_dir()  # 库文件目录不存在时 create_engine 也不会建，这里先补上
        connect_args["check_same_thread"] = False

    created = create_engine(
        settings.database_url,
        echo=settings.sql_echo,
        future=True,
        pool_pre_ping=True,
        connect_args=connect_args,
    )

    if settings.is_sqlite:

        @event.listens_for(created, "connect")
        def _set_sqlite_pragma(dbapi_connection: Any, _connection_record: Any) -> None:
            """每条新连接都设置一次 PRAGMA（SQLite 的 PRAGMA 是连接级的）。"""
            cursor = dbapi_connection.cursor()
            try:
                cursor.execute("PRAGMA foreign_keys = ON")
                cursor.execute("PRAGMA journal_mode = WAL")
                cursor.execute("PRAGMA synchronous = NORMAL")
            finally:
                cursor.close()

    return created


engine: Engine = _build_engine()

# expire_on_commit=False：commit 后仍可读对象属性，便于直接当作响应体返回
SessionLocal = sessionmaker(
    bind=engine,
    class_=Session,
    autoflush=False,
    autocommit=False,
    expire_on_commit=False,
)


def get_db() -> Generator[Session, None, None]:
    """FastAPI 依赖：每个请求一个 Session，异常回滚，结束必关。

    用法::

        @router.get("/recipes")
        def list_recipes(db: Annotated[Session, Depends(get_db)]) -> list[RecipeOut]:
            ...
    """
    db = SessionLocal()
    try:
        yield db
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def check_database() -> tuple[bool, str]:
    """探活：返回 (是否可用, 说明)。给 /health 用，不抛异常。"""
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        return True, "ok"
    except SQLAlchemyError as exc:  # pragma: no cover - 仅在库损坏/权限异常时触发
        logger.exception("数据库连通性检查失败")
        return False, f"{type(exc).__name__}: {exc}"


def dispose_engine() -> None:
    """关闭连接池，供应用退出时调用。"""
    engine.dispose()
