"""deity init：生成最小可用配置。

密钥纪律不变：配置文件里只写密钥所在的环境变量名，绝不写密钥值本身。
默认值面向 V1.1 的默认厂商 Qwen（DashScope OpenAI 兼容端点）。
"""

import json
from pathlib import Path

from deity.application.config import (
    QWEN_DEFAULT_API_KEY_ENV,
    QWEN_DEFAULT_BASE_URL,
    QWEN_DEFAULT_MODEL,
)

PROVIDER_DEFAULTS: dict[str, tuple[str, str, str | None]] = {
    "qwen": (QWEN_DEFAULT_MODEL, QWEN_DEFAULT_API_KEY_ENV, QWEN_DEFAULT_BASE_URL),
    "openai_compatible": ("", "OPENAI_API_KEY", None),
    "anthropic": ("", "ANTHROPIC_API_KEY", None),
}


def build_init_config(
    provider: str,
    *,
    model: str | None = None,
    api_key_env: str | None = None,
    base_url: str | None = None,
) -> str:
    """Render the minimal config.toml text for one provider.

    Raises ValueError for an unknown provider, or when a provider without a
    default model is requested without an explicit model name.
    """
    if provider not in PROVIDER_DEFAULTS:
        raise ValueError(f"未知厂商：{provider}（可选：{', '.join(PROVIDER_DEFAULTS)}）")
    default_model, default_env, default_base_url = PROVIDER_DEFAULTS[provider]
    resolved_model = model or default_model
    if not resolved_model:
        raise ValueError(f"厂商 {provider} 没有默认模型，必须用 --model 指定模型名")
    resolved_env = api_key_env or default_env
    resolved_base_url = base_url if base_url is not None else default_base_url
    lines = [
        "version = 1",
        "",
        "[model]",
        f"provider = {json.dumps(provider)}",
        f"name = {json.dumps(resolved_model)}",
        f"api_key_env = {json.dumps(resolved_env)}",
    ]
    if resolved_base_url:
        lines.append(f"base_url = {json.dumps(resolved_base_url)}")
    return "\n".join(lines) + "\n"


def write_init_config(path: Path, text: str, *, force: bool = False) -> None:
    """Write the config, refusing to overwrite an existing file unless forced."""
    if path.exists() and not force:
        raise FileExistsError(f"配置已存在：{path}（确认覆盖请加 --force）")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


__all__ = ["PROVIDER_DEFAULTS", "build_init_config", "write_init_config"]
