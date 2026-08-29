import importlib.util
import pathlib
import sys

repo = pathlib.Path(sys.argv[1])
spec = importlib.util.spec_from_file_location("subject", repo / "text_utils.py")
assert spec and spec.loader
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
assert module.normalize_label("  alpha\t beta\n gamma  ") == "alpha beta gamma"
assert module.normalize_label("\t\n") == ""
print("hidden behavior: clean normalization passed")
