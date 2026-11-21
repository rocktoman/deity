from deity.core.runtime import BudgetExceeded, ModelFailure
from deity.interfaces.cli.chat_commands import run_error_message


def test_run_error_message_appends_model_failure_detail() -> None:
    exc = ModelFailure(
        "model_http",
        retryable=False,
        detail="HTTP 404: The model `deepseek-v4-flash` does not exist",
    )
    assert run_error_message(exc) == (
        "model_http（HTTP 404: The model `deepseek-v4-flash` does not exist）"
    )


def test_run_error_message_without_detail_is_code_only() -> None:
    exc = ModelFailure("model_connection", retryable=True)
    assert run_error_message(exc) == "model_connection"


def test_run_error_message_keeps_budget_guidance() -> None:
    assert "max_turns" in run_error_message(BudgetExceeded("max_turns"))
