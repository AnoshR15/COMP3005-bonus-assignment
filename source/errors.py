class RAError(Exception):
    """Expected user-facing engine error."""

class LexError(RAError):
    pass

class ParseError(RAError):
    pass

class NameErrorRA(RAError):
    pass

class SchemaError(RAError):
    pass

class TypeErrorRA(RAError):
    pass

class DataError(RAError):
    pass

class PlanError(RAError):
    """A plan in JSON that is malformed or does not follow the plan format."""
