import time

from app.gateway import GatewayError, PaymentGateway


class Crash(BaseException):
    """Stands in for the process being killed. BaseException so the runner can't swallow it."""


class ScriptedGateway(PaymentGateway):
    def __init__(self, fail_first=0, crash_after_refund=False, delay=0.0):
        self.fail_first = fail_first
        self.crash_after_refund = crash_after_refund
        self.delay = delay
        self.calls = 0

    def refund(self, key, amount):
        self.calls += 1
        if self.delay:
            time.sleep(self.delay)
        if self.calls <= self.fail_first:
            raise GatewayError("provider timed out")
        out = super().refund(key, amount)
        if self.crash_after_refund:
            self.crash_after_refund = False
            raise Crash()
        return out
