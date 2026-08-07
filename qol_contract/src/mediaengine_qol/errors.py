"""Public error types returned by the contract layer."""


class ContractError(ValueError):
    """Base class for invalid declarations and client requests."""


class SheetError(ContractError):
    """The declarative change sheet is malformed or internally inconsistent."""


class QueryError(ContractError):
    """A search request cannot be represented by the declared surface."""

