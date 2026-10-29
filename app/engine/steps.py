from app.ai import classify_ticket
from app.gateway import get_gateway


def analyze(wf) -> dict:
    result = classify_ticket(wf.input["subject"], wf.input["body"])
    return result.model_dump()


def needs_approval(analysis: dict) -> bool:
    return analysis["action"] == "refund"


def execute(wf, analysis: dict) -> dict:
    if analysis["action"] != "refund":
        return {"result": analysis["action"], "note": "no external action needed"}

    out = get_gateway().refund(key=f"wf-{wf.id}-refund", amount=analysis["refund_amount"])
    return {"result": "refunded", **out}
