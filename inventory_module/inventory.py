"""
Inventory + AI Supplier-Selection module (EWM layer).

Strict workflow API:

* ewm_check(component, required_qty)       → {"status": FULL|PARTIAL|NONE, ...}
* create_reservation(case, component, qty) → {"reservation_id": "RES-...", ...}
* ai_select_supplier(component, qty)       → {"supplier": {...}, "reason": "..."}

ewm_check returns:
    FULL    → on_hand >= required_qty
    PARTIAL → 0 < on_hand < required_qty
    NONE    → on_hand == 0
"""

from __future__ import annotations

import datetime as _dt
import json
import uuid
from pathlib import Path
from typing import Any

from backend.storage import append_record, load as load_json

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"


def _now() -> str:
    return _dt.datetime.now().isoformat(timespec="seconds")


def _load(name: str) -> Any:
    with open(DATA / name, encoding="utf-8") as f:
        return json.load(f)


def _norm(s: str) -> str:
    return (s or "").strip().lower()


def _resolve_item(component: str) -> tuple[str, dict[str, Any] | None]:
    """Look up a component in the inventory catalogue with fuzzy matching."""
    inventory = _load("inventory.json")
    comp = _norm(component)
    item = inventory.get(comp)
    if item is None:
        for key, val in inventory.items():
            if key in comp or comp in key:
                item = val
                comp = key
                break
    return comp, item


# ─────────────────────────────────────────────────────────────────────────────
# EWM availability check — FULL / PARTIAL / NONE
# ─────────────────────────────────────────────────────────────────────────────
def ewm_check(component: str, required_qty: int = 1) -> dict[str, Any]:
    """Return the EWM availability verdict for the requested component & qty."""
    comp, item = _resolve_item(component)
    required_qty = max(1, int(required_qty or 1))

    if item is None:
        return {
            "component": component,
            "required_qty": required_qty,
            "on_hand": 0,
            "status": "NONE",
            "location": "N/A",
            "unit_price": 0,
            "shortage_qty": required_qty,
            "reservable_qty": 0,
            "message": f"{component!r} is not catalogued in EWM — escalating to procurement.",
            "checked_at": _now(),
        }

    on_hand = int(item.get("quantity", 0))
    if on_hand >= required_qty:
        status = "FULL"
        reservable = required_qty
        shortage = 0
        msg = f"{on_hand} units of {comp} on hand at {item.get('location')} — reservation can cover the request in full."
    elif on_hand > 0:
        status = "PARTIAL"
        reservable = on_hand
        shortage = required_qty - on_hand
        msg = (
            f"Only {on_hand} of {required_qty} units of {comp} available at "
            f"{item.get('location')}. Reserving {on_hand}, raising PR for {shortage}."
        )
    else:
        status = "NONE"
        reservable = 0
        shortage = required_qty
        msg = f"Zero stock of {comp} — full procurement required."

    return {
        "component": comp,
        "required_qty": required_qty,
        "on_hand": on_hand,
        "status": status,
        "location": item.get("location", "N/A"),
        "unit_price": item.get("unit_price", 0),
        "reservable_qty": reservable,
        "shortage_qty": shortage,
        "message": msg,
        "checked_at": _now(),
    }


# ─────────────────────────────────────────────────────────────────────────────
# Reservation (stored to data/reservations.json + inventory decremented)
# ─────────────────────────────────────────────────────────────────────────────
def create_reservation(case: dict[str, Any], component: str, qty: int, location: str) -> dict[str, Any]:
    res_id = "RES-" + uuid.uuid4().hex[:6].upper()
    reservation = {
        "reservation_id": res_id,
        "case_id": case.get("case_id"),
        "component": component,
        "quantity": int(qty),
        "location": location,
        "status": "RESERVED",
        "created_at": _now(),
    }
    append_record("reservations.json", reservation)
    # Decrement the physical inventory so subsequent checks are accurate
    _decrement_inventory(component, qty)
    return reservation


def _decrement_inventory(component: str, qty: int) -> None:
    path = DATA / "inventory.json"
    with open(path, encoding="utf-8") as f:
        inv = json.load(f)
    comp = _norm(component)
    if comp in inv:
        inv[comp]["quantity"] = max(0, int(inv[comp].get("quantity", 0)) - int(qty))
        with open(path, "w", encoding="utf-8") as f:
            json.dump(inv, f, indent=2)


