"""Full end-to-end integration test that exercises every branch of the strict
architecture by driving the service layer directly (no Flask needed).

Covers: REPAIR, REPLACE-FULL, REPLACE-PARTIAL, REPLACE-NONE,
Approve PR → PO, Reject PR, Reject case, Resubmit, and dashboard aggregates.
"""

import json
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

# Reset data files before the test runs
DATA = ROOT / "data"
BACKUP = ROOT / "_test_backup"
BACKUP.mkdir(exist_ok=True)

FILES_TO_RESET = [
    ("cases.json", "[]"),
    ("maintenance_orders.json", "[]"),
    ("reservations.json", "[]"),
    ("purchase_requisitions.json", "[]"),
    ("purchase_orders.json", "[]"),
]

# back up inventory to restore after test
for name in ("inventory.json",) + tuple(n for n, _ in FILES_TO_RESET):
    src = DATA / name
    if src.exists():
        shutil.copy(src, BACKUP / name)

for name, initial in FILES_TO_RESET:
    (DATA / name).write_text(initial, encoding="utf-8")

from backend import cases as case_service  # noqa: E402
from backend.storage import find_record, update_record  # noqa: E402
from inventory_module import ewm_check, ai_select_supplier, create_reservation  # noqa: E402
from sap_integration import (  # noqa: E402
    create_maintenance_order,
    update_maintenance_order,
    create_purchase_requisition,
    approve_purchase_requisition,
    reject_purchase_requisition,
    get_purchase_requisition,
    create_purchase_order,
)

_SEV = {"HIGH": "URGENT", "MEDIUM": "NORMAL", "LOW": "LOW"}


def orchestrate_new_case(component=None, severity="MEDIUM"):
    """Simulate the operator-side /api/cases POST: AI detect → case → MO."""
    ai_report = {
        "component": component or "bolt",
        "defect": "crack",
        "damaged_area": "head",
        "severity": severity,
        "confidence": 0.91,
        "equipment_id": "ENG-001",
        "ai_report": "Simulated finding for integration test",
        "ai_source": "test-mock",
        "recommendation": {"decision": "REPAIR", "risk_level": "LOW", "reason": "seed"},
    }
    case = case_service.create_case(ai_report, image_path="uploads/test.jpg")
    priority = _SEV.get(severity, "NORMAL")
    mo = create_maintenance_order(case, priority=priority, status="OPEN")
    case = case_service.transition(
        case["case_id"],
        action="MAINTENANCE_ORDER_OPENED",
        by="system",
        note=f"MO {mo['maintenance_order_id']} auto-created",
        patch={"initial_mo_id": mo["maintenance_order_id"]},
    )
    assert case["status"] == "NEW"
    assert case["initial_mo_id"] == mo["maintenance_order_id"]
    print(f"   ✓ case {case['case_id']} NEW · component={case['component']} · MO {mo['maintenance_order_id']}")
    return case, mo


def orchestrate_repair(case):
    update_maintenance_order(case["initial_mo_id"], {"status": "UNDER_REPAIR"})
    return case_service.transition(
        case["case_id"],
        action="REPAIR_STARTED",
        by="supervisor",
        note=f"Repair dispatched — MO {case['initial_mo_id']} UNDER_REPAIR.",
        patch={
            "status": "UNDER_REPAIR",
            "stage": "END",
            "decision": "REPAIR",
            "repair_mo_id": case["initial_mo_id"],
        },
        approval=("APPROVAL_1", "REPAIR"),
    )


def orchestrate_replace(case, required_qty):
    component = case["component"]
    ewm = ewm_check(component, required_qty)
    status = ewm["status"]

    update_maintenance_order(case["initial_mo_id"], {"status": "SUPERSEDED_BY_REPLACE"})
    case_service.transition(
        case["case_id"],
        action="REPLACE_INITIATED",
        by="supervisor",
        note=f"EWM={status}",
        patch={
            "decision": "REPLACE",
            "required_qty": required_qty,
            "ewm_status": status,
            "ewm_snapshot": ewm,
        },
        approval=("APPROVAL_1", "REPLACE"),
    )

    if status == "FULL":
        reservation = create_reservation(case, component, ewm["reservable_qty"], ewm["location"])
        case = case_service.transition(
            case["case_id"],
            action="RESERVATION_CREATED",
            by="system",
            note=f"FULL reserve {reservation['reservation_id']}",
            patch={
                "status": "RESERVED",
                "stage": "END",
                "reservation_id": reservation["reservation_id"],
                "reserved_qty": reservation["quantity"],
                "shortage_qty": 0,
            },
        )
        return {"case": case, "ewm": ewm, "reservation": reservation, "pr": None}

    reservation = None
    if status == "PARTIAL" and ewm["reservable_qty"] > 0:
        reservation = create_reservation(case, component, ewm["reservable_qty"], ewm["location"])
        case_service.transition(
            case["case_id"],
            action="RESERVATION_CREATED",
            by="system",
            note=f"PARTIAL reserve {reservation['reservation_id']}",
            patch={
                "reservation_id": reservation["reservation_id"],
                "reserved_qty": reservation["quantity"],
            },
        )

    shortage_qty = ewm["shortage_qty"]
    supplier_sel = ai_select_supplier(component, qty=shortage_qty)
    if "error" in supplier_sel:
        raise RuntimeError(supplier_sel["error"])
    supplier = supplier_sel["supplier"]
    pr = create_purchase_requisition(
        case_service.get_case(case["case_id"]),
        supplier,
        quantity=shortage_qty,
        note=f"Shortage {shortage_qty} × {component}",
    )
    case = case_service.transition(
        case["case_id"],
        action="PR_RAISED",
        by="system",
        note=f"AI supplier={supplier['name']}, PR {pr['pr_id']}",
        patch={
            "status": "PR_PENDING_APPROVAL",
            "stage": "APPROVAL_2",
            "shortage_qty": shortage_qty,
            "supplier": supplier,
            "supplier_reason": supplier_sel["reason"],
            "pr_id": pr["pr_id"],
            "pr_status": "PENDING_APPROVAL",
        },
    )
    return {"case": case, "ewm": ewm, "reservation": reservation, "pr": pr, "supplier": supplier}


