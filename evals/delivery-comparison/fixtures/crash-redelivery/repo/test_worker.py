import unittest

from worker import apply_delivery


class ApplyDeliveryTests(unittest.TestCase):
    def test_apply_delivery(self) -> None:
        state = apply_delivery({"total": 0, "applied": []}, "action-1", 4)
        state = apply_delivery(state, "action-1", 4)
        self.assertEqual(state, {"total": 4, "applied": ["action-1"]})


if __name__ == "__main__":
    unittest.main()
