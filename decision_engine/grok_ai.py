"""
Grok AI integration layer.

Provides two AI-assisted capabilities on top of the rule-based engine:

  1. validate_decision(report, rule_decision)
        → Sends the AI vision report and the rule-engine's recommendation
          to Grok and asks it to validate / comment. Returns a dict with
          a verdict ("AGREE" | "OVERRIDE_REPAIR" | "OVERRIDE_REPLACE"),
          a confidence score, and the AI's reasoning.

  2. select_supplier_ai(component, qty, shortlist)
        → Sends a shortlist of certified suppliers (from suppliers.json)
          to Grok and asks it to pick the best one considering cost,
          delivery urgency, reliability and proximity. Returns the chosen
          supplier_id plus a rationale.

Both functions degrade gracefully when GROK_API_KEY is not set or the
network call fails — they return a structured fallback result and the
caller keeps working from the rule-engine / composite-score pick.

Environment variables:
  * GROK_API_KEY   — Grok (xAI) API key
  * GROK_MODEL     — defaults to "grok-2-latest"
  * GROK_BASE_URL  — defaults to "https://api.x.ai/v1"
"""

from __future__ import annotations

import json
import os
from typing import Any

try:
    import requests  # type: ignore
except Exception:  # pragma: no cover
    requests = None  # requests may not be installed in minimal envs


_DEFAULT_MODEL = os.environ.get("GROK_MODEL", "grok-2-latest")
_DEFAULT_BASE = os.environ.get("GROK_BASE_URL", "https://api.x.ai/v1")


def _grok_available() -> bool:
    return bool(os.environ.get("GROK_API_KEY")) and requests is not None


def _grok_chat(system: str, user: str, timeout: float = 15.0) -> str | None:
    """Call Grok and return the text content. None on any failure."""
    if not _grok_available():
        return None
    try:
        resp = requests.post(
            f"{_DEFAULT_BASE}/chat/completions",
            headers={
                "Authorization": f"Bearer {os.environ['GROK_API_KEY']}",
                "Content-Type": "application/json",
            },
            json={
                "model": _DEFAULT_MODEL,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                "temperature": 0.2,
                "max_tokens": 500,
            },
            timeout=timeout,
        )
        resp.raise_for_status()
        data = resp.json()
        return data["choices"][0]["message"]["content"]
    except Exception:
        return None


def _extract_json(text: str) -> dict[str, Any] | None:
    """Best-effort JSON extraction from an LLM response."""
    if not text:
        return None
    text = text.strip()
    # strip markdown fences if present
    if text.startswith("```"):
        text = text.strip("`")
        if text.lower().startswith("json"):
            text = text[4:]
    # find first { ... last }
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end <= start:
        return None
    try:
        return json.loads(text[start : end + 1])
    except Exception:
        return None


