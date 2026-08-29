import unittest

from collections_ext import dedupe_stable


class DedupeStableTests(unittest.TestCase):
    def test_dedupe_stable(self) -> None:
        self.assertEqual(dedupe_stable([3, 1, 3, 2, 1]), [3, 1, 2])
        self.assertEqual(dedupe_stable([[1], [1], [2]]), [[1], [2]])


if __name__ == "__main__":
    unittest.main()
