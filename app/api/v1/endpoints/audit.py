"""AI 代码安全审计接口：`POST /api/v1/audit`。

职责边界（见 README「分层约定」）：
    本文件只做三件事 —— 接参（Pydantic 已校验）、调 `ai_service`、把异常映射成状态码；
    审计逻辑、prompt、JSON 清洗全部留在 `app/services/ai_service.py`。

状态码约定（与服务层异常一一对应）：
    AIConfigError    -> 503  未配置 DEEPSEEK_API_KEY，AI 暂不可用（不是客户端的错）
    AIUpstreamError  -> 502  DeepSeek 鉴权/限流/超时/网络异常
    AIResponseError  -> 502  模型输出不可解析 / 漏洞条目全脏 / 无有效结论
    ValueError       -> 422  越过 Pydantic 边界仍不合法（服务层兜底）

错误响应统一结构（前端可据此分流展示）::

    {"detail": {"code": "ai_not_configured", "message": "..."}}
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, status

from app.schemas.audit import AuditRequest, AuditResult
from app.services import ai_service

logger = logging.getLogger(__name__)

# 不带 prefix：完整路径由汇总路由的 /api/v1 前缀 + 这里的 "/audit" 拼成
router = APIRouter(tags=["audit"])


@router.post(
    "/audit",
    response_model=AuditResult,
    status_code=status.HTTP_200_OK,
    summary="审计代码片段中的安全漏洞（DeepSeek）",
    description=(
        "对用户提交的代码做静态安全审计，返回结构化漏洞报告："
        "漏洞名称 + 风险等级（高/中/低）+ 修复建议 + 行号。"
        "模型输出会先经 JSON 解析与 Pydantic 清洗，脏数据不会透传给前端；"
        "`vulnerabilities` 为空表示未发现明显漏洞。"
    ),
    responses={
        # 直接用 422 字面量：Starlette 新版把 HTTP_422_UNPROCESSABLE_ENTITY 标记为
        # 弃用（改名为 ..._CONTENT），字面量可兼容各版本、避免启动时的 deprecation 告警
        422: {"description": "入参不合法（code_snippet 为空或超长）"},
        status.HTTP_502_BAD_GATEWAY: {"description": "AI 上游异常或返回内容不可用"},
        status.HTTP_503_SERVICE_UNAVAILABLE: {"description": "未配置 DEEPSEEK_API_KEY"},
    },
)
def audit_code_snippet(payload: AuditRequest) -> AuditResult:
    """审计代码片段：同步调用 DeepSeek（走 OpenAI 兼容协议）。

    注意：这是**阻塞**接口（大模型首字节较慢，默认超时 60s），
    所以用普通 `def` 交给 FastAPI 线程池执行，不要写成 `async def` 阻塞事件循环。
    """
    try:
        return ai_service.audit_code(
            payload.code_snippet,
            language=payload.language,
        )
    except ai_service.AIConfigError as exc:
        # 服务端自身没配好密钥：503（客户端重试无意义，需要运维/开发补配置）
        logger.warning("AI 代码审计不可用：%s", exc)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"code": exc.code, "message": str(exc)},
        ) from exc
    except ai_service.AIUpstreamError as exc:
        logger.warning("AI 上游调用失败：%s", exc)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail={"code": exc.code, "message": str(exc)},
        ) from exc
    except ai_service.AIResponseError as exc:
        logger.warning("AI 返回内容不可用：%s", exc)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail={"code": exc.code, "message": str(exc)},
        ) from exc
    except ValueError as exc:
        # 理论上被 AuditRequest 拦住了，这里只做兜底，避免 500
        logger.warning("AI 审计入参不合法：%s", exc)
        raise HTTPException(
            status_code=422,
            detail={"code": "invalid_request", "message": str(exc)},
        ) from exc