def orchestrate_approve_pr(case_id):
    case = case_service.get_case(case_id)
    approve_purchase_requisition(case["pr_id"], approver="supervisor")
    pr = get_purchase_requisition(case["pr_id"])
    supplier = case["supplier"]
    unit_price = float((case.get("ewm_snapshot") or {}).get("unit_price") or 1000)
    po = create_purchase_order(case, pr, supplier, unit_price=unit_price)
    case = case_service.transition(
        case_id,
        action="PROCUREMENT_COMPLETED",
        by="supervisor",
        note=f"PR→PO {po['po_id']} ${po['total_cost']:.2f}",
        patch={
            "status": "PROCUREMENT_COMPLETED",
            "stage": "END",
            "pr_status": "APPROVED",
            "po_id": po["po_id"],
            "total_cost": po["total_cost"],
        },
        approval=("APPROVAL_2", "APPROVE_PR"),
    )
    return case, pr, po


def orchestrate_reject_pr(case_id, note):
    case = case_service.get_case(case_id)
    reject_purchase_requisition(case["pr_id"], approver="supervisor", reason=note)
    return case_service.transition(
        case_id,
        action="PR_REJECTED",
        by="supervisor",
        note=note,
        patch={"status": "PR_REJECTED", "stage": "END", "pr_status": "REJECTED"},
        approval=("APPROVAL_2", "REJECT_PR"),
    )


def orchestrate_reject_case(case_id, note):
    case = case_service.get_case(case_id)
    if case.get("initial_mo_id"):
        update_maintenance_order(case["initial_mo_id"], {"status": "CANCELLED"})
    return case_service.transition(
        case_id,
        action="REJECTED",
        by="supervisor",
        note=note,
        patch={"status": "REJECTED", "stage": "END", "decision": "REJECT", "decision_note": note},
        approval=("APPROVAL_1", "REJECT"),
    )


def orchestrate_resubmit(case_id, note):
    case = case_service.get_case(case_id)
    if case.get("initial_mo_id"):
        update_maintenance_order(case["initial_mo_id"], {"status": "AWAITING_RESUBMIT"})
    return case_service.transition(
        case_id,
        action="RESUBMIT_REQUESTED",
        by="supervisor",
        note=note,
        patch={"status": "RESUBMIT", "stage": "OPERATOR", "decision": "RESUBMIT", "decision_note": note},
        approval=("APPROVAL_1", "RESUBMIT"),
    )


# ─── TESTS ─────────────────────────────────────────────────────────────────

def test_repair_path():
    print("▶ REPAIR PATH")
    case, _ = orchestrate_new_case(component="bolt")
    after = orchestrate_repair(case)
    assert after["status"] == "UNDER_REPAIR"
    assert after["stage"] == "END"
    assert after["decision"] == "REPAIR"
    assert after["repair_mo_id"] == case["initial_mo_id"]
    # verify MO was transitioned
    mo = find_record("maintenance_orders.json", "maintenance_order_id", case["initial_mo_id"])
    assert mo["status"] == "UNDER_REPAIR"
    print(f"   ✓ {case['case_id']} UNDER_REPAIR · MO {after['repair_mo_id']}")


def test_full_path():
    print("▶ REPLACE-FULL PATH")
    case, _ = orchestrate_new_case(component="bolt")  # inventory has plenty of bolts
    result = orchestrate_replace(case, required_qty=5)
    assert result["ewm"]["status"] == "FULL", result["ewm"]
    assert result["reservation"] is not None
    assert result["pr"] is None
    after = result["case"]
    assert after["status"] == "RESERVED"
    assert after["stage"] == "END"
    print(f"   ✓ {case['case_id']} RESERVED qty=5 · res {result['reservation']['reservation_id']}")


