"""
Case lifecycle service — STRICT workflow.

Case lifecycle
==============

    Operator: image → AI → initial MO → case (status=NEW)
                                       ↓
                          Supervisor Inbox (Approval 1)
                                       ↓
      ┌───────────┬────────────┬─────────────┬────────────────────────┐
      ↓           ↓            ↓             ↓                        ↓
    REJECT    RESUBMIT       REPAIR       REPLACE                (see below)
   REJECTED   RESUBMIT    UNDER_REPAIR       ↓
                             (END)    EWM check
                                         ↓
                            ┌────────────┼────────────┐
                         FULL          PARTIAL       NONE
                           ↓             ↓             ↓
                      RESERVED       RESERVED     (no reservation)
                        (END)    + PR shortage  + PR full-qty
                                        ↓              ↓
                                  PR_PENDING_APPROVAL (Approval 2)
                                        ↓
                                   approve → PO → PROCUREMENT_COMPLETED
                                   reject  → PR_REJECTED
"""

from __future__ import annotations

import datetime as _dt
from typing import Any

from backend.storage import load, append_record, update_record, find_record

_CASES_FILE = "cases.json"


def _now() -> str:
    return _dt.datetime.now().isoformat(timespec="seconds")


def _next_case_id() -> str:
    existing = load(_CASES_FILE)
    n = 1001 + len(existing)
    return f"CASE-{n:04d}"


