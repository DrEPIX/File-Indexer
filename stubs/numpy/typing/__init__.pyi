# Shadow for numpy.typing, for the same reason as stubs/numpy/__init__.pyi:
# resolving it pulls in numpy._typing, whose modules use Python 3.12 syntax
# that mypy will not parse against this project's 3.11 target.
from typing import Any

ArrayLike = Any
DTypeLike = Any
NDArray = Any

def __getattr__(name: str) -> Any: ...
