"""AI 代码安全审计相关的 Pydantic 模型（API 契约层）。

`AuditRequest` 是 `POST /api/v1/audit` 的请求体；
`Vulnerability` / `AuditResult` 既是 `ai_service` 的返回类型，
也是该接口的响应结构，保证「服务产物 = 接口出参」不走样。

命名约定（与项目一致）：
    入参：AuditRequest
    出参：AuditResult / Vulnerability

设计要点：
    - 风险等级只允许「高 / 中 / 低」三档：模型常返回「高危 / HIGH / 严重」等变体，
      由服务层（ai_service._normalize_severity）归一化后再进模型；
    - 漏洞列表允许为空 ——「没扫出问题」是合法的审计结论，
      不像菜谱那样必须有内容；
    - 字段长度上限按「大模型输出」的实际情况设定，服务层会先截断再构造，
      因此这里的约束只是最后一道防线。
"""

from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, Field, field_validator

DISCLAIMER = "AI 审计结果仅供参考，可能存在漏报或误报，请务必结合人工复核后再修复上线。"

# 风险等级：只允许这三档，避免「高危 / critical / 严重」等写法污染前端
Severity = Literal["高", "中", "低"]

# 排序权重：高 > 中 > 低，服务层用它把最危险的问题排到最前面
SEVERITY_RANK: dict[str, int] = {"高": 3, "中": 2, "低": 1}


class AuditRequest(BaseModel):
    """审计接口入参：`POST /api/v1/audit` 的请求体。

    长度边界（1~8000 字符）与 `ai_service.MAX_CODE_LENGTH` 保持一致：
    接口层 Pydantic 先拦一道，服务层再兜底一道，避免超长代码打到模型。
    """

    code_snippet: str = Field(
        ...,
        min_length=1,
        max_length=8000,
        description="待审计的源码片段，保留原始缩进与换行（模型报的行号依赖它）",
        examples=["cursor.execute(f\"SELECT * FROM users WHERE id={user_id}\")"],
    )
    language: str | None = Field(
        default=None,
        max_length=32,
        description="代码语言提示（可选），如 python / java / php，用于让模型选对审计规则",
        examples=["python"],
    )

    @field_validator("code_snippet")
    @classmethod
    def _validate_code_snippet(cls, value: str) -> str:
        """只校验「不能是空白」，**不做 strip**。

        代码的缩进与空行是有意义的信息，一旦去掉首行缩进或前导空行，
        模型报出的行号就对不上用户粘贴的原文了。
        """
        if not value.strip():
            raise ValueError("待审计代码不能为空白字符")
        return value

    @field_validator("language")
    @classmethod
    def _normalize_language(cls, value: str | None) -> str | None:
        """语言是可选的：空串 / 纯空白统一成 None，避免 prompt 里出现「语言：」。"""
        if value is None:
            return None
        stripped = value.strip().lower()
        return stripped or None


class Vulnerability(BaseModel):
    """单条漏洞：漏洞名称 + 风险等级 + 修复建议（+ 可选行号）。"""

    name: str = Field(
        ...,
        min_length=1,
        max_length=80,
        description="漏洞名称，如「SQL 注入」「反射型 XSS」「命令注入」",
    )
    severity: Severity = Field(
        ...,
        description="风险等级，只能是「高」「中」「低」",
    )
    suggestion: str = Field(
        ...,
        min_length=1,
        max_length=1000,
        description="可落地的修复建议（应该改成什么写法）",
    )
    line: int | None = Field(
        default=None,
        ge=1,
        description="漏洞所在行号（从 1 开始，基于用户提交的原文）；模型定位不到时为 null",
    )

    @field_validator("name", "suggestion")
    @classmethod
    def _strip_text(cls, value: str) -> str:
        """去掉首尾空白；纯空白视为空，直接判为非法条目。"""
        stripped = value.strip()
        if not stripped:
            raise ValueError("漏洞名称与修复建议不能为空白字符")
        return stripped

    @field_validator("line", mode="before")
    @classmethod
    def _coerce_line(cls, value: object) -> int | None:
        """模型可能返回 "第 12 行" / "12-15" / 0 / null，这里统一收敛成行号或 None。"""
        if value is None or isinstance(value, bool):
            return None
        if isinstance(value, int):
            return value if value >= 1 else None
        if isinstance(value, float):
            return int(value) if value >= 1 else None
        if isinstance(value, str):
            match = re.search(r"\d+", value)
            if match is None:
                return None
            number = int(match.group())
            return number if number >= 1 else None
        return None


class AuditResult(BaseModel):
    """一次审计请求的完整结果。

    注意 `vulnerabilities` 允许为空列表 —— 空表示「未发现明显安全问题」，
    这是合法结论（与推荐菜谱不同，不能当成「模型没干活」）。
    """

    vulnerabilities: list[Vulnerability] = Field(
        default_factory=list,
        description="漏洞列表，按风险等级从高到低排序；空列表表示未发现明显漏洞",
    )
    summary: str | None = Field(
        default=None,
        max_length=500,
        description="整体结论（可选，1~2 句话）",
    )
    disclaimer: str = Field(default=DISCLAIMER, description="固定免责声明")
