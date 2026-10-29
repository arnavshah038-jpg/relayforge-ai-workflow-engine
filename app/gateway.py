import random

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.config import settings
from app.db import SessionLocal
from app.errors import TransientError
from app.models import GatewayRefund


class GatewayError(TransientError):
    pass


class PaymentGateway:
    """Fake payment provider. Refunds are deduplicated on `key`, the same way
    real providers (Stripe etc.) treat an idempotency key."""

    def refund(self, key: str, amount: float) -> dict:
        if settings.gateway_failure_rate and random.random() < settings.gateway_failure_rate:
            raise GatewayError("provider timed out")

        # own session on purpose: the provider commits independently of us
        with SessionLocal() as db:
            row = db.scalar(select(GatewayRefund).where(GatewayRefund.key == key))
            if row is not None:
                return {"refund_id": row.id, "amount": row.amount, "duplicate": True}
            row = GatewayRefund(key=key, amount=amount)
            db.add(row)
            try:
                db.commit()
            except IntegrityError:
                db.rollback()
                row = db.scalar(select(GatewayRefund).where(GatewayRefund.key == key))
                return {"refund_id": row.id, "amount": row.amount, "duplicate": True}
            return {"refund_id": row.id, "amount": row.amount, "duplicate": False}


_gateway = PaymentGateway()


def get_gateway() -> PaymentGateway:
    return _gateway
