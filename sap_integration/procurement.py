"""
SAP MM procurement simulation.

Workflow (strict):
    1. create_purchase_requisition(case, supplier, qty)
       → PR with status=PENDING_APPROVAL
    2. Supervisor (Approval 2) calls approve_purchase_requisition(pr_id)
       → PR status = APPROVED
    3. create_purchase_order(case, pr, supplier, unit_price)
       → PO with status=CREATED, linked to PR
"""

from __future__ import annotations

import datetime as _dt
import uuid
from typing import Any

from backend.storage import append_record, update_record, find_record


def _now() -> str:
    return _dt.datetime.now().isoformat(timespec="seconds")


def create_purchase_requisition(
    case: dict[str, Any],
    supplier: dict[str, Any],
    quantity: int = 1,
    note: str | None = None,
) -> dict[str, Any]:
    pr_id = "PR-" + uuid.uuid4().hex[:6].upper()
    pr = {
        "pr_id": pr_id,
        "case_id": case.get("case_id"),
        "component": case.get("component"),
        "quantity": int(quantity),
        "supplier_id": supplier.get("supplier_id"),
        "supplier_name": supplier.get("name"),
        "status": "PENDING_APPROVAL",
        "note": note,
        "created_at": _now(),
        "updated_at": _now(),
        "approved_at": None,
        "approved_by": None,
    }
    append_record("purchase_requisitions.json", pr)
    return pr


def approve_purchase_requisition(pr_id: str, approver: str = "supervisor") -> dict[str, Any] | None:
    patch = {
        "status": "APPROVED",
        "approved_at": _now(),
        "approved_by": approver,
        "updated_at": _now(),
    }
    return update_record("purchase_requisitions.json", "pr_id", pr_id, patch)


def reject_purchase_requisition(pr_id: str, approver: str = "supervisor", reason: str | None = None) -> dict[str, Any] | None:
    patch = {
        "status": "REJECTED",
        "approved_at": _now(),
        "approved_by": approver,
        "reject_reason": reason,
        "updated_at": _now(),
    }
    return update_record("purchase_requisitions.json", "pr_id", pr_id, patch)


def get_purchase_requisition(pr_id: str) -> dict[str, Any] | None:
    return find_record("purchase_requisitions.json", "pr_id", pr_id)


def create_purchase_order(
    case: dict[str, Any],
    pr: dict[str, Any],
    supplier: dict[str, Any],
    unit_price: float,
) -> dict[str, Any]:
    po_id = "PO-" + uuid.uuid4().hex[:6].upper()
    qty = int(pr.get("quantity", 1))
    total = round(unit_price * qty * float(supplier.get("cost_multiplier", 1.0)), 2)

    po = {
        "po_id": po_id,
        "pr_id": pr.get("pr_id"),
        "case_id": case.get("case_id"),
        "component": case.get("component"),
        "quantity": qty,
        "unit_price": unit_price,
        "total_cost": total,
        "currency": "USD",
        "supplier_id": supplier.get("supplier_id"),
        "supplier_name": supplier.get("name"),
        "expected_delivery_days": supplier.get("delivery_days"),
        "status": "CREATED",
        "created_at": _now(),
    }
    append_record("purchase_orders.json", po)
    return po
