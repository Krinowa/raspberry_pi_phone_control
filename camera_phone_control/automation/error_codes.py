"""Stable client-facing error metadata without replacing legacy messages."""


class ClientVisibleError(ValueError):
    def __init__(self, message, error_code, error_params=None):
        super().__init__(message)
        self.error_code = error_code
        self.error_params = dict(error_params or {})


def error_fields(message, error_code=None, error_params=None, **extra):
    fields = {"error": str(message), **extra}
    if error_code:
        fields["error_code"] = error_code
        fields["error_params"] = dict(error_params or {})
    return fields


def exception_error_fields(error, **extra):
    return error_fields(
        error,
        getattr(error, "error_code", None),
        getattr(error, "error_params", None),
        **extra,
    )
