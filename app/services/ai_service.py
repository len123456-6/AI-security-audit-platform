"""AI 代码安全审计服务：调用 DeepSeek（OpenAI 兼容协议）审计用户提交的代码片段。

调用链::

    audit_code(代码片段)
        -> 组装 system / user prompt
        -> DeepSeek chat/completions（response_format=json_object 强约束）
        -> json 解析 + Pydantic 清洗校验（脏数据不进业务层）
        -> AuditResult（结构化漏洞报告）

约定：
  - 密钥 / base_url / 模型 / 超时全部来自 `app.core.config`（即 .env 或环境变量），
    代码里不出现任何真实 key，也不允许业务层散落配置；
  - 本模块只负责「静态审计」：不执行用户提交的代码、不发起探测请求，
    也不写库；是否落库由调用方（API 层）决定；
  - 上游异常统一抛 `AIServiceError`，接口层据此映射 503 / 502。

直接自测（需先配好 .env 里的 DEEPSEEK_API_KEY）::

    python -c "from app.services.ai_service import audit_code; \
print(audit_code('db.execute(\"SELECT * FROM users WHERE id=\" + user_id)').model_dump_json(indent=2))"
"""

from __future__ import annotations

import json
import logging
import os
import re
from functools import lru_cache
from typing import Any

from dotenv import load_dotenv
from openai import OpenAI, OpenAIError
from pydantic import AliasChoices, BaseModel, Field, ValidationError

from app.core.config import BASE_DIR, settings
from app.schemas.audit import SEVERITY_RANK, AuditResult, Severity, Vulnerability

logger = logging.getLogger(__name__)

# 单次请求的 token 上限：一份报告通常 3~10 条漏洞，留足空间但不放任模型长篇大论
MAX_OUTPUT_TOKENS = 2000
# 一份报告最多保留多少条漏洞（按风险等级排序后截断），防止模型刷屏
MAX_VULNERABILITIES = 20
# 待审计代码长度上限：与 schemas.audit.AuditRequest.code_snippet 的 max_length 保持一致
MAX_CODE_LENGTH = 8000
# 模型没给 / 给了认不出的风险等级时，按「中」处理（宁可提醒也不放过）
DEFAULT_SEVERITY: Severity = "中"

# 读取项目根目录的 .env，让下面的 os.getenv 兜底在「容器注入环境变量」场景下也能生效；
# 指定 BASE_DIR 是为了不依赖启动时的 cwd。（settings 自己也读 .env，这里只是双保险）
load_dotenv(BASE_DIR / ".env")

# ---------------------------------------------------------------------------
# 异常：接口层只需捕获 AIServiceError，按 code 映射 HTTP 状态码
# ---------------------------------------------------------------------------


class AIServiceError(RuntimeError):
    """AI 服务统一异常基类。"""

    def __init__(self, message: str, *, code: str = "ai_error") -> None:
        super().__init__(message)
        self.code = code


class AIConfigError(AIServiceError):
    """未配置密钥等「服务端自身」问题 -> 503。"""

    def __init__(
        self, message: str = "未配置 DEEPSEEK_API_KEY，AI 代码审计暂不可用"
    ) -> None:
        super().__init__(message, code="ai_not_configured")


class AIUpstreamError(AIServiceError):
    """DeepSeek 调用失败（鉴权、限流、超时、网络）-> 502。"""

    def __init__(self, message: str) -> None:
        super().__init__(message, code="ai_upstream_error")


class AIResponseError(AIServiceError):
    """模型返回内容不可用（非 JSON / 结构不符 / 条目全脏）-> 502。"""

    def __init__(self, message: str) -> None:
        super().__init__(message, code="ai_bad_response")


