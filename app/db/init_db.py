"""建库脚本：执行 schema.sql，把 SQLite 库准备好。

设计取舍：
  - 用标准库 `sqlite3` 而不是 SQLAlchemy —— schema.sql 是一整段多语句 SQL，
    `executescript()` 能一次跑完，且不受 ORM 事务/连接池干扰；
  - 幂等：schema.sql 全部是 `CREATE ... IF NOT EXISTS` + `INSERT OR IGNORE`，
    所以「启动时自动建表」重复执行是安全的，不会清掉已有数据；
  - 库文件路径来自 settings.sqlite_path（绝对路径），无论从哪个目录启动都一致。

命令行用法::

    python -m app.db.init_db            # 建表（已存在则跳过）
    python -m app.db.init_db --reset    # 先删库文件再重建（会丢数据！）
"""

from __future__ import annotations

import logging
import sqlite3
import sys
from pathlib import Path

from app.core.config import SCHEMA_FILE, settings

logger = logging.getLogger(__name__)

CORE_TABLES: tuple[str, ...] = ("recipes", "ingredients", "recipe_ingredients")


def _table_names(conn: sqlite3.Connection) -> list[str]:
    rows = conn.execute(
        "SELECT name FROM sqlite_master "
        "WHERE type = 'table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
    ).fetchall()
    return [row[0] for row in rows]


def init_db(schema_file: Path | None = None, reset: bool = False) -> dict[str, object]:
    """连接 SQLite 并执行建表 SQL，返回本次结果摘要。

    Args:
        schema_file: 建表 SQL 路径，默认项目根目录的 schema.sql。
        reset: 为 True 时先删除库文件（连同 -wal / -shm），用于本地重来。

    Returns:
        含 db_path / created / tables / seeded 的摘要字典，可直接写日志。
    """
    schema_path = Path(schema_file or SCHEMA_FILE)
    if not schema_path.is_file():
        raise FileNotFoundError(f"建表脚本不存在: {schema_path}")

    db_path = settings.ensure_data_dir()
    if db_path is None:  # 非 SQLite 配置下本脚本不适用
        raise RuntimeError(
            f"init_db 只支持 SQLite，当前 DATABASE_URL={settings.database_url}；"
            "换库请改用 Alembic 迁移。"
        )

    existed = db_path.exists()
    if reset and existed:
        for suffix in ("", "-wal", "-shm"):
            Path(f"{db_path}{suffix}").unlink(missing_ok=True)
        existed = False
        logger.warning("已删除旧库文件: %s", db_path)

    sql = schema_path.read_text(encoding="utf-8")
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("PRAGMA foreign_keys = ON")
        # executescript 会先隐式提交当前事务，再逐条执行脚本内的语句
        conn.executescript(sql)
        conn.commit()

        tables = _table_names(conn)
        missing = [t for t in CORE_TABLES if t not in tables]
        if missing:
            raise RuntimeError(f"建表后缺少核心表: {missing}（检查 schema.sql）")

        counts = {
            table: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in CORE_TABLES
        }
    finally:
        conn.close()

    summary: dict[str, object] = {
        "db_path": str(db_path),
        "created": not existed,  # 本次是否新建了库文件
        "tables": tables,
        "rows": counts,
        "schema": str(schema_path.name),
    }
    logger.info(
        "数据库就绪: %s（%s，表: %s，行数: %s）",
        summary["db_path"],
        "新建" if summary["created"] else "复用",
        ", ".join(tables),
        counts,
    )
    return summary


def main(argv: list[str] | None = None) -> int:
    """命令行入口：python -m app.db.init_db [--reset]"""
    args = list(sys.argv[1:] if argv is None else argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    try:
        summary = init_db(reset="--reset" in args)
    except (FileNotFoundError, RuntimeError, sqlite3.Error) as exc:
        print(f"[FAIL] 建库失败: {exc}", file=sys.stderr)
        return 1

    print(f"[OK] 库文件     : {summary['db_path']} ({'新建' if summary['created'] else '已存在, 复用'})")
    print(f"[OK] 建表脚本   : {summary['schema']}")
    print(f"[OK] 表         : {', '.join(summary['tables'])}")
    print(f"[OK] 数据行数   : {summary['rows']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