# ─────────────────────────────────────────────────────────────────────────────
# AI supplier selection (certified only, availability + lead + quality + proximity)
# ─────────────────────────────────────────────────────────────────────────────
def _match(supplier: dict[str, Any], component: str) -> bool:
    comp = _norm(component)
    for c in supplier.get("components", []):
        c = _norm(c)
        if c == comp or c in comp or comp in c:
            return True
    return False


def _composite_score(s: dict[str, Any], max_distance: float) -> float:
    """Blend quality, delivery speed, proximity, reliability into a 0-100 score."""
    quality = float(s.get("quality", 0))            # 0-10
    delivery = float(s.get("delivery_days", 30))    # lower is better
    distance = float(s.get("distance_km", 10000))
    reliability = float(s.get("reliability", 0.85)) # 0-1

    quality_score = quality * 10
    delivery_score = max(0.0, 100 - delivery * 4)
    distance_score = max(0.0, 100 - (distance / max_distance) * 100)
    reliability_score = reliability * 100
    return round(
        0.30 * quality_score
        + 0.25 * delivery_score
        + 0.20 * distance_score
        + 0.25 * reliability_score,
        2,
    )


def rank_suppliers(component: str) -> list[dict[str, Any]]:
    suppliers = _load("suppliers.json")
    # Only consider approved + certified suppliers for aerospace procurement
    pool0 = [s for s in suppliers if s.get("certified") and s.get("approved", True)]
    matches = [s for s in pool0 if _match(s, component)]
    pool = matches or pool0

    max_distance = max((s.get("distance_km", 1) for s in pool), default=1)

    scored = []
    for s in pool:
        entry = dict(s)
        entry["score"] = _composite_score(s, max_distance)
        scored.append(entry)
    scored.sort(key=lambda x: x["score"], reverse=True)
    return scored


def ai_select_supplier(component: str, qty: int = 1, urgency: str = "NORMAL") -> dict[str, Any]:
    """AI Supplier Selection layer.

    Pipeline:
      1. Filter approved/certified suppliers that carry this component.
      2. Rank them locally (quality + delivery + proximity + reliability).
      3. Ask Grok AI to pick the best one from the top candidates,
         considering cost, delivery urgency, reliability, and proximity.
      4. If Grok is unavailable or returns something unusable, fall back
         to the top locally-ranked supplier.

    Returns:
        {
          "supplier": {...},
          "quantity": int,
          "reason": "explanation",
          "ai_source": "grok" | "local_ranker",
          "alternatives": [...],
          "selected_at": "timestamp",
        }
    """
    ranked = rank_suppliers(component)
    if not ranked:
        return {"error": "No certified suppliers available for this component."}

    # Try Grok first (certified shortlist, top 5 candidates)
    try:
        from decision_engine import select_supplier_ai
        shortlist = ranked[:5]
        grok_pick = select_supplier_ai(component, qty, shortlist, urgency=urgency)
    except Exception:
        grok_pick = None

    if grok_pick:
        chosen = next(
            (s for s in ranked if s.get("supplier_id") == grok_pick["supplier_id"]),
            ranked[0],
        )
        reason = (
            f"Grok AI selected {chosen['name']} for {qty}× {component}. "
            f"{grok_pick.get('reasoning') or ''} "
            f"Signals: quality {chosen.get('quality')}/10, lead "
            f"{chosen.get('delivery_days')}d, {chosen.get('distance_km')} km, "
            f"reliability {chosen.get('reliability')}, composite score "
            f"{chosen.get('score')}/100."
        )
        ai_source = "grok"
    else:
        chosen = ranked[0]
        reason = (
            f"Rule-based ranker selected {chosen['name']} (certified) for "
            f"{qty}× {component}. Quality {chosen.get('quality')}/10, "
            f"lead {chosen.get('delivery_days')}d, "
            f"{chosen.get('distance_km')} km from plant, reliability "
            f"{chosen.get('reliability')}. Composite score "
            f"{chosen.get('score')}/100 across {len(ranked)} eligible suppliers."
        )
        ai_source = "local_ranker"

    return {
        "supplier": chosen,
        "quantity": int(qty),
        "reason": reason,
        "ai_source": ai_source,
        "alternatives": [s for s in ranked if s.get("supplier_id") != chosen.get("supplier_id")][:3],
        "selected_at": _now(),
    }


# ─────────────────────────────────────────────────────────────────────────────
# Back-compat helpers still used by simple UI endpoints
# ─────────────────────────────────────────────────────────────────────────────
def check_inventory(component: str) -> dict[str, Any]:
    return ewm_check(component, required_qty=1)


def select_supplier(component: str) -> dict[str, Any]:
    return ai_select_supplier(component, qty=1, urgency="NORMAL")