# ---------------------------------------------------------------------------
# Prompt：要求「纯 JSON」，并固定字段语义，避免每条报告结构都不一样
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = """你是一个资深的白帽子安全专家，请分析用户提供的代码，找出其中的 SQL 注入、XSS、命令执行等安全漏洞，并按 JSON 格式输出漏洞名称、风险等级（高/中/低）和修复建议。

硬性要求：
1. 只输出一个 JSON 对象；不要 markdown 代码块，不要任何解释性前后缀。
2. JSON 结构固定为：
   {"vulnerabilities": [{"name": "漏洞名称", "severity": "高", "suggestion": "修复建议", "line": 12}], "summary": "整体结论"}
3. severity 只能是「高」「中」「低」三者之一：可直接导致数据泄露或服务器被控的为高；
   需要特定前提条件才能利用的为中；信息泄露、配置加固类建议为低。
4. name 用通用漏洞名称（例如「SQL 注入」「反射型 XSS」「命令注入」「路径穿越」），
   不要只写行号或函数名。
5. suggestion 必须可落地：说明该改成什么写法（例如「改用参数化查询 / 预编译语句，
   不要拼接 SQL」），80 字以内。
6. line 是漏洞所在行号（从 1 开始，以用户提供的代码原文为准，包含空行）；
   定位不到就填 null。
7. summary 用 1~2 句话给出整体结论（安全状况 + 最需要优先处理的问题），100 字以内。
8. 只分析用户提供的代码，不要编造代码里不存在的漏洞；确实没有发现问题时返回
   {"vulnerabilities": [], "summary": "未发现明显安全漏洞"}。
9. 只做静态分析：不要执行代码，也不要索要真实密钥、内网地址等敏感信息。"""


def _build_user_prompt(code_snippet: str, *, language: str | None) -> str:
    """把代码塞进 user 消息（system 保持稳定，便于命中 prompt 缓存）。"""
    language_hint = f"，主要语言：{language}" if language else ""
    return (
        f"请审计下面这段代码{language_hint}。行号以代码原文为准（第一行是第 1 行）。\n"
        "```\n"
        f"{code_snippet}\n"
        "```\n"
        "如果完全没有发现漏洞，vulnerabilities 返回空数组。"
    )


# ---------------------------------------------------------------------------
# 客户端：惰性创建 + 进程内复用（内部维护连接池，别每次请求 new 一个）
# ---------------------------------------------------------------------------


def resolve_api_key() -> str:
    """读取 DeepSeek 密钥。

    `Settings` 已经从 `.env` / 环境变量加载了配置；这里再用 `os.getenv` 兜底，
    是为了兼容「进程启动后由容器/CI 注入环境变量」而配置单例已缓存的情况。
    两处都是读环境，不存在硬编码，也不会把 key 打进日志或终端。
    """
    return (settings.deepseek_api_key or os.getenv("DEEPSEEK_API_KEY", "")).strip()


@lru_cache(maxsize=1)
def get_client() -> OpenAI:
    """构造指向 DeepSeek 的 OpenAI 客户端（OpenAI 兼容协议，只换 base_url）。

    测试里改了 key / 需要重建客户端时调用 `get_client.cache_clear()`。
    """
    api_key = resolve_api_key()
    if not api_key:
        raise AIConfigError()

    return OpenAI(
        api_key=api_key,
        base_url=settings.deepseek_base_url,
        timeout=settings.deepseek_timeout_seconds,
        max_retries=2,  # 限流 / 抖动时自动重试，仍失败才抛给上层
    )


# ---------------------------------------------------------------------------
# 响应解析：模型可能包代码块、塞多余文本、换字段名、返回脏条目，这里逐层清洗
# ---------------------------------------------------------------------------


class _RawVulnerability(BaseModel):
    """模型返回的单条漏洞（宽松校验，脏条目在下面统一过滤）。

    `validation_alias` 兼容模型常见的同义字段名（fix / issues 之类），
    免得因为一个 key 写法不同就把整份报告判定为「结构不符」。
    """

    name: str = Field(
        default="",
        validation_alias=AliasChoices("name", "title", "type", "vulnerability"),
    )
    severity: str = Field(
        default="",
        validation_alias=AliasChoices("severity", "risk", "level"),
    )
    suggestion: str = Field(
        default="",
        validation_alias=AliasChoices("suggestion", "fix", "remediation", "advice"),
    )
    line: Any = Field(
        default=None,
        validation_alias=AliasChoices("line", "line_number", "lineno"),
    )


