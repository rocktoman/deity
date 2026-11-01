import pytest

from demo import quick_sort, sort_items


def test_sorts_integers_ascending() -> None:
    assert sort_items([3, 1, 2]) == [1, 2, 3]


def test_reverse_sorts_descending() -> None:
    assert sort_items([3, 1, 2], reverse=True) == [3, 2, 1]


def test_key_sorts_dict_records_by_field() -> None:
    records = [
        {"name": "a", "score": 2},
        {"name": "b", "score": 9},
        {"name": "c", "score": 5},
    ]

    result = sort_items(records, key=lambda item: item["score"], reverse=True)

    assert [item["name"] for item in result] == ["b", "c", "a"]


def test_stable_for_equal_keys() -> None:
    records = [("a", 1), ("a", 2), ("b", 0), ("a", 3)]

    def first(item: tuple[str, int]) -> str:
        return item[0]

    result = sort_items(records, key=first)

    assert [item[1] for item in result] == [1, 2, 3, 0]


def test_does_not_mutate_input() -> None:
    items = [3, 1, 2]

    result = sort_items(items)

    assert items == [3, 1, 2]
    assert result == [1, 2, 3]
    assert result is not items


def test_empty_input_returns_new_empty_list() -> None:
    items: list[int] = []

    result = sort_items(items)

    assert result == []
    assert result is not items


def test_single_element_returned_unchanged() -> None:
    assert sort_items([42]) == [42]


def test_incomparable_elements_raise_type_error() -> None:
    with pytest.raises(TypeError):
        sort_items([1, "a"])


def test_quick_sort_sorts_integers_ascending() -> None:
    assert quick_sort([3, 1, 2]) == [1, 2, 3]


def test_quick_sort_reverse_sorts_descending() -> None:
    assert quick_sort([3, 1, 2], reverse=True) == [3, 2, 1]


def test_quick_sort_key_sorts_dict_records_by_field() -> None:
    records = [
        {"name": "a", "score": 2},
        {"name": "b", "score": 9},
        {"name": "c", "score": 5},
    ]

    result = quick_sort(records, key=lambda item: item["score"], reverse=True)

    assert [item["name"] for item in result] == ["b", "c", "a"]


def test_quick_sort_stable_for_equal_keys() -> None:
    records = [("a", 1), ("a", 2), ("b", 0), ("a", 3)]

    def first(item: tuple[str, int]) -> str:
        return item[0]

    result = quick_sort(records, key=first)

    assert [item[1] for item in result] == [1, 2, 3, 0]


def test_quick_sort_reverse_keeps_input_order_for_equal_keys() -> None:
    records = [("a", 1), ("a", 2), ("b", 0), ("a", 3)]

    def first(item: tuple[str, int]) -> str:
        return item[0]

    result = quick_sort(records, key=first, reverse=True)

    assert [item[1] for item in result] == [0, 1, 2, 3]


def test_quick_sort_does_not_mutate_input() -> None:
    items = [3, 1, 2]

    result = quick_sort(items)

    assert items == [3, 1, 2]
    assert result == [1, 2, 3]
    assert result is not items


def test_quick_sort_empty_input_returns_new_empty_list() -> None:
    items: list[int] = []

    result = quick_sort(items)

    assert result == []
    assert result is not items


def test_quick_sort_single_element_returned_unchanged() -> None:
    assert quick_sort([42]) == [42]


def test_quick_sort_already_sorted_input_unchanged() -> None:
    assert quick_sort([1, 2, 3]) == [1, 2, 3]


def test_quick_sort_accepts_iterables_and_returns_list() -> None:
    assert quick_sort((3, 1, 2)) == [1, 2, 3]

    result = quick_sort(iter([3, 1, 2]))

    assert result == [1, 2, 3]
    assert isinstance(result, list)


def test_quick_sort_incomparable_elements_raise_type_error() -> None:
    with pytest.raises(TypeError):
        quick_sort([1, "a"])


def test_quick_sort_matches_sorted_on_duplicates() -> None:
    values = [(index * 37) % 17 for index in range(1000)]

    assert quick_sort(values) == sorted(values)


def test_quick_sort_handles_large_sorted_input_iteratively() -> None:
    values = list(range(2000))

    assert quick_sort(values) == values
