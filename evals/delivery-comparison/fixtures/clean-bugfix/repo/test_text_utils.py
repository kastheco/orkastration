import unittest

from text_utils import normalize_label


class NormalizeLabelTests(unittest.TestCase):
    def test_normalize_label(self) -> None:
        self.assertEqual(normalize_label("  hello   world  "), "hello world")
        self.assertEqual(normalize_label("hello"), "hello")


if __name__ == "__main__":
    unittest.main()