class _RawPayload(BaseModel):
    """模型返回的 JSON 顶层结构。"""

    vulnerabilities: list[_RawVulnerability] = Field(
        default_factory=list,
        validation_alias=AliasChoices(
            "vulnerabilities", "findings", "issues", "vulnerability_list"
        ),
    )
    summary: str | None = Field(
        default=None,
        validation_alias=AliasChoices("summary", "conclusion", "overview"),
    )


# 模型可能返回「高危 / HIGH / 严重 / critical」等写法，统一收敛到「高/中/低」
_SEVERITY_ALIASES: dict[str, Severity] = {
    "高": "高",
    "高危": "高",
    "严重": "高",
    "极高": "高",
    "high": "高",
    "critical": "高",
    "severe": "高",
    "中": "中",
    "中危": "中",
    "中等": "中",
    "medium": "中",
    "moderate": "中",
    "低": "低",
    "低危": "低",
    "较低": "低",
    "low": "低",
    "info": "低",
    "提示": "低",
}


def _normalize_severity(raw: str) -> Severity:
    """把模型给的风险等级收敛成「高/中/低」；认不出时按「中」处理。"""
    key = (raw or "").strip().lower()
    severity = _SEVERITY_ALIASES.get(key)
    if severity is None:
        if key:
            logger.warning("无法识别的风险等级 %r，按「%s」处理", raw, DEFAULT_SEVERITY)
        return DEFAULT_SEVERITY
    return severity


