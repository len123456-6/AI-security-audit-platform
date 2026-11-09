"""AI 代码安全审计平台 · 应用包。

分层约定（依赖方向只能向下，禁止反向 import）::

    api/       HTTP 层：路由、参数校验、状态码。不写业务判断。
      ↓
    services/  业务层：调用大模型做静态代码审计、清洗漏洞报告。
      ↓
    models/    ORM 模型（与 schema.sql 一一对应）
    schemas/   Pydantic 出入参模型（API 契约，不要直接暴露 ORM 对象）
    core/      横切能力：配置、数据库连接、日志、异常
    db/        建库与种子数据脚本
"""

__version__ = "0.3.0"