# ─── Creation ───────────────────────────────────────────────────────────────
def create_case(
    ai_report: dict[str, Any],
    image_path: str,
    operator: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a fresh case from an AI report. Status = NEW.

    At this point the decision engine (rule-based + explainable) runs so
    the supervisor sees a ready REPAIR/REPLACE recommendation with a full
    reasoning trail. The Grok AI layer optionally validates the decision
    (no-op when GROK_API_KEY is not set).

    The optional ``operator`` dict (``username``, ``name``, ``role``) is
    persisted on the case so the Supervisor Inbox can show *who* filed it.

    NOTE: No Maintenance Order is raised here. An MO is only created when
    the supervisor clicks the "Repair" button (see /api/cases/<id>/repair).
    """
    from decision_engine import recommend_decision, validate_decision

    recommendation = recommend_decision(ai_report)
    # AI-assisted decision validation (Grok). Gracefully falls back when
    # GROK_API_KEY is not set.
    try:
        ai_validation = validate_decision(ai_report, recommendation)
    except Exception as exc:
        ai_validation = {
            "verdict": "AGREE",
            "final_decision": recommendation.get("decision"),
            "confidence": 0.5,
            "reasoning": f"AI validation skipped: {exc}",
            "source": "fallback",
        }

    now = _now()
    op = operator or {}
    op_username = (op.get("username") or "").strip()
    op_name = (op.get("name") or "").strip()
    op_role = (op.get("role") or "operator").strip() or "operator"
    operator_record = {
        "username": op_username,
        "name": op_name or op_username or "operator",
        "role": op_role,
    }
    operator_label = (
        f"{operator_record['name']} ({op_username})"
        if op_username and op_name and op_username != op_name
        else (op_name or op_username or "operator")
    )

    case = {
        "case_id": _next_case_id(),
        "image_path": image_path,
        "component": ai_report.get("component"),
        "defect": ai_report.get("defect"),
        "damaged_area": ai_report.get("damaged_area"),
        "severity": ai_report.get("severity"),
        "confidence": ai_report.get("confidence"),
        "equipment_id": ai_report.get("equipment_id"),
        "ai_report": ai_report.get("ai_report"),
        "ai_source": ai_report.get("source", "unknown"),
        "recommendation": recommendation,
        "ai_validation": ai_validation,
        # Operator who filed the case — surfaced on the Supervisor Inbox card
        # and in the case-detail modal so reviewers know who to follow up with.
        "operator": operator_record,
        "operator_username": op_username,
        "operator_name": operator_record["name"],
        "operator_label": operator_label,
        # Cases start as DRAFT — they are not visible in the Supervisor Inbox
        # until the Operator explicitly clicks "Submit" in the result panel,
        # which transitions the case to status NEW via /api/cases/<id>/submit.
        "status": "DRAFT",
        "stage": "OPERATOR_REVIEW",
        "decision": None,
        "decision_note": None,
        # SAP PM — MO is NOT auto-created on image upload. It is only
        # created when the supervisor approves the REPAIR action.
        "initial_mo_id": None,
        "repair_mo_id": None,
        # Replacement flow
        "required_qty": 1,
        "ewm_status": None,               # FULL | PARTIAL | NONE
        "ewm_snapshot": None,
        "reservation_id": None,
        "reserved_qty": 0,
        "shortage_qty": 0,
        "supplier": None,
        "supplier_reason": None,
        "pr_id": None,
        "pr_status": None,                # PENDING_APPROVAL | APPROVED | REJECTED
        "po_id": None,
        "total_cost": None,
        "approvals": [],
        "created_at": now,
        "updated_at": now,
        "history": [
            {"action": "CASE_DRAFTED", "at": now, "by": operator_label,
             "note": "AI analysis complete — case drafted, awaiting operator Submit action."}
        ],
    }
    append_record(_CASES_FILE, case)
    return case


def submit_to_inbox(
    case_id: str,
    by: str = "operator",
    operator: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Operator clicks Submit in the result panel → DRAFT → NEW (visible in Supervisor Inbox).

    If ``operator`` is supplied (and the case was drafted anonymously, e.g.
    by an older client), we backfill the operator identity onto the case
    so the Supervisor Inbox always has a name to display.
    """
    case = get_case(case_id)
    if case is None:
        return None
    if case.get("status") != "DRAFT":
        # Idempotent: already submitted.
        return case

    patch: dict[str, Any] = {"status": "NEW", "stage": "APPROVAL_1"}
    if operator:
        op_username = (operator.get("username") or "").strip()
        op_name = (operator.get("name") or "").strip()
        op_role = (operator.get("role") or "operator").strip() or "operator"
        if op_username or op_name:
            existing = case.get("operator") or {}
            # Only overwrite if the case has no operator yet, or if the
            # incoming identity matches the existing one (refresh).
            if not existing.get("username") and not existing.get("name"):
                patch["operator"] = {
                    "username": op_username,
                    "name": op_name or op_username,
                    "role": op_role,
                }
                patch["operator_username"] = op_username
                patch["operator_name"] = op_name or op_username
                patch["operator_label"] = (
                    f"{op_name} ({op_username})" if op_username and op_name and op_username != op_name
                    else (op_name or op_username)
                )

    return transition(
        case_id,
        action="CASE_SUBMITTED",
        by=by,
        patch=patch,
        note=f"{by} submitted the case to Supervisor Inbox for Approval 1.",
    )


# ─── Helpers ────────────────────────────────────────────────────────────────
def get_case(case_id: str) -> dict[str, Any] | None:
    return find_record(_CASES_FILE, "case_id", case_id)


def find_case_by_pr(pr_id: str) -> dict[str, Any] | None:
    for c in load(_CASES_FILE):
        if c.get("pr_id") == pr_id:
            return c
    return None


def list_cases(status: str | None = None, severity: str | None = None, stage: str | None = None) -> list[dict[str, Any]]:
    cases = load(_CASES_FILE)
    if status:
        cases = [c for c in cases if (c.get("status") or "").upper() == status.upper()]
    if severity:
        cases = [c for c in cases if (c.get("severity") or "").upper() == severity.upper()]
    if stage:
        cases = [c for c in cases if (c.get("stage") or "").upper() == stage.upper()]
    cases.sort(key=lambda c: c.get("created_at", ""), reverse=True)
    return cases


def _push_history(case: dict[str, Any], action: str, by: str, note: str | None = None) -> None:
    case.setdefault("history", []).append(
        {"action": action, "at": _now(), "by": by, "note": note}
    )


def _record_approval(case: dict[str, Any], stage: str, decision: str, by: str, note: str | None = None) -> None:
    case.setdefault("approvals", []).append(
        {"stage": stage, "decision": decision, "by": by, "at": _now(), "note": note}
    )


def transition(case_id: str, action: str, by: str, patch: dict[str, Any], note: str | None = None,
               approval: tuple[str, str] | None = None) -> dict[str, Any] | None:
    case = get_case(case_id)
    if case is None:
        return None
    case.update(patch)
    case["updated_at"] = _now()
    _push_history(case, action, by, note)
    if approval:
        stage, decision = approval
        _record_approval(case, stage, decision, by, note)
    update_record(_CASES_FILE, "case_id", case_id, case)
    return case


# ─── Dashboard aggregates ──────────────────────────────────────────────────
def dashboard_stats() -> dict[str, Any]:
    cases = load(_CASES_FILE)
    status_counts: dict[str, int] = {}
    severity_counts: dict[str, int] = {"LOW": 0, "MEDIUM": 0, "HIGH": 0}
    defect_counts: dict[str, int] = {}
    decision_counts = {"REPAIR": 0, "REPLACE": 0, "REJECT": 0, "RESUBMIT": 0}
    ewm_counts = {"FULL": 0, "PARTIAL": 0, "NONE": 0}

    for c in cases:
        status_counts[c.get("status", "UNKNOWN")] = status_counts.get(c.get("status", "UNKNOWN"), 0) + 1
        sev = (c.get("severity") or "").upper()
        if sev in severity_counts:
            severity_counts[sev] += 1
        d = (c.get("defect") or "unknown").lower()
        defect_counts[d] = defect_counts.get(d, 0) + 1
        dec = (c.get("decision") or "").upper()
        if dec in decision_counts:
            decision_counts[dec] += 1
        ewm = (c.get("ewm_status") or "").upper()
        if ewm in ewm_counts:
            ewm_counts[ewm] += 1

    maintenance = load("maintenance_orders.json")
    reservations = load("reservations.json")
    prs = load("purchase_requisitions.json")
    pos = load("purchase_orders.json")
    total_spend = round(sum(float(p.get("total_cost", 0)) for p in pos), 2)

    return {
        "total_cases": len(cases),
        "status_counts": status_counts,
        "severity_counts": severity_counts,
        "defect_counts": defect_counts,
        "decision_counts": decision_counts,
        "ewm_counts": ewm_counts,
        "maintenance_orders": len(maintenance),
        "reservations": len(reservations),
        "purchase_requisitions": len(prs),
        "purchase_orders": len(pos),
        "total_procurement_spend": total_spend,
    }
