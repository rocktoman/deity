"""交互式聊天配置的 CLI 侧入口。

ChatConfig 本体在 application/config.py（随 [chat] 配置段解析）；
本模块只提供 CLI 侧的取值与派生默认值，不另起一套配置。
"""

from pathlib import Path

from deity.application.config import ChatConfig, Config

__all__ = ["ChatConfig", "chat_config_of", "default_history_path"]


def chat_config_of(settings: Config) -> ChatConfig:
    """取配置里的 [chat] 段；旧 Config 对象没有该字段时用默认值。"""
    return getattr(settings, "chat", ChatConfig())


def default_history_path(config: ChatConfig) -> Path:
    """历史文件路径：配置为空时用 ~/.local/share/deity/chat_history。"""
    if config.history_file:
        return Path(config.history_file).expanduser()
    return Path.home() / ".local" / "share" / "deity" / "chat_history"
