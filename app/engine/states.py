from enum import Enum

from app.errors import Conflict


class Status(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    WAITING_APPROVAL = "waiting_approval"
    COMPLETED = "completed"
    FAILED = "failed"
    REJECTED = "rejected"


# the runner never picks these up on its own
IDLE = {Status.WAITING_APPROVAL, Status.COMPLETED, Status.FAILED, Status.REJECTED}

_ALLOWED = {
    Status.PENDING: {Status.RUNNING, Status.FAILED},
    Status.RUNNING: {Status.WAITING_APPROVAL, Status.COMPLETED, Status.FAILED},
    Status.WAITING_APPROVAL: {Status.RUNNING, Status.REJECTED},
    Status.FAILED: {Status.RUNNING},  # only through an explicit manual retry
    Status.COMPLETED: set(),
    Status.REJECTED: set(),
}


class InvalidTransition(Conflict):
    pass


def can_move(current: Status, new: Status) -> bool:
    return new in _ALLOWED[current]


def move(workflow, new: Status) -> None:
    current = Status(workflow.status)
    if not can_move(current, new):
        raise InvalidTransition(f"cannot go from {current.value} to {new.value}")
    workflow.status = new.value
