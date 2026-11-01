"""演示可复用的稳定排序函数。"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from typing import Any


def sort_items[T](
    items: Iterable[T], *, key: Callable[[T], Any] | None = None, reverse: bool = False
) -> list[T]:
    """返回稳定排序的新列表；无法比较的元素会抛出 TypeError。"""
    effective_key: Callable[[T], Any] = key if key is not None else lambda item: item
    return sorted(items, key=effective_key, reverse=reverse)


def quick_sort[T](
    items: Iterable[T], *, key: Callable[[T], Any] | None = None, reverse: bool = False
) -> list[T]:
    """返回稳定快速排序后的新列表；无法比较的元素会抛出 TypeError。

    语义与 ``sort_items`` 对齐：不修改入参、返回新列表、按 ``key`` 比较、稳定排序。
    实现采用三路分区（小于 / 等于 / 大于枢轴）配合显式栈迭代，不依赖递归，
    因此大输入不会触发 ``RecursionError``；三个分区按原顺序写回，故相等元素
    保持输入顺序（``reverse=True`` 时同样稳定）。
    平均时间复杂度 O(n log n)，最坏 O(n^2)，额外占用 O(n) 内存，仅用于演示；
    生产场景的排序仍推荐 ``sort_items``（Timsort）。
    """
    effective_key: Callable[[T], Any] = key if key is not None else lambda item: item
    pairs: list[tuple[Any, T]] = [(effective_key(item), item) for item in items]

    def less_than(left: Any, right: Any) -> bool:
        """只使用 ``<`` 比较，并在此处承载 ``reverse`` 语义。"""
        return bool(right < left) if reverse else bool(left < right)

    stack: list[tuple[int, int]] = []
    if pairs:
        stack.append((0, len(pairs) - 1))
    while stack:
        low, high = stack.pop()
        if low >= high:
            continue
        pivot_key = pairs[(low + high) // 2][0]
        less: list[tuple[Any, T]] = []
        equal: list[tuple[Any, T]] = []
        greater: list[tuple[Any, T]] = []
        for pair in pairs[low : high + 1]:
            pair_key = pair[0]
            if less_than(pair_key, pivot_key):
                less.append(pair)
            elif less_than(pivot_key, pair_key):
                greater.append(pair)
            else:
                equal.append(pair)
        pairs[low : high + 1] = less + equal + greater
        middle_low = low + len(less)
        middle_high = middle_low + len(equal) - 1
        if middle_low - low > 1:
            stack.append((low, middle_low - 1))
        if high - middle_high > 1:
            stack.append((middle_high + 1, high))
    return [item for _, item in pairs]


if __name__ == "__main__":
    print(sort_items([3, 1, 2]))
    records = [{"name": "甲", "score": 2}, {"name": "乙", "score": 5}]
    print(sort_items(records, key=lambda record: record["score"], reverse=True))
    print(quick_sort([3, 1, 2]))
