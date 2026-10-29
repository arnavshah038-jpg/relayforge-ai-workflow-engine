from datetime import datetime
from typing import Annotated, Literal

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, StringConstraints

Role = Literal["agent", "manager", "admin"]

EMAIL_PATTERN = r"^[^@\s]+@[^@\s]+\.[^@\s]+$"


def _no_nul(value: str) -> str:
    # Postgres can't store NUL characters in text columns
    if "\x00" in value:
        raise ValueError("null characters are not allowed")
    return value


def _text(max_length: int):
    return Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=max_length),
                     AfterValidator(_no_nul)]


class SignupIn(BaseModel):
    company_name: Annotated[str, StringConstraints(strip_whitespace=True, min_length=2, max_length=120)]
    email: str = Field(min_length=5, max_length=200, pattern=EMAIL_PATTERN)
    password: str = Field(min_length=8, max_length=128)


class LoginIn(BaseModel):
    email: str = Field(max_length=200)
    password: str = Field(max_length=128)


class NewUserIn(BaseModel):
    email: str = Field(min_length=5, max_length=200, pattern=EMAIL_PATTERN)
    password: str = Field(min_length=8, max_length=128)
    role: Role = "agent"


class TokenOut(BaseModel):
    access_token: str
    token_type: str = "bearer"


class UserOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    tenant_id: int
    email: str
    role: str


class TicketIn(BaseModel):
    customer_email: Annotated[str, StringConstraints(strip_whitespace=True, max_length=200,
                                                     pattern=EMAIL_PATTERN)]
    subject: _text(200)
    body: _text(5000)


class StepOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    name: str
    status: str
    attempts: int
    output: dict | None
    error: str | None


class WorkflowSummary(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    status: str
    input: dict
    created_at: datetime
    updated_at: datetime


class WorkflowOut(WorkflowSummary):
    steps: list[StepOut]


class WorkflowPage(BaseModel):
    items: list[WorkflowSummary]
    total: int
    limit: int
    offset: int


class RejectIn(BaseModel):
    reason: Annotated[str, StringConstraints(max_length=300), AfterValidator(_no_nul)] | None = None


class AuditOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    actor: str
    event: str
    detail: dict
    created_at: datetime
