"""
SAP Plant Maintenance (PM) simulation.

Two MO states:
  * OPEN                  — created automatically on AI detection
  * UNDER_REPAIR          — supervisor chose REPAIR
  * CANCELLED             — supervisor rejected the case
  * SUPERSEDED_BY_REPLACE — supervisor chose REPLACE (superseded by procurement)
"""

from __future__ import annotations

import datetime as _dt
import uuid
from typing import Any

from backend.storage import append_record, update_record


def _now() -> str:
    return _dt.datetime.now().isoformat(timespec="seconds")


def create_maintenance_notification(case: dict[str, Any]) -> dict[str, Any]:
    notif_id = "MN-" + uuid.uuid4().hex[:6].upper()
    return {
        "notification_id": notif_id,
        "case_id": case.get("case_id"),
        "equipment_id": case.get("equipment_id"),
        "component": case.get("component"),
        "defect": case.get("defect"),
        "severity": case.get("severity"),
        "status": "NOTIFIED",
        "created_at": _now(),
    }


def create_maintenance_order(case: dict[str, Any], priority: str = "NORMAL", status: str = "OPEN") -> dict[str, Any]:
    """Create a SAP PM maintenance order and persist it."""
    mo_id = "MO-" + uuid.uuid4().hex[:6].upper()
    notification = create_maintenance_notification(case)

    order = {
        "maintenance_order_id": mo_id,
        "notification_id": notification["notification_id"],
        "case_id": case.get("case_id"),
        "equipment_id": case.get("equipment_id"),
        "component": case.get("component"),
        "defect": case.get("defect"),
        "severity": case.get("severity"),
        "issue": case.get("ai_report", ""),
        "priority": priority,
        "status": status,
        "assigned_to": "Maintenance Crew A",
        "created_at": _now(),
        "updated_at": _now(),
    }
    append_record("maintenance_orders.json", order)
    return order


def update_maintenance_order(mo_id: str, patch: dict[str, Any]) -> dict[str, Any] | None:
    patch = dict(patch)
    patch["updated_at"] = _now()
    return update_record("maintenance_orders.json", "maintenance_order_id", mo_id, patch)