def _extract_json_text(raw: str) -> str:
    """去掉 markdown 代码块与 JSON 前后的多余文本。"""
    text = (raw or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```[A-Za-z]*\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    text = text.strip()

    # 兜底：模型偶尔会加「好的，以下是审计结果：」这类前缀
    # 顶层可能是对象（{"vulnerabilities": ...}）也可能是数组（[{...}, {...}]），两种都要认
    starts = [index for index in (text.find("{"), text.find("[")) if index != -1]
    if starts:
        start = min(starts)
        closer = "}" if text[start] == "{" else "]"
        end = text.rfind(closer)
        if end > start:
            text = text[start : end + 1]
    return text.strip()


def parse_audit_report(
    raw: str,
    *,
    max_vulnerabilities: int = MAX_VULNERABILITIES,
) -> AuditResult:
    """把模型原始输出解析成 `AuditResult`（纯函数，便于单测）。

    清洗规则：
      - 代码块 / 前后缀文本先剥掉；
      - 顶层直接返回数组时，按「漏洞列表」处理；
      - 名称或建议为空的条目丢弃，同名同行的重复条目只留一条；
      - 风险等级归一化后按「高 > 中 > 低」排序，并截断到 max_vulnerabilities。

    Raises:
        AIResponseError: 非 JSON、结构不符、条目全脏、既无漏洞也无结论。
    """
    try:
        data = json.loads(_extract_json_text(raw))
    except json.JSONDecodeError as exc:
        logger.warning("AI 返回内容不是合法 JSON: %r", raw[:500])
        raise AIResponseError("AI 返回的不是合法 JSON") from exc

    if isinstance(data, list):
        # 模型有时只返回一个漏洞数组，补成顶层结构再校验
        data = {"vulnerabilities": data}

    try:
        payload = _RawPayload.model_validate(data)
    except ValidationError as exc:
        logger.warning("AI 返回结构不符合约定: %s", exc)
        raise AIResponseError("AI 返回的数据结构不符合约定") from exc

    summary = (payload.summary or "").strip()[:500] or None

    vulnerabilities: list[Vulnerability] = []
    seen: set[tuple[str, int | None]] = set()
    for item in payload.vulnerabilities:
        name, suggestion = item.name.strip(), item.suggestion.strip()
        if not name or not suggestion:
            continue  # 空名称 / 空建议直接丢弃，不污染报告

        severity = _normalize_severity(item.severity)
        try:
            vulnerability = Vulnerability(
                name=name[:80], severity=severity, suggestion=suggestion[:1000],
                line=item.line,
            )
        except ValidationError as exc:
            # 行号是脏数据（如「未知」）时只丢行号，保留漏洞本身，别整条丢弃
            logger.debug("漏洞条目字段不合法，忽略行号后重试: %s", exc)
            vulnerability = Vulnerability(
                name=name[:80], severity=severity, suggestion=suggestion[:1000]
            )

        dedup_key = (vulnerability.name, vulnerability.line)
        if dedup_key in seen:
            continue
        seen.add(dedup_key)
        vulnerabilities.append(vulnerability)

    if payload.vulnerabilities and not vulnerabilities:
        logger.warning("AI 返回的漏洞条目全部无效: %r", raw[:500])
        raise AIResponseError("AI 返回的漏洞条目均缺少名称或修复建议")

    if not vulnerabilities and summary is None:
        # 既没有漏洞也没有结论 —— 说不清是「代码干净」还是「模型糊弄」
        logger.warning("AI 返回中既无漏洞列表也无结论: %r", raw[:500])
        raise AIResponseError("AI 未返回有效的审计结论")

    vulnerabilities.sort(key=lambda item: SEVERITY_RANK[item.severity], reverse=True)
    return AuditResult(
        vulnerabilities=vulnerabilities[:max_vulnerabilities], summary=summary
    )


# ---------------------------------------------------------------------------
# 对外入口
# ---------------------------------------------------------------------------


def audit_code(
    code_snippet: str,
    *,
    language: str | None = None,
    max_vulnerabilities: int = MAX_VULNERABILITIES,
) -> AuditResult:
    """审计一段代码，返回结构化的漏洞报告。

    Args:
        code_snippet: 待审计的源码片段（保留原始缩进与换行，模型报的行号依赖它）。
        language: 语言提示（可选），如 python / java / php，帮助模型选对审计规则。
        max_vulnerabilities: 报告中保留的漏洞条数上限（1~20）。

    Returns:
        AuditResult: 漏洞列表（按风险等级从高到低）+ 整体结论 + 免责声明；
        列表为空表示未发现明显漏洞。

    Raises:
        ValueError: 入参不合法（接口层由 Pydantic 先行拦截）。
        AIConfigError: 未配置 DEEPSEEK_API_KEY。
        AIUpstreamError: DeepSeek 调用失败。
        AIResponseError: 模型输出不可解析 / 条目全脏 / 无有效结论。
    """
    code_snippet = (code_snippet or "").strip("\n")
    if not code_snippet.strip():
        raise ValueError("待审计代码不能为空")
    if len(code_snippet) > MAX_CODE_LENGTH:
        raise ValueError(f"待审计代码长度不能超过 {MAX_CODE_LENGTH} 字符")
    if not 1 <= max_vulnerabilities <= MAX_VULNERABILITIES:
        raise ValueError(f"max_vulnerabilities 必须在 1~{MAX_VULNERABILITIES} 之间")

    language = (language or "").strip().lower() or None

    client = get_client()
    logger.info(
        "请求 DeepSeek 代码审计：chars=%s language=%r model=%s",
        len(code_snippet),
        language,
        settings.deepseek_model,
    )

    try:
        completion = client.chat.completions.create(
            model=settings.deepseek_model,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {
                    "role": "user",
                    "content": _build_user_prompt(code_snippet, language=language),
                },
            ],
            temperature=0.1,  # 审计要稳定可复现，不让模型发散
            max_tokens=MAX_OUTPUT_TOKENS,
            response_format={"type": "json_object"},  # DeepSeek 支持，保证纯 JSON
        )
    except OpenAIError as exc:
        # 鉴权失败 / 限流 / 超时 / 网络异常都在这里，细节只进日志不外泄
        logger.exception("调用 DeepSeek 失败")
        raise AIUpstreamError(f"调用 AI 服务失败：{type(exc).__name__}") from exc

    if not completion.choices:
        raise AIResponseError("AI 未返回任何结果")

    content = completion.choices[0].message.content or ""
    result = parse_audit_report(content, max_vulnerabilities=max_vulnerabilities)
    logger.info(
        "AI 审计完成：发现 %s 个漏洞（%s）",
        len(result.vulnerabilities),
        [item.severity for item in result.vulnerabilities],
    )
    return result
