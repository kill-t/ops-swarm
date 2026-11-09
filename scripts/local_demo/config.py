"""生成可重建的脚本模型配置与私下保存的独立演示身份。"""

import hashlib
import json
import secrets
from datetime import UTC, datetime, timedelta
from pathlib import Path

from opsswarm.identity.authentication import TOKEN_PATTERN

ROOT = Path(__file__).resolve().parents[2]
ARTIFACT_ROOT = ROOT / ".pytest_cache" / "local-demo"
PROJECT = "opsswarm-local-demo"
MODEL = "opsswarm-script-demo"
PROFILE = "local-demo-v1"
ORIGIN = "http://127.0.0.1:35173"
ORDER = "http://127.0.0.1:38001"
CHANGE = "http://127.0.0.1:38002"
ROLES = ("engineer", "approver", "executor", "alert_source")


def directory(value: str | Path | None = None) -> Path:
    path = (Path(value) if value is not None else ARTIFACT_ROOT).resolve()
    if not path.is_relative_to(ARTIFACT_ROOT.resolve()):
        raise ValueError("演示文件必须保存在仓库 .pytest_cache/local-demo 下")
    return path


def read_config(path: Path) -> dict:
    path = directory(path)
    if not (path / "demo.env").is_file():
        raise ValueError("演示环境文件缺失，请使用新的演示子目录")
    data = json.loads((path / "config.json").read_text(encoding="utf-8"))
    tokens = data.get("tokens") if isinstance(data, dict) else None
    if not isinstance(tokens, dict) or data.get("version") != PROFILE or set(tokens) != set(ROLES):
        raise ValueError("演示配置版本或角色不完整")
    if datetime.fromisoformat(data["expires_at"]) <= datetime.now(UTC):
        raise ValueError("演示身份已过期，请在新的演示子目录重新准备并启动")
    for role, token in tokens.items():
        prefix = f"demo-{role}."
        if not isinstance(token, str):
            raise ValueError("演示令牌格式无效")
        # 旧配置保存的是秘密部分，原凭据 ID 与摘要仍可直接复用。
        token = token if "." in token else prefix + token
        if not token.startswith(prefix) or TOKEN_PATTERN.fullmatch(token) is None:
            raise ValueError("演示令牌格式无效")
        tokens[role] = token
    return data


def prepare(path: Path) -> Path:
    path = directory(path)
    if (path / "config.json").exists():
        read_config(path)
        return path / "demo.env"
    if (path / "demo.env").exists():
        raise ValueError("发现未完成的演示配置，请使用新的演示子目录")
    path.mkdir(parents=True, exist_ok=True)
    expires = (datetime.now(UTC) + timedelta(hours=8)).isoformat()
    tokens = {role: f"demo-{role}.{secrets.token_urlsafe(32)}" for role in ROLES}
    credentials = [
        dict(
            credential_id=f"demo-{role}",
            subject_id=f"demo-{role}",
            role=role,
            secret_sha256=hashlib.sha256(tokens[role].split(".", 1)[1].encode()).hexdigest(),
            expires_at=expires,
        )
        for role in ROLES
    ]
    settings = {
        "OPSSWARM_OPERATOR_AUTH_MODE": "bearer",
        "OPSSWARM_OPERATOR_AUTH_CREDENTIALS": json.dumps(credentials, separators=(",", ":")),
        "LLM_API_KEY": "script-fixture-only",
        "LLM_BASE_URL": "http://demo-model:8000/v1",
        "LLM_MODEL": MODEL,
        "LLM_STRUCTURED_OUTPUT_METHOD": "function_calling",
        "LLM_BUDGET_PROFILE_ID": PROFILE,
        "LLM_CONTEXT_WINDOW_TOKENS": "65536",
        "LLM_INPUT_COST_PER_MILLION_USD": "1",
        "LLM_OUTPUT_COST_PER_MILLION_USD": "2",
    }
    for name, content in (
        ("demo.env", "".join(f"{key}='{value}'\n" for key, value in settings.items())),
        ("config.json", json.dumps(dict(version=PROFILE, expires_at=expires, tokens=tokens))),
    ):
        with (path / name).open("x", encoding="utf-8") as file:
            file.write(content)
        (path / name).chmod(0o600)
    return path / "demo.env"


def model_budget() -> dict:
    return dict(
        version="model-budget-v1",
        profile=dict(
            profile_id=PROFILE,
            model_name=MODEL,
            context_window_tokens=65536,
            structured_output_method="function_calling",
            pricing=dict(input_cost_per_million_usd="1", output_cost_per_million_usd="2"),
        ),
        max_input_tokens_per_call=48000,
        max_output_tokens_per_call=1024,
        safety_margin_tokens=1024,
        max_input_tokens=500000,
        max_output_tokens=24000,
        max_estimated_cost_usd="1",
    )
