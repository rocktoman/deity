"""冒泡排序示例。运行 `python bubble_sort.py` 查看结果。"""

from collections.abc import Sequence


def bubble_sort[T: (int, float, str)](items: Sequence[T]) -> list[T]:
    """返回升序排序后的新列表，不修改输入序列。"""
    result = list(items)
    for end in range(len(result) - 1, 0, -1):
        swapped = False
        for index in range(end):
            if result[index] > result[index + 1]:
                result[index], result[index + 1] = result[index + 1], result[index]
                swapped = True
        if not swapped:
            break
    return result


if __name__ == "__main__":
    print(bubble_sort([5, 2, 9, 1, 5, 6]))
