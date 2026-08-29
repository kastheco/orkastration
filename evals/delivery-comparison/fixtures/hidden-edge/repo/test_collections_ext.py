from collections_ext import dedupe_stable


def test_dedupe_stable() -> None:
    assert dedupe_stable([3, 1, 3, 2, 1]) == [3, 1, 2]
    assert dedupe_stable([]) == []
