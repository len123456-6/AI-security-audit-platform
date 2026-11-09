"""应用配置：集中读取 .env / 环境变量。

约定：
  - 所有配置只能从这里取，禁止在业务代码里散落 os.getenv；
  - 字段名与环境变量名大小写不敏感（DATABASE_URL 或 database_url 都行）；
  - 默认值面向本地开发，生产环境必须用环境变量覆盖 SECRET_KEY / DATABASE_URL。
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

# d:\cook —— 项目根目录（本文件位于 app/core/config.py）
BASE_DIR: Path = Path(__file__).resolve().parent.parent.parent
SCHEMA_FILE: Path = BASE_DIR / "schema.sql"


class Settings(BaseSettings):
    """环境配置。字段名即环境变量名（大小写不敏感）。"""

    model_config = SettingsConfigDict(
        env_file=BASE_DIR / ".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",  # .env 里有多余键时不要报错
    )

    # --- 应用 ---
    # 注意：.env 里的 APP_NAME 会覆盖这里的默认值，改名字要两处一起改
    app_name: str = "AI 代码安全审计平台"
    app_version: str = "0.3.0"
    env: str = "development"  # development | staging | production
    debug: bool = True
    api_v1_prefix: str = "/api/v1"

    # --- 数据库 ---
    # 相对路径以「启动进程时的工作目录」为基准，所以 init_db 会先转成绝对路径
    database_url: str = "sqlite:///./data/cook.db"
    sql_echo: bool = False

    # --- 安全 ---
    secret_key: str = "dev-only-change-me"
    access_token_expire_minutes: int = 1440

    # --- CORS ---
    # 用逗号分隔的字符串而非 list：避免 pydantic-settings 对复杂类型强制 JSON 解析，
    # 让 .env 写 `CORS_ORIGINS=http://a.com,http://b.com` 就能用。
    cors_origins: str = "http://localhost:5173,http://127.0.0.1:5173,http://localhost:3000"

    # --- AI（DeepSeek，走 OpenAI 兼容协议）---
    # 密钥只从 .env / 环境变量读取，仓库与代码里都不出现真实 key。
    # 未配置时推荐接口返回 503，而不是让整个服务起不来。
    deepseek_api_key: str = ""
    deepseek_base_url: str = "https://api.deepseek.com"
    deepseek_model: str = "deepseek-chat"
    deepseek_timeout_seconds: float = 60.0  # 大模型首字节较慢，比普通外部接口放宽

    @property
    def has_deepseek_api_key(self) -> bool:
        """是否已配置 DeepSeek 密钥（接口层用于提前给出可读错误）。"""
        return bool(self.deepseek_api_key.strip())

    @property
    def cors_origin_list(self) -> list[str]:
        """把逗号分隔的 CORS 配置拆成列表，并过滤空项。"""
        return [origin.strip() for origin in self.cors_origins.split(",") if origin.strip()]

    @property
    def is_sqlite(self) -> bool:
        return self.database_url.startswith("sqlite")

    @property
    def sqlite_path(self) -> Path | None:
        """SQLite 库文件的绝对路径；非 SQLite 连接串返回 None。

        把 `sqlite:///./data/cook.db` 解析成 `d:\\cook\\data\\cook.db`，
        这样无论从哪个目录启动 uvicorn，都指向同一个库文件。
        """
        if not self.is_sqlite:
            return None
        raw = self.database_url.split("sqlite:///", 1)[-1]
        path = Path(raw)
        return path if path.is_absolute() else (BASE_DIR / path).resolve()

    def ensure_data_dir(self) -> Path | None:
        """确保 SQLite 库文件所在目录存在（首次启动时目录可能还没建）。"""
        path = self.sqlite_path
        if path is not None:
            path.parent.mkdir(parents=True, exist_ok=True)
        return path


@lru_cache
def get_settings() -> Settings:
    """带缓存的配置单例（测试里可用 get_settings.cache_clear() 重置）。"""
    return Settings()


settings = get_settings()
