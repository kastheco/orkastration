import importlib.util
import pathlib
import sys

repo = pathlib.Path(sys.argv[1])
spec = importlib.util.spec_from_file_location("subject", repo / "collections_ext.py")
assert spec and spec.loader
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
values = [{"x": [1]}, {"x": [1]}, [2], [2], {"x": [3]}]
assert module.dedupe_stable(values) == [{"x": [1]}, [2], {"x": [3]}]
print("hidden behavior: unhashable equality passed")
