import importlib.util
import pathlib
import sys

repo = pathlib.Path(sys.argv[1])
spec = importlib.util.spec_from_file_location("subject", repo / "worker.py")
assert spec and spec.loader
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
state = {"total": 0, "applied": []}
module.apply_delivery(state, "stable-1", 7)
module.apply_delivery(state, "stable-1", 7)
module.apply_delivery(state, "stable-2", 2)
assert state == {"total": 9, "applied": ["stable-1", "stable-2"]}
print("hidden behavior: at-most-once redelivery passed")
