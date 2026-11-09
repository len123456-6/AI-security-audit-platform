"""ai_service 单测：不联网、不消耗额度，用假客户端验证「调用参数 + 输出清洗」。

真实调用 DeepSeek 需要 .env 里配置 DEEPSEEK_API_KEY，因此这里全部用 mock：
  - parse_audit_report：纯函数，覆盖代码块包裹、脏条目、等级归一化、空结论、非 JSON；
  - audit_code：验证模型名、response_format、prompt 内容是否正确透传，
    以及未配密钥 / 上游异常时抛出的异常类型。
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import pytest
from openai import OpenAIError

from app.core.config import settings
from app.services import ai_service

# 一段「明显有洞」的示例代码：用于验证 prompt 透传与行号约定
VULNERABLE_CODE = """user_id = request.args.get("id")
sql = "SELECT * FROM users WHERE id = " + user_id
cursor.execute(sql)
os.system("ping " + request.args.get("host"))
"""

VALID_CONTENT = json.dumps(
    {
        "vulnerabilities": [
            {
                "name": "命令注入",
                "severity": "严重",
                "suggestion": "改用 subprocess 列表参数，禁止拼接 shell 字符串",
                "line": 4,
            },
            {
                "name": "SQL 注入",
                "severity": "high",
                "suggestion": "改用参数化查询 cursor.execute(sql, (user_id,))",
                "line": 3,
            },
        ],
        "summary": "存在两处高风险漏洞，优先修复 SQL 注入。",
    },
    ensure_ascii=False,
)


class _FakeCompletions:
    """记录调用参数，返回预设内容（模拟 client.chat.completions）。"""

    def __init__(self, content: str) -> None:
        self.content = content
        self.calls: list[dict[str, Any]] = []

    def create(self, **kwargs: Any) -> SimpleNamespace:
        self.calls.append(kwargs)
        message = SimpleNamespace(content=self.content)
        return SimpleNamespace(choices=[SimpleNamespace(message=message)])


class _FakeClient:
    def __init__(self, content: str) -> None:
        self.chat = SimpleNamespace(completions=_FakeCompletions(content))


@pytest.fixture(autouse=True)
def _reset_client_cache() -> Any:
    """get_client 带 lru_cache，用例之间必须清掉，避免 key 串场。"""
    ai_service.get_client.cache_clear()
    yield
    ai_service.get_client.cache_clear()


def test_parse_audit_report_cleans_code_fence_and_dirty_items() -> None:
    """模型常见的「代码块包裹 + 重复项 + 空字段 + 等级写法不统一」都要清洗。"""
    raw = """```json
    {"vulnerabilities": [
        {"name": "SQL 注入", "severity": "高危", "suggestion": "改用参数化查询", "line": 3},
        {"name": "SQL 注入", "severity": "高危", "suggestion": "重复项应被丢弃", "line": 3},
        {"name": "   ", "severity": "高", "suggestion": "空名称应被丢弃"},
        {"name": "反射型 XSS", "severity": "中等", "suggestion": "输出时做 HTML 转义", "line": "第 7 行"},
        {"name": "调试接口未鉴权", "severity": "low", "suggestion": "上线前关闭调试路由"}
    ], "summary": "优先修复 SQL 注入。"}
    ```"""

    result = ai_service.parse_audit_report(raw, max_vulnerabilities=5)

    assert [item.name for item in result.vulnerabilities] == [
        "SQL 注入",
        "反射型 XSS",
        "调试接口未鉴权",
    ]
    # 等级被归一化成「高/中/低」，并按风险从高到低排序
    assert [item.severity for item in result.vulnerabilities] == ["高", "中", "低"]
    assert result.vulnerabilities[1].line == 7  # "第 7 行" -> 7
    assert result.vulnerabilities[2].line is None
    assert result.summary == "优先修复 SQL 注入。"
    assert result.disclaimer  # 审计结论必须带免责声明


def test_parse_audit_report_respects_max_vulnerabilities() -> None:
    """按等级排序后截断：最多保留 max_vulnerabilities 条。"""
    raw = json.dumps(
        {
            "vulnerabilities": [
                {"name": "低危问题", "severity": "低", "suggestion": "加固"},
                {"name": "高危问题", "severity": "高", "suggestion": "立即修复"},
            ]
        },
        ensure_ascii=False,
    )

    result = ai_service.parse_audit_report(raw, max_vulnerabilities=1)

    assert [item.name for item in result.vulnerabilities] == ["高危问题"]


def test_parse_audit_report_accepts_clean_code() -> None:
    """「没扫出问题」是合法结论：空列表 + 结论不应被当成错误。"""
    result = ai_service.parse_audit_report(
        '{"vulnerabilities": [], "summary": "未发现明显安全漏洞"}'
    )

    assert result.vulnerabilities == []
    assert result.summary == "未发现明显安全漏洞"


def test_parse_audit_report_accepts_top_level_array() -> None:
    """模型有时只返回漏洞数组，也要能解析。"""
    raw = json.dumps(
        [{"name": "命令注入", "severity": "高", "suggestion": "避免拼接 shell 命令"}],
        ensure_ascii=False,
    )

    result = ai_service.parse_audit_report(raw)

    assert [item.name for item in result.vulnerabilities] == ["命令注入"]


def test_parse_audit_report_rejects_non_json() -> None:
    with pytest.raises(ai_service.AIResponseError):
        ai_service.parse_audit_report("抱歉，我无法分析这段代码。")


def test_parse_audit_report_rejects_all_dirty_items() -> None:
    """有漏洞字段但全部缺名称 / 缺建议 -> 不能输出「没有漏洞」的假结论。"""
    with pytest.raises(ai_service.AIResponseError):
        ai_service.parse_audit_report(
            '{"vulnerabilities": [{"name": "", "severity": "高", "suggestion": ""}]}'
        )


def test_parse_audit_report_rejects_empty_payload() -> None:
    """既没有漏洞也没有结论 -> 视为模型糊弄，直接报错。"""
    with pytest.raises(ai_service.AIResponseError):
        ai_service.parse_audit_report("{}")


def test_audit_code_passes_params_to_deepseek(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _FakeClient(VALID_CONTENT)
    monkeypatch.setattr(ai_service, "get_client", lambda: fake)
    monkeypatch.setattr(settings, "deepseek_api_key", "sk-test-only")

    result = ai_service.audit_code(VULNERABLE_CODE, language=" Python ")

    assert [item.name for item in result.vulnerabilities] == ["命令注入", "SQL 注入"]
    assert [item.severity for item in result.vulnerabilities] == ["高", "高"]

    call = fake.chat.completions.calls[0]
    assert call["model"] == settings.deepseek_model
    assert call["response_format"] == {"type": "json_object"}
    assert call["messages"][0]["content"] == ai_service.SYSTEM_PROMPT
    assert "白帽子安全专家" in call["messages"][0]["content"]

    user_prompt = call["messages"][1]["content"]
    assert "主要语言：python" in user_prompt  # 语言提示被归一化成小写
    assert 'sql = "SELECT * FROM users WHERE id = " + user_id' in user_prompt


def test_audit_code_omits_language_hint_when_not_given(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _FakeClient(VALID_CONTENT)
    monkeypatch.setattr(ai_service, "get_client", lambda: fake)
    monkeypatch.setattr(settings, "deepseek_api_key", "sk-test-only")

    ai_service.audit_code(VULNERABLE_CODE, language="  ")

    user_prompt = fake.chat.completions.calls[0]["messages"][1]["content"]
    assert "主要语言" not in user_prompt


def test_audit_code_rejects_blank_code() -> None:
    with pytest.raises(ValueError):
        ai_service.audit_code("   \n  ")


def test_audit_code_rejects_too_long_code() -> None:
    with pytest.raises(ValueError):
        ai_service.audit_code("a" * (ai_service.MAX_CODE_LENGTH + 1))


def test_audit_code_requires_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "deepseek_api_key", "")
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)

    with pytest.raises(ai_service.AIConfigError) as excinfo:
        ai_service.audit_code(VULNERABLE_CODE)

    assert excinfo.value.code == "ai_not_configured"


def test_audit_code_wraps_upstream_error(monkeypatch: pytest.MonkeyPatch) -> None:
    def _raise(**_: Any) -> None:
        raise OpenAIError("connection reset")

    client = SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=_raise))
    )
    monkeypatch.setattr(ai_service, "get_client", lambda: client)
    monkeypatch.setattr(settings, "deepseek_api_key", "sk-test-only")

    with pytest.raises(ai_service.AIUpstreamError) as excinfo:
        ai_service.audit_code(VULNERABLE_CODE)

    assert excinfo.value.code == "ai_upstream_error"