def test_partial_path():
    print("▶ REPLACE-PARTIAL PATH")
    # Make shaft inventory predictable: set to exactly 1 unit
    inv = json.loads((DATA / "inventory.json").read_text())
    if "shaft" in inv:
        inv["shaft"]["quantity"] = 1
    else:
        inv["shaft"] = {"quantity": 1, "unit_price": 2500, "location": "WH-A"}
    (DATA / "inventory.json").write_text(json.dumps(inv, indent=2))

    case, _ = orchestrate_new_case(component="shaft")
    result = orchestrate_replace(case, required_qty=3)
    assert result["ewm"]["status"] == "PARTIAL", result["ewm"]
    assert result["reservation"] is not None
    assert result["pr"] is not None
    assert result["pr"]["quantity"] == 2, f"expected shortage of 2, got {result['pr']['quantity']}"
    assert result["case"]["status"] == "PR_PENDING_APPROVAL"
    assert result["case"]["stage"] == "APPROVAL_2"

    # Approval 2 — approve PR → PO
    after, pr, po = orchestrate_approve_pr(case["case_id"])
    assert after["status"] == "PROCUREMENT_COMPLETED"
    assert pr["status"] == "APPROVED"
    assert po["pr_id"] == result["pr"]["pr_id"]
    print(f"   ✓ {case['case_id']} PARTIAL reserved=1 shortage=2 · PR→PO {po['po_id']} total ${po['total_cost']:.2f}")


def test_none_path():
    print("▶ REPLACE-NONE PATH")
    # Force fan blade inventory to 0
    inv = json.loads((DATA / "inventory.json").read_text())
    if "fan blade" in inv:
        inv["fan blade"]["quantity"] = 0
    else:
        inv["fan blade"] = {"quantity": 0, "unit_price": 4200, "location": "WH-B"}
    (DATA / "inventory.json").write_text(json.dumps(inv, indent=2))

    case, _ = orchestrate_new_case(component="fan blade")
    result = orchestrate_replace(case, required_qty=1)
    assert result["ewm"]["status"] == "NONE", result["ewm"]
    assert result["reservation"] is None
    assert result["pr"] is not None
    assert result["pr"]["quantity"] == 1
    assert result["case"]["status"] == "PR_PENDING_APPROVAL"

    # Reject PR (Approval 2)
    after = orchestrate_reject_pr(case["case_id"], "Budget on hold")
    assert after["status"] == "PR_REJECTED"
    assert after["pr_status"] == "REJECTED"
    print(f"   ✓ {case['case_id']} NONE · PR rejected at Approval 2")


def test_reject_resubmit():
    print("▶ REJECT + RESUBMIT")
    case_a, _ = orchestrate_new_case(component="bolt")
    r = orchestrate_reject_case(case_a["case_id"], "Image blurry")
    assert r["status"] == "REJECTED"
    mo = find_record("maintenance_orders.json", "maintenance_order_id", case_a["initial_mo_id"])
    assert mo["status"] == "CANCELLED"
    print(f"   ✓ {case_a['case_id']} REJECTED · MO cancelled")

    case_b, _ = orchestrate_new_case(component="bolt")
    r = orchestrate_resubmit(case_b["case_id"], "Need closer shot")
    assert r["status"] == "RESUBMIT"
    mo = find_record("maintenance_orders.json", "maintenance_order_id", case_b["initial_mo_id"])
    assert mo["status"] == "AWAITING_RESUBMIT"
    print(f"   ✓ {case_b['case_id']} RESUBMIT · MO awaiting resubmit")


def test_dashboard():
    print("▶ DASHBOARD AGGREGATES")
    stats = case_service.dashboard_stats()
    assert stats["total_cases"] >= 6
    assert stats["maintenance_orders"] >= 6
    assert stats["purchase_requisitions"] >= 2
    assert stats["purchase_orders"] >= 1
    assert stats["reservations"] >= 2
    assert (stats["ewm_counts"] or {}).get("FULL", 0) >= 1
    assert (stats["ewm_counts"] or {}).get("PARTIAL", 0) >= 1
    assert (stats["ewm_counts"] or {}).get("NONE", 0) >= 1
    print(f"   ✓ {stats['total_cases']} cases · {stats['maintenance_orders']} MOs · "
          f"{stats['reservations']} reservations · {stats['purchase_requisitions']} PRs · "
          f"{stats['purchase_orders']} POs · spend ${stats['total_procurement_spend']:.2f}")
    print(f"     EWM counts: {stats['ewm_counts']}")


def main():
    print("=" * 70)
    print("HCL AI FORCE · END-TO-END INTEGRATION TEST (core service layer)")
    print("=" * 70)
    try:
        test_repair_path()
        test_full_path()
        test_partial_path()
        test_none_path()
        test_reject_resubmit()
        test_dashboard()
        print("=" * 70)
        print("✅ ALL CHECKS PASSED")
        print("=" * 70)
    finally:
        # Restore data files
        for name, initial in FILES_TO_RESET:
            (DATA / name).write_text(initial, encoding="utf-8")
        shutil.copy(BACKUP / "inventory.json", DATA / "inventory.json")
        shutil.rmtree(BACKUP)
        print("data files reset — ready for demo.")


if __name__ == "__main__":
    main()
