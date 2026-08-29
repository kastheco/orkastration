from text_utils import normalize_label


def test_normalize_label() -> None:
    assert normalize_label("  hello world  ") == "hello world"
    assert normalize_label("hello") == "hello"
