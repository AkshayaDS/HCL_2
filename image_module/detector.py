"""
Real-time AI damage detection for RTX aerospace components.

Primary path  : Groq vision model (meta-llama/llama-4-scout-17b-16e-instruct).
                The prompt is engineered so the model first decides whether
                the image is actually an aircraft / engine / aerospace
                component, and ONLY THEN returns a full diagnostic report.

Rejection path: If the model says the image is NOT an aircraft component,
                a ``NotAnAircraftComponentError`` is raised. The backend
                catches this and returns HTTP 422 so the operator page can
                ask the user to resubmit a valid image — nothing is ever
                filed into SAP from a non-aircraft photo.

Fallback path : If the Groq call itself blows up (no API key, rate limit,
                network outage) we synthesise a plausible result from a
                deterministic hash of the image bytes so a live demo never
                freezes. This fallback is ONLY used when the call itself
                errored — it is never used to "approve" an image the model
                explicitly rejected.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import random
import re
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")

GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "").strip()
GROQ_MODEL = os.environ.get("GROQ_MODEL", "meta-llama/llama-4-scout-17b-16e-instruct")

_COMPONENTS = [
    "turbine blade",
    "compressor blade",
    "fan blade",
    "bearing",
    "shaft",
    "combustor",
    "nozzle",
    "seal",
    "bracket",
]
_DEFECTS = [
    "hairline crack",
    "thermal fatigue crack",
    "surface corrosion",
    "leading-edge erosion",
    "impact damage (FOD)",
    "coating delamination",
    "wear pitting",
]
_EQUIPMENT_POOL = ["EQ-1234", "EQ-2201", "EQ-3310"]


# ─── Exceptions ────────────────────────────────────────────────────────────
class NotAnAircraftComponentError(Exception):
    """The uploaded image does not depict an aircraft / engine component."""

    def __init__(self, reason: str = "", observed_subject: str = ""):
        self.reason = reason or "The image does not appear to be an aircraft component."
        self.observed_subject = observed_subject or "unknown"
        super().__init__(self.reason)


# ─── Prompt ────────────────────────────────────────────────────────────────
_PROMPT = """\
You are an industrial maintenance AI for RTX aerospace components (aircraft
engines, gas turbines, airframe parts, landing gear, auxiliary power units).

Your first job is to DECIDE whether the image actually shows an aircraft,
aerospace, or gas-turbine component. Photos of people, pets, food, cars,
buildings, screenshots, random objects, memes, text-only images, etc. are
NOT aircraft components and must be rejected.

Return ONLY valid minified JSON (no markdown, no code fences, no commentary)
with exactly these keys:

{
  "is_aircraft_component": true | false,
  "observed_subject": "<1-4 words describing what you actually see>",
  "rejection_reason": "<empty string if aircraft component, else short user-friendly reason>",
  "component": "<short canonical name, e.g. 'turbine blade'. Empty if not aircraft.>",
  "defect": "<type of damage, e.g. 'hairline crack'. Empty if no damage or not aircraft.>",
  "damaged_area": "<short location, e.g. 'leading edge'. Empty if not aircraft.>",
  "severity": "LOW" | "MEDIUM" | "HIGH" | "NONE",
  "confidence": <float 0..1 — your confidence in the classification>,
  "ai_report": "<one or two sentence technical explanation, or rejection explanation>"
}

