class DomainError(Exception):
    status_code = 400

    def __init__(self, message: str = ""):
        super().__init__(message)
        self.message = message or self.__class__.__name__


class NotFound(DomainError):
    status_code = 404


class Conflict(DomainError):
    status_code = 409


class Forbidden(DomainError):
    status_code = 403


class TransientError(Exception):
    """Something that is worth retrying (network blip, bad LLM output, provider timeout)."""
