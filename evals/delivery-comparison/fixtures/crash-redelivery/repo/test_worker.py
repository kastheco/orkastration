from worker import apply_delivery


def test_apply_delivery() -> None:
    state = apply_delivery({"total": 0, "applied": []}, "action-1", 4)
    assert state == {"total": 4, "applied": ["action-1"]}