Rules:
- If is_aircraft_component is false, set component/defect/damaged_area to "" and severity to "NONE".
- If the component LOOKS aerospace but you cannot determine a defect, set defect to "no visible damage" and severity to "LOW".
- Be strict: if unsure, prefer is_aircraft_component=false with a clear rejection_reason.
- Confidence must reflect YOUR certainty, not an optimistic default.
"""


# ─── Deterministic mock (used only for network/key failures) ──────────────
def _hash_based_mock(image_path: str) -> dict[str, Any]:
    with open(image_path, "rb") as f:
        digest = hashlib.sha256(f.read()).digest()
    seed = int.from_bytes(digest[:4], "big")
    rng = random.Random(seed)

    component = rng.choice(_COMPONENTS)
    defect = rng.choice(_DEFECTS)
    sev_roll = rng.random()
    severity = "HIGH" if sev_roll > 0.66 else "MEDIUM" if sev_roll > 0.33 else "LOW"
    confidence = round(0.78 + rng.random() * 0.2, 2)
    area = rng.choice(["leading edge", "trailing edge", "root", "tip", "mid-span"])
    report = (
        f"Offline vision fallback detected a {defect} on the {area} of the "
        f"{component}. Severity rated {severity} based on visible damage extent. "
        f"Recommend physical inspection to confirm AI finding."
    )

    return {
        "component": component,
        "defect": defect,
        "damaged_area": area,
        "severity": severity,
        "confidence": confidence,
        "ai_report": report,
        "equipment_id": _EQUIPMENT_POOL[seed % len(_EQUIPMENT_POOL)],
        "source": "offline-mock",
    }


# ─── Groq call ────────────────────────────────────────────────────────────
def _infer_mime(image_path: str) -> str:
    ext = Path(image_path).suffix.lower()
    return {
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".png": "image/png",
        ".webp": "image/webp",
        ".gif": "image/gif",
    }.get(ext, "image/jpeg")


def _call_groq(image_path: str) -> dict[str, Any]:
    """Invoke Groq Llama-4 Scout and parse the structured result.

    Raises ``NotAnAircraftComponentError`` if the model rejects the image.
    Raises any other exception upstream to let ``detect_damage`` decide
    whether to fall back to the offline mock.
    """
    import groq  # lazy import

    client = groq.Groq(api_key=GROQ_API_KEY)

    with open(image_path, "rb") as f:
        image_bytes = f.read()
    image_b64 = base64.b64encode(image_bytes).decode("utf-8")
    mime = _infer_mime(image_path)

    response = client.chat.completions.create(
        model=GROQ_MODEL,
        max_tokens=500,
        temperature=0.1,
        messages=[
            {
                "role": "user",
                "content": [
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:{mime};base64,{image_b64}"},
                    },
                    {"type": "text", "text": _PROMPT},
                ],
            }
        ],
    )
    raw = (response.choices[0].message.content or "").strip()
    # Strip any accidental markdown fences the model leaks through.
    raw = re.sub(r"^```(?:json)?|```$", "", raw, flags=re.MULTILINE).strip()

    # Extract the first JSON object even if the model appends stray text.
    match = re.search(r"\{.*\}", raw, re.DOTALL)
    if match:
        raw = match.group(0)

    data = json.loads(raw)

    # ── Aircraft gate ─────────────────────────────────────────────────
    is_aircraft = data.get("is_aircraft_component")
    if isinstance(is_aircraft, str):
        is_aircraft = is_aircraft.strip().lower() in {"true", "yes", "1"}
    if not is_aircraft:
        raise NotAnAircraftComponentError(
            reason=str(data.get("rejection_reason") or "").strip()
            or "The AI could not recognise this as an aircraft component.",
            observed_subject=str(data.get("observed_subject") or "").strip()
            or "unknown subject",
        )

    # ── Normalise fields ──────────────────────────────────────────────
    sev = str(data.get("severity", "MEDIUM")).upper()
    if sev not in {"LOW", "MEDIUM", "HIGH"}:
        sev = "MEDIUM"
    data["severity"] = sev

    try:
        conf = float(data.get("confidence", 0.85))
    except (TypeError, ValueError):
        conf = 0.85
    data["confidence"] = max(0.0, min(1.0, conf))

    data["component"] = (str(data.get("component") or "").strip()
                         or "aerospace component")
    data["defect"] = (str(data.get("defect") or "").strip()
                      or "no visible damage")
    data["damaged_area"] = (str(data.get("damaged_area") or "").strip()
                            or "unspecified region")
    data.setdefault("ai_report", "AI analysis completed.")

    # Derive a deterministic equipment tag from the image bytes so the same
    # photo always ties to the same equipment record during a demo.
    digest = hashlib.sha256(image_bytes).digest()
    data["equipment_id"] = _EQUIPMENT_POOL[digest[0] % len(_EQUIPMENT_POOL)]
    data["source"] = "groq-llama4-scout"

    # Drop the gate fields from the case record — they were only for the gate.
    data.pop("is_aircraft_component", None)
    data.pop("observed_subject", None)
    data.pop("rejection_reason", None)
    return data


# ─── Public entrypoint ────────────────────────────────────────────────────
def detect_damage(image_path: str) -> dict[str, Any]:
    """Return a structured damage report for the given image.

    Raises:
        NotAnAircraftComponentError: if the Groq model classifies the image
            as NOT an aircraft / aerospace component. Callers (the Flask
            endpoint) must translate this into an HTTP 422 so the operator
            is asked to resubmit a valid image.

    Never raises any other exception — on transport failure the function
    falls back to a deterministic offline mock so the demo keeps running.
    """
    if not GROQ_API_KEY:
        mock = _hash_based_mock(image_path)
        mock["ai_report"] += " (GROQ_API_KEY not configured — using offline fallback.)"
        return mock

    try:
        return _call_groq(image_path)
    except NotAnAircraftComponentError:
        # Propagate so the API layer can reject the upload.
        raise
    except Exception as exc:  # pragma: no cover
        mock = _hash_based_mock(image_path)
        mock["ai_report"] += f" (Groq error: {type(exc).__name__}: {exc})"
        return mock
