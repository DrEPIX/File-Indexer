# Minimal shadow stub for NumPy.
#
# NumPy 2.x ships type stubs written with PEP 695 `type` statements, which mypy
# refuses to parse while checking against a Python 3.11 target — and it refuses
# loudly enough to abort before reaching any project code. Lowering the target
# would stop enforcing the 3.11 floor this project promises in pyproject.toml,
# and `follow_imports = skip` does not apply to stub packages.
#
# Shadowing the stub keeps `python_version = "3.11"` meaningful. MediaEngine
# uses NumPy only for embedding maths at the edges (identity clustering, vector
# search fallbacks), where the array element types mypy could infer are not the
# properties worth checking.
from typing import Any

def __getattr__(name: str) -> Any: ...