# ──────────────────────────────────────────────────────────────────────────
# 1) Decision validation
# ──────────────────────────────────────────────────────────────────────────
def validate_decision(report: dict[str, Any], rule_decision: dict[str, Any]) -> dict[str, Any]:
    """Ask Grok to validate the rule-engine's REPAIR/REPLACE recommendation.

    Returns:
      {
        "verdict": "AGREE" | "OVERRIDE_REPAIR" | "OVERRIDE_REPLACE",
        "final_decision": "REPAIR" | "REPLACE",
        "confidence": float 0-1,
        "reasoning": str,
        "source": "grok" | "fallback",
      }
    """
    rule_label = rule_decision.get("decision")
    fallback = {
        "verdict": "AGREE",
        "final_decision": rule_label,
        "confidence": float(rule_decision.get("risk_score", 60)) / 100.0,
        "reasoning": (
            "Grok API unavailable — rule-engine decision accepted as-is. "
            "Rule basis: " + (rule_decision.get("reason") or "")
        ),
        "source": "fallback",
    }

    if not _grok_available():
        return fallback

    system = (
        "You are an aerospace maintenance engineer validating automated "
        "maintenance decisions for an RTX/Raytheon-style Plant Maintenance "
        "platform. You must respond with STRICT JSON only — no prose."
    )
    user = (
        "Vision AI report:\n"
        f"{json.dumps({k: report.get(k) for k in ('component','defect','severity','confidence','damaged_area','ai_report')}, indent=2)}\n\n"
        "Rule-engine recommendation:\n"
        f"{json.dumps({'decision': rule_decision.get('decision'), 'reason': rule_decision.get('reason'), 'rules_fired': rule_decision.get('rules_fired'), 'risk_score': rule_decision.get('risk_score'), 'safety_risk': rule_decision.get('safety_risk')}, indent=2)}\n\n"
        "Validate this recommendation. Reply with JSON of the form:\n"
        "{\n"
        '  "verdict": "AGREE" | "OVERRIDE_REPAIR" | "OVERRIDE_REPLACE",\n'
        '  "final_decision": "REPAIR" | "REPLACE",\n'
        '  "confidence": 0.0-1.0,\n'
        '  "reasoning": "one or two sentences"\n'
        "}"
    )
    text = _grok_chat(system, user)
    parsed = _extract_json(text or "")
    if not parsed:
        return fallback

    verdict = str(parsed.get("verdict", "AGREE")).upper()
    final = str(parsed.get("final_decision", rule_label or "REPAIR")).upper()
    if final not in ("REPAIR", "REPLACE"):
        final = rule_label or "REPAIR"
    try:
        conf = float(parsed.get("confidence", 0.7))
    except Exception:
        conf = 0.7
    return {
        "verdict": verdict if verdict in ("AGREE", "OVERRIDE_REPAIR", "OVERRIDE_REPLACE") else "AGREE",
        "final_decision": final,
        "confidence": max(0.0, min(1.0, conf)),
        "reasoning": str(parsed.get("reasoning", "") or ""),
        "source": "grok",
    }


# ──────────────────────────────────────────────────────────────────────────
# 2) AI-driven supplier selection
# ──────────────────────────────────────────────────────────────────────────
def select_supplier_ai(
    component: str,
    qty: int,
    shortlist: list[dict[str, Any]],
    urgency: str = "NORMAL",
) -> dict[str, Any] | None:
    """Ask Grok to pick the best supplier from a shortlist.

    `shortlist` is a list of supplier dicts already filtered to certified/
    approved suppliers that carry the component. Returns:

        {
          "supplier_id": "SUP-00X",
          "reasoning": "...",
          "source": "grok",
        }

    or None if Grok is unavailable / returned something unusable. The
    caller should then fall back to its own scoring pick.
    """
    if not _grok_available() or not shortlist:
        return None

    # Keep the prompt compact — just the signals Grok needs
    compact = [
        {
            "supplier_id": s.get("supplier_id"),
            "name": s.get("name"),
            "location": s.get("location"),
            "distance_km": s.get("distance_km"),
            "delivery_days": s.get("delivery_days"),
            "cost_multiplier": s.get("cost_multiplier"),
            "reliability": s.get("reliability"),
            "on_time_delivery_pct": s.get("on_time_delivery_pct"),
            "availability": s.get("availability"),
            "stock_level": s.get("stock_level"),
            "quality": s.get("quality"),
            "approved": s.get("approved"),
            "contract_type": s.get("contract_type"),
        }
        for s in shortlist
    ]

    system = (
        "You are an aerospace procurement AI for an RTX/Raytheon-style "
        "Plant Maintenance platform. Pick the single best supplier from a "
        "shortlist, considering: cost optimization, delivery urgency, "
        "supplier reliability, and location proximity. Prefer approved "
        "suppliers with IN_STOCK availability and strong on-time delivery. "
        "Respond with STRICT JSON only."
    )
    user = (
        f"Component needed: {component}\n"
        f"Quantity: {qty}\n"
        f"Urgency: {urgency}\n\n"
        f"Shortlist (JSON):\n{json.dumps(compact, indent=2)}\n\n"
        "Return JSON of the form:\n"
        "{\n"
        '  "supplier_id": "SUP-00X",\n'
        '  "reasoning": "why this supplier beats the others"\n'
        "}"
    )
    text = _grok_chat(system, user)
    parsed = _extract_json(text or "")
    if not parsed:
        return None

    sid = str(parsed.get("supplier_id", "")).strip()
    if not sid:
        return None
    # confirm the chosen id is actually in the shortlist
    if not any(s.get("supplier_id") == sid for s in shortlist):
        return None

    return {
        "supplier_id": sid,
        "reasoning": str(parsed.get("reasoning", "") or ""),
        "source": "grok",
    }
