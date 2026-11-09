"""FastAPI 应用入口。

启动流程（lifespan）：
    init_db()  执行 schema.sql，库/表缺失时自动创建（幂等，不会清数据）
      ↓
    挂载 CORS / 路由
      ↓
    进程退出时 dispose 连接池

本地运行::

    uvicorn app.main:app --reload
    # 或 python -m app.main
"""

from __future__ import annotations
from dotenv import load_dotenv
load_dotenv()
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app import __version__
from app.api.v1.router import api_router
from app.core.config import settings
from app.core.database import check_database, dispose_engine
from app.db.init_db import init_db

logging.basicConfig(
    level=logging.DEBUG if settings.debug else logging.INFO,
    format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """应用生命周期：启动时建表，退出时释放连接池。"""
    logger.info("启动 %s v%s（env=%s）", settings.app_name, settings.app_version, settings.env)
    try:
        app.state.db_summary = init_db()
        logger.info("数据库已就绪: %s", app.state.db_summary["db_path"])
    except Exception:  # 建库失败必须让进程起不来，避免半可用状态
        logger.exception("启动建表失败，服务中止")
        raise
    yield
    dispose_engine()
    logger.info("服务已停止，数据库连接池已释放")


app = FastAPI(
    title="AI 代码安全审计平台",
    version=settings.app_version,
    description="AI 代码安全审计平台 · 后端 MVP（DeepSeek 静态代码审计 / 漏洞报告）",
    lifespan=lifespan,
    docs_url="/docs",
    redoc_url="/redoc",
    openapi_url="/openapi.json",
)

# --- CORS ---
# allow_credentials=True 时 allow_origins 不能是 "*"（浏览器会拒绝），
# 所以必须用 .env 里显式列出的白名单；本地前端地址见 .env.example。
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origin_list,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PATCH", "PUT", "DELETE", "OPTIONS"],
    allow_headers=["*"],
    expose_headers=["X-Request-ID"],
    max_age=600,  # 预检结果缓存 10 分钟
)


@app.get("/", tags=["meta"], summary="服务信息")
def read_root() -> dict[str, Any]:
    """根路径：确认服务活着，并给出文档入口。"""
    return {
        "service": settings.app_name,
        "version": __version__,
        "env": settings.env,
        "docs": "/docs",
        "health": "/health",
    }


@app.get("/health", tags=["meta"], summary="健康检查（含数据库探活）")
def health() -> dict[str, Any]:
    """测试接口：服务 + 数据库都正常才返回 status=ok。

    数据库不可用时返回 200 + status=degraded，而不是 500 ——
    这样 K8s / 负载均衡的健康检查能拿到结构化原因，前端也好展示。
    """
    db_ok, db_message = check_database()
    return {
        "status": "ok" if db_ok else "degraded",
        "database": db_message,
        "version": settings.app_version,
        "env": settings.env,
    }


# --- 业务路由 ---
# 各业务域的路由在 app/api/v1/router.py 汇总，这里只负责加统一版本前缀。
app.include_router(api_router, prefix=settings.api_v1_prefix)


if __name__ == "__main__":  # pragma: no cover
    import uvicorn

    uvicorn.run("app.main:app", host="127.0.0.1", port=8000, reload=settings.debug)
