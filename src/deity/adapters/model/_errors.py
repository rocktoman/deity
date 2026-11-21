"""把 SDK 异常压缩成可安全呈现的诊断摘要（ModelFailure.detail）。

脱敏红线：只取 HTTP 状态码与服务端返回的错误消息文本（SDK 已从响应体
提取的 message 字段）；绝不取请求头、请求体或本地配置（API key）。
摘要做空白折叠并截断，避免服务端文本撑爆界面或借换行伪造界面内容。
"""

_MAX_MESSAGE_CHARS = 200


def status_error_detail(status: int, message: str | None) -> str:
    """APIStatusError 的 detail：`HTTP <状态码>` + 服务端消息安全摘要。"""
    summary = " ".join((message or "").split())[:_MAX_MESSAGE_CHARS]
    if summary:
        return f"HTTP {status}: {summary}"
    return f"HTTP {status}"


def connection_error_detail(exc: BaseException) -> str:
    """连接类错误的 detail：只用异常类型名。

    不取异常消息——传输层消息可能含代理/URL 等本地网络细节，
    类型名（APIConnectionError / APITimeoutError）已足够用户自查方向。
    """
    return type(exc).__name__
