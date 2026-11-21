"""工作台纯逻辑单测（Klee 风格 P1）：滚动节流与时间问候语。"""

from deity.interfaces.tui.interactive import ScrollThrottler, greeting_for_hour


class _FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def test_scroll_throttler_merges_within_interval_and_flushes() -> None:
    clock = _FakeClock()
    throttler = ScrollThrottler(interval=0.15, clock=clock)
    assert throttler.request() is True  # 首次立即放行
    clock.now = 0.05
    assert throttler.request() is False  # 间隔内合并
    assert throttler.flush_due() is False  # 未到期不补滚
    clock.now = 0.16
    assert throttler.flush_due() is True  # 到期尾随补滚一次
    assert throttler.flush_due() is False  # 补滚后 pending 已清
    clock.now = 0.20
    assert throttler.request() is False  # 距上次滚动仍不足间隔
    clock.now = 0.40
    assert throttler.request() is True


def test_scroll_throttler_force_resets_window() -> None:
    clock = _FakeClock()
    throttler = ScrollThrottler(interval=0.15, clock=clock)
    assert throttler.request() is True
    clock.now = 0.05
    assert throttler.request() is False
    throttler.force()
    assert throttler.flush_due() is False  # force 清掉 pending


def test_greeting_for_hour_boundaries() -> None:
    assert greeting_for_hour(5) == "早上好"
    assert greeting_for_hour(11) == "早上好"
    assert greeting_for_hour(12) == "下午好"
    assert greeting_for_hour(16) == "下午好"
    assert greeting_for_hour(17) == "晚上好"
    assert greeting_for_hour(21) == "晚上好"
    assert greeting_for_hour(22) == "夜深了"
    assert greeting_for_hour(2) == "夜深了"
