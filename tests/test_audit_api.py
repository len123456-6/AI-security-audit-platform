"""审计接口冒烟测试：路由挂载、入参校验、异常到状态码的映射。

不联网：`ai_service.audit_code` 全部 monkeypatch 掉，
所以不依赖 .env 里的真实密钥，也不会消耗额度。

跑法::

    python -m pytest tests/test_audit_api.py
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.schemas.audit import DISCLAIMER, AuditResult, Vulnerability
from app.services import ai_service

ENDPOINT = "/api/v1/audit"

VULNERABLE_CODE = """user_id = request.args.get("id")
sql = "SELECT * FROM users WHERE id = " + user_id
cursor.execute(sql)
"""


@pytest.fixture(scope="module")
def client() -> TestClient:
    """with 语句触发 lifespan（启动建表），与 test_health.py 保持一致。"""
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture(autouse=True)
def _restore_service() -> object:
    """用 monkeypatch 的用例无需清理；这里只确保 ai_service.get_client 缓存不串场。"""
    ai_service.get_client.cache_clear()
    yield
    ai_service.get_client.cache_clear()


def test_audit_returns_structured_report(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """正常路径：服务层结果原样成为响应体，代码原文与语言提示正确透传。"""
    captured: dict[str, object] = {}

    def _fake_audit(
        code_snippet: str, *, language: str | None = None
    ) -> AuditResult:
        captured.update(code_snippet=code_snippet, language=language)
        return AuditResult(
            vulnerabilities=[
                Vulnerability(
                    name="SQL 注入", severity="高", suggestion="改用参数化查询", line=2
                ),
                Vulnerability(
                    name="缺少异常处理", severity="低", suggestion="补齐 try/except"
                ),
            ],
            summary="存在高风险 SQL 注入，需优先修复。",
        )

    monkeypatch.setattr(ai_service, "audit_code", _fake_audit)

    response = client.post(
        ENDPOINT, json={"code_snippet": VULNERABLE_CODE, "language": " Python "}
    )

    assert response.status_code == 200
    body = response.json()
    assert [item["name"] for item in body["vulnerabilities"]] == ["SQL 注入", "缺少异常处理"]
    assert [item["severity"] for item in body["vulnerabilities"]] == ["高", "低"]
    assert body["vulnerabilities"][0]["line"] == 2
    assert body["summary"] == "存在高风险 SQL 注入，需优先修复。"
    assert body["disclaimer"] == DISCLAIMER

    # 代码原文（缩进/换行）必须完整透传，否则模型报的行号会对不上
    assert captured["code_snippet"] == VULNERABLE_CODE
    assert captured["language"] == "python"  # 首尾空白已清洗、统一小写


def test_audit_defaults_language_to_none(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """不传 language 时为 None，不会往 prompt 里塞空的语言提示。"""
    captured: dict[str, object] = {}

    def _fake_audit(code_snippet: str, *, language: str | None = None) -> AuditResult:
        captured.update(language=language)
        return AuditResult(vulnerabilities=[], summary="未发现明显安全漏洞")

    monkeypatch.setattr(ai_service, "audit_code", _fake_audit)

    assert client.post(ENDPOINT, json={"code_snippet": "print(1)"}).status_code == 200
    assert captured["language"] is None


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"code_snippet": ""},
        {"code_snippet": "   "},
        {"code_snippet": "a" * 8001},
        {"code_snippet": "print(1)", "language": "a" * 33},
    ],
)
def test_audit_rejects_invalid_payload(
    client: TestClient, payload: dict[str, object]
) -> None:
    """入参边界由 Pydantic 拦住，返回 422 且不触碰 AI 服务。"""
    assert client.post(ENDPOINT, json=payload).status_code == 422


def test_audit_without_api_key_returns_503(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """未配置密钥 -> 503 + 可读错误码，而不是 500。"""

    def _raise_config_error(*_: object, **__: object) -> None:
        raise ai_service.AIConfigError()

    monkeypatch.setattr(ai_service, "audit_code", _raise_config_error)

    response = client.post(ENDPOINT, json={"code_snippet": VULNERABLE_CODE})

    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "ai_not_configured"


def test_audit_upstream_error_returns_502(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """DeepSeek 调用失败 -> 502，且不把上游细节泄露给前端。"""

    def _raise_upstream(*_: object, **__: object) -> None:
        raise ai_service.AIUpstreamError("调用 AI 服务失败：APIConnectionError")

    monkeypatch.setattr(ai_service, "audit_code", _raise_upstream)

    response = client.post(ENDPOINT, json={"code_snippet": VULNERABLE_CODE})

    assert response.status_code == 502
    assert response.json()["detail"]["code"] == "ai_upstream_error"


def test_audit_bad_model_output_returns_502(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """模型输出不可解析 -> 502 ai_bad_response。"""

    def _raise_bad_response(*_: object, **__: object) -> None:
        raise ai_service.AIResponseError("AI 未返回有效的审计结论")

    monkeypatch.setattr(ai_service, "audit_code", _raise_bad_response)

    response = client.post(ENDPOINT, json={"code_snippet": VULNERABLE_CODE})

    assert response.status_code == 502
    assert response.json()["detail"]["code"] == "ai_bad_response"


def test_openapi_contains_audit_endpoint(client: TestClient) -> None:
    """路由确实挂在 /api/v1 前缀下，文档里能看到（浏览器 /docs 可测）。"""
    schema = client.get("/openapi.json").json()

    assert ENDPOINT in schema["paths"]
    assert "/api/v1/recommend" not in schema["paths"]  # 旧路由已下线
    request_schema = schema["paths"][ENDPOINT]["post"]["requestBody"]["content"][
        "application/json"
    ]["schema"]
    assert request_schema["$ref"].endswith("/AuditRequest")
    assert schema["paths"][ENDPOINT]["post"]["tags"] == ["audit"]
    assert schema["info"]["title"] == "AI 代码安全审计平台"
