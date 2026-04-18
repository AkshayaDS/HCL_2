"""
Decision engine (Rule-based + Explainable AI layer).

Takes the AI vision report and produces an explainable recommendation
(REPAIR vs REPLACE) with a risk level, human-readable reason, and a
detailed explanation trail listing which rules fired and why.

Inputs considered:
  * Defect severity      (LOW | MEDIUM | HIGH)
  * Model confidence     (0.0 - 1.0)
  * Component criticality (safety-critical rotating parts)
  * Non-repairable defect patterns (fracture, FOD impact, thermal fatigue
    crack, etc.)

This is pure rule-based logic, no external calls — safe to run offline.
The Grok AI layer (decision_engine/grok_ai.py) can be optionally invoked
on top of this engine for AI-assisted decision validation.
"""

from __future__ import annotations

from typing import Any

# ──────────────────────────────────────────────────────────────────────────
# Safety-critical components — a HIGH-severity finding on any of these
# should always trigger REPLACE (airworthiness policy).
# ──────────────────────────────────────────────────────────────────────────
_SAFETY_CRITICAL = {
    "turbine blade",
    "compressor blade",
    "fan blade",
    "blade",
    "disk",
    "rotor",
    "shaft",
    "combustor",
    "nozzle",
}

# ──────────────────────────────────────────────────────────────────────────
# Defect patterns that usually cannot be repaired and need replacement.
# ──────────────────────────────────────────────────────────────────────────
_UNREPAIRABLE_DEFECTS = {
    "thermal fatigue crack",
    "fatigue crack",
    "impact damage (fod)",
    "impact damage",
    "fod",
    "fracture",
    "crack propagation",
    "delamination",
}


def risk_score(severity: str, confidence: float, component: str) -> int:
    """Return a 0-100 risk score combining severity, confidence and criticality."""
    sev_weight = {"LOW": 20, "MEDIUM": 55, "HIGH": 90}.get(severity.upper(), 50)
    critical_bonus = 10 if (component or "").lower() in _SAFETY_CRITICAL else 0
    # confidence amplifies the severity signal
    score = int(sev_weight * (0.6 + 0.4 * max(0.0, min(1.0, confidence))) + critical_bonus)
    return max(0, min(100, score))


def _matches_unrepairable(defect: str) -> str | None:
    """Return the matching unrepairable pattern, or None."""
    defect = (defect or "").lower()
    for pattern in _UNREPAIRABLE_DEFECTS:
        if pattern in defect:
            return pattern
    return None


def recommend_decision(report: dict[str, Any]) -> dict[str, Any]:
    """Produce a recommendation with explainability.

    Returns:
        {
            "decision": "REPAIR" | "REPLACE",
            "risk_level": "LOW" | "MEDIUM" | "HIGH",
            "risk_score": int (0-100),
            "reason": "short technical explanation",
            "explanation": [ "rule-by-rule trace" ],
            "rules_fired": [ "RULE_ID", ... ],
            "estimated_downtime": "e.g. 4-6 hours",
            "safety_risk": "LOW" | "MEDIUM" | "HIGH",
            "inputs": { ... echo of what went in },
            "source": "rule_engine",
        }
    """
    component = str(report.get("component", "")).lower()
    defect = str(report.get("defect", "")).lower()
    severity = str(report.get("severity", "MEDIUM")).upper()
    confidence = float(report.get("confidence", 0.8))

    score = risk_score(severity, confidence, component)

    if score >= 75:
        risk_level = "HIGH"
    elif score >= 45:
        risk_level = "MEDIUM"
    else:
        risk_level = "LOW"

    is_critical = component in _SAFETY_CRITICAL
    unrepairable_match = _matches_unrepairable(defect)

    explanation: list[str] = []
    rules_fired: list[str] = []

    # Explainability trace — always list what was considered
    explanation.append(
        f"Severity classified as {severity} (weight={ {'LOW':20,'MEDIUM':55,'HIGH':90}.get(severity,50) })."
    )
    explanation.append(
        f"AI model confidence = {confidence:.2f}. Confidence amplifies severity signal."
    )
    if is_critical:
        explanation.append(
            f"Component '{component}' is on the SAFETY-CRITICAL list "
            f"(rotating / flight-critical part) → +10 risk bonus."
        )
        rules_fired.append("SAFETY_CRITICAL_COMPONENT")
    else:
        explanation.append(
            f"Component '{component}' is not on the safety-critical list."
        )
    if unrepairable_match:
        explanation.append(
            f"Defect '{defect}' matches NON-REPAIRABLE pattern "
            f"'{unrepairable_match}' — replacement strongly indicated."
        )
        rules_fired.append("NON_REPAIRABLE_DEFECT_PATTERN")
    explanation.append(f"Composite risk score = {score}/100 → risk level {risk_level}.")

    # ──────────────────────────────────────────────────────────────────
    # Decision rules (evaluated in order — first match wins)
    # ──────────────────────────────────────────────────────────────────
    if severity == "HIGH" and is_critical:
        decision = "REPLACE"
        rules_fired.append("RULE_HIGH_SEVERITY_ON_CRITICAL_COMPONENT")
        reason = (
            f"{component.title()} is a safety-critical rotating component and the "
            f"{defect} is severe — replacement is mandated by airworthiness policy."
        )
        downtime = "12-24 hours"
        safety_risk = "HIGH"
    elif unrepairable_match:
        decision = "REPLACE"
        rules_fired.append("RULE_UNREPAIRABLE_DEFECT")
        reason = (
            f"The detected defect ({defect}) is not reliably repairable and "
            f"poses a propagation risk under load — replacement recommended."
        )
        downtime = "8-16 hours"
        safety_risk = "HIGH" if severity == "HIGH" else "MEDIUM"
    elif severity == "HIGH":
        decision = "REPLACE"
        rules_fired.append("RULE_HIGH_SEVERITY_EXCEEDS_REPAIR_THRESHOLD")
        reason = (
            f"High-severity {defect} on {component} exceeds repair threshold; "
            f"replacement is the lower-risk option."
        )
        downtime = "6-10 hours"
        safety_risk = "HIGH"
    elif severity == "MEDIUM":
        decision = "REPAIR"
        rules_fired.append("RULE_MEDIUM_SEVERITY_WITHIN_REPAIR_LIMITS")
        reason = (
            f"Medium-severity {defect} is within repairable limits for "
            f"{component}. Grinding, polishing or coating restoration should "
            f"return the part to service."
        )
        downtime = "3-6 hours"
        safety_risk = "MEDIUM"
    else:
        decision = "REPAIR"
        rules_fired.append("RULE_LOW_SEVERITY_IN_SITU_MAINTENANCE")
        reason = (
            f"Low-severity {defect} on {component} can be addressed with "
            f"routine in-situ maintenance."
        )
        downtime = "1-3 hours"
        safety_risk = "LOW"

    explanation.append(f"Final decision: {decision} (rule → {rules_fired[-1]}).")

    return {
        "decision": decision,
        "risk_level": risk_level,
        "risk_score": score,
        "reason": reason,
        "explanation": explanation,
        "rules_fired": rules_fired,
        "estimated_downtime": downtime,
        "safety_risk": safety_risk,
        "inputs": {
            "component": component,
            "defect": defect,
            "severity": severity,
            "confidence": confidence,
            "is_safety_critical": is_critical,
            "unrepairable_pattern_matched": unrepairable_match,
        },
        "source": "rule_engine",
    }
