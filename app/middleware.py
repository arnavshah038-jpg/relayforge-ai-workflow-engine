import logging
import uuid
from contextvars import ContextVar

request_id_var: ContextVar[str] = ContextVar("request_id", default="-")


class _RequestIdFilter(logging.Filter):
    def filter(self, record):
        record.request_id = request_id_var.get()
        return True


def setup_logging() -> None:
    log = logging.getLogger("relayforge")
    if log.handlers:
        return
    handler = logging.StreamHandler()
    handler.addFilter(_RequestIdFilter())
    handler.setFormatter(logging.Formatter(
        "%(asctime)s %(levelname)s [%(request_id)s] %(name)s: %(message)s"))
    log.addHandler(handler)
    log.setLevel(logging.INFO)


def new_request_id(incoming: str | None) -> str:
    # trust a caller-supplied id only if it looks sane
    if incoming and len(incoming) <= 64 and incoming.replace("-", "").isalnum():
        return incoming
    return uuid.uuid4().hex[:16]
