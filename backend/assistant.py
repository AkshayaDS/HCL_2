"""
Conversational AI assistant for the HCL AI Force platform.

Routes every chat turn through Groq (same provider used for the vision
detector) using the full message history passed by the frontend, so the
assistant actually holds context across turns.

Public API:
    chat(messages, context=None) -> dict
        messages : list of {"role": "user"|"assistant", "content": str}
        context  : optional dict with:
                     role          - "operator" | "supervisor" | "guest"
                     page          - "home" | "operator" | "supervisor" | "login"
                     user          - { username, name, role, ... }
                     current_case  - partial case payload, if any
        returns  : {
            "reply":   str,
            "source":  "groq" | "fallback",
            "model":   str,
            "usage":   dict (when available),
        }

When the GROQ_API_KEY is missing or the upstream call fails, the module
falls back to a deterministic canned response so the widget never leaves
the user staring at a silent error.
"""

from __future__ import annotations

import json
import os
from typing import Any, Iterable

try:
    import requests  # type: ignore
except Exception:  # pragma: no cover
    requests = None


# ── Config (env-driven) ─────────────────────────────────────────────────────
# Groq deprecates models every few months. We try a preferred model first
# then fall through a chain — the first one that returns 2xx wins.
_ENV_PRIMARY = os.environ.get("GROQ_CHAT_MODEL") or ""
_DEFAULT_CHAIN = [
    "llama-3.3-70b-versatile",
    "llama-3.1-70b-versatile",
    "llama-3.1-8b-instant",
    "llama3-70b-8192",
    "llama3-8b-8192",
    "mixtral-8x7b-32768",
]
MODEL_CHAIN = ([_ENV_PRIMARY] + _DEFAULT_CHAIN) if _ENV_PRIMARY else _DEFAULT_CHAIN
BASE_URL = os.environ.get("GROQ_BASE_URL", "https://api.groq.com/openai/v1")
MAX_HISTORY = 20   # trim conversation to the last N turns before sending


def _groq_available() -> bool:
    return bool(os.environ.get("GROQ_API_KEY")) and requests is not None


# ── ElevenLabs voice synthesis ──────────────────────────────────────────────
ELEVEN_BASE = os.environ.get("ELEVENLABS_BASE_URL", "https://api.elevenlabs.io/v1")
ELEVEN_DEFAULT_VOICE = os.environ.get("ELEVENLABS_VOICE_ID", "21m00Tcm4TlvDq8ikWAM")
ELEVEN_DEFAULT_MODEL = os.environ.get("ELEVENLABS_MODEL", "eleven_turbo_v2_5")


def elevenlabs_available() -> bool:
    return bool(os.environ.get("ELEVENLABS_API_KEY")) and requests is not None


def tts_stream(text: str, voice_id: str | None = None, model_id: str | None = None):
    """Synthesise speech via ElevenLabs and return a streaming response
    object from `requests.post(..., stream=True)`. The caller is expected
    to pipe the raw audio/mpeg bytes back to the client.

    Returns (response, mime) on success or (None, error_message).
    """
    if not elevenlabs_available():
        return None, "elevenlabs_not_configured"
    key = os.environ["ELEVENLABS_API_KEY"]
    voice = (voice_id or ELEVEN_DEFAULT_VOICE).strip() or ELEVEN_DEFAULT_VOICE
    model = (model_id or ELEVEN_DEFAULT_MODEL).strip() or ELEVEN_DEFAULT_MODEL

    # Trim very long text — ElevenLabs bills per character. Voice replies
    # should be short anyway because the LLM system prompt enforces brevity.
    clean = (text or "").strip()
    if not clean:
        return None, "empty_text"
    if len(clean) > 2500:
        clean = clean[:2500]

    url = f"{ELEVEN_BASE}/text-to-speech/{voice}/stream?optimize_streaming_latency=3"
    headers = {
        "xi-api-key": key,
        "Accept": "audio/mpeg",
        "Content-Type": "application/json",
    }
    payload = {
        "text": clean,
        "model_id": model,
        "voice_settings": {
            "stability": 0.4,
            "similarity_boost": 0.75,
            "style": 0.25,
            "use_speaker_boost": True,
        },
    }
    try:
        resp = requests.post(url, headers=headers, json=payload, stream=True, timeout=30)
    except Exception as exc:
        return None, f"upstream_error: {type(exc).__name__}"
    if resp.status_code >= 400:
        try:
            body = resp.json()
            detail = (body.get("detail") or {}).get("message") or body.get("detail") or resp.text
        except Exception:
            detail = resp.text[:200]
        resp.close()
        return None, f"elevenlabs_http_{resp.status_code}: {detail}"
    return resp, "audio/mpeg"


def list_voices() -> dict:
    """List the voices available to this ElevenLabs account. Used by the
    voice assistant widget to populate the voice picker."""
    if not elevenlabs_available():
        return {"voices": [], "error": "elevenlabs_not_configured"}
    try:
        r = requests.get(
            f"{ELEVEN_BASE}/voices",
            headers={"xi-api-key": os.environ["ELEVENLABS_API_KEY"]},
            timeout=10,
        )
        r.raise_for_status()
        data = r.json()
        voices = []
        for v in data.get("voices", []) or []:
            voices.append({
                "voice_id": v.get("voice_id"),
                "name": v.get("name"),
                "labels": v.get("labels") or {},
                "preview_url": v.get("preview_url"),
                "category": v.get("category"),
            })
        return {"voices": voices, "default_voice_id": ELEVEN_DEFAULT_VOICE}
    except Exception as exc:
        return {"voices": [], "error": f"{type(exc).__name__}: {exc}"}


# ── System prompt ───────────────────────────────────────────────────────────
_BASE_SYSTEM = """You are the HCL AI Force in-app assistant.

HCL AI Force is an AI-driven SAP Maintenance & Procurement platform for
aviation MRO (maintenance, repair, overhaul). Every uploaded component
photo runs through a Vision AI (Llama-4 Scout on Groq), produces an
AI-validated Decision Engine recommendation (REPAIR / REPLACE / REJECT /
RESUBMIT), and — on the supervisor's approval — creates SAP PM
maintenance orders, EWM reservations, AI-selected supplier PRs and POs.

Workflow summary:
  Image → AI detect → Supervisor Inbox (Approval 1)
    ├─ REPAIR  → SAP Maintenance Order UNDER_REPAIR (END)
    └─ REPLACE → EWM check (FULL | PARTIAL | NONE)
                  FULL     → Reservation (END)
                  PARTIAL  → Reservation + PR for the shortage
                  NONE     → PR for the full quantity
                             → Supervisor Approval 2 (PR → PO)
                             → PROCUREMENT_COMPLETED

Roles:
  Operator   — captures photos, submits inspection cases.
  Supervisor — reviews cases, approves Repair/Replace/Reject/Resubmit,
               approves PRs so the PO auto-issues.

Guidelines:
  * Keep responses concise — 1-3 short paragraphs or a crisp bulleted
    list. Never produce an essay.
  * Cite workflow states by their exact name (e.g. PR_PENDING_APPROVAL).
  * When the user asks how to do something, give step-by-step guidance
    tailored to their role / page.
  * You may reference context fields (case_id, component, defect,
    severity, decision, supplier, pr_id, etc.) when they are supplied.
  * If a question is outside the platform's scope (e.g. general coding
    help, personal advice), politely redirect.
"""


def _context_block(ctx: dict[str, Any] | None) -> str:
    if not ctx:
        return "No additional context supplied."
    lines: list[str] = []
    role = (ctx.get("role") or "guest").lower()
    page = (ctx.get("page") or "").lower()
    user = ctx.get("user") or {}
    if user:
        lines.append(f"Signed-in user: {user.get('name') or user.get('username') or 'anonymous'} "
                     f"({user.get('username') or '—'}) · role={user.get('role') or role}")
    if role:
        lines.append(f"Current role context: {role}")
    if page:
        lines.append(f"Current page: {page}")

    cc = ctx.get("current_case") or {}
    if cc:
        rec = cc.get("recommendation") or {}
        keys = [
            ("case_id", "Case"),
            ("component", "Component"),
            ("defect", "Defect"),
            ("damaged_area", "Area"),
            ("severity", "Severity"),
            ("status", "Status"),
            ("stage", "Stage"),
            ("decision", "Decision"),
            ("ewm_status", "EWM"),
            ("reservation_id", "Reservation"),
            ("pr_id", "PR"),
            ("pr_status", "PR status"),
            ("po_id", "PO"),
            ("operator_label", "Filed by"),
        ]
        frag = ", ".join(f"{label}={cc.get(k)}" for k, label in keys if cc.get(k))
        if frag:
            lines.append(f"THE USER IS CURRENTLY VIEWING: {frag}")
        if rec.get("decision"):
            lines.append(
                f"AI recommendation for this case: {rec.get('decision')} "
                f"(risk {rec.get('risk_level') or '—'}, safety {rec.get('safety_risk') or '—'})"
            )

    vs = ctx.get("visible_stats") or {}
    if vs:
        lines.append(
            f"Dashboard on screen: {vs.get('total_cases')} cases · "
            f"MO {vs.get('maintenance_orders')} · PR {vs.get('purchase_requisitions')} · PO {vs.get('purchase_orders')} · "
            f"status {vs.get('status_counts')} · severity {vs.get('severity_counts')} · decisions {vs.get('decision_counts')}"
        )
    vcs = ctx.get("visible_case_ids") or []
    if vcs:
        lines.append(f"Case IDs visible on screen: {', '.join(vcs[:12])}")
    return "\n".join(lines) if lines else "No additional context supplied."


def _platform_snapshot() -> dict[str, Any]:
    """Build a compact live snapshot of the platform state — cases,
    inventory, suppliers, PRs, POs, MOs, reservations — so the AI can
    answer data questions ("how many cases are pending?", "what's the
    stock of turbine blades?", "who's the top-scored supplier for X?")
    without needing tool calls.

    Reads from the authoritative storage layer on every chat turn so
    answers are always fresh.
    """
    try:
        from backend.storage import load
    except Exception:
        return {}

    def _safe(name: str, default):
        try:
            return load(name)
        except Exception:
            return default

    cases = _safe("cases.json", []) or []
    mos = _safe("maintenance_orders.json", []) or []
    pos = _safe("purchase_orders.json", []) or []
    prs = _safe("purchase_requisitions.json", []) or []
    reservations = _safe("reservations.json", []) or []
    inv = _safe("inventory.json", {}) or {}
    suppliers = _safe("suppliers.json", []) or []

    status_counts: dict[str, int] = {}
    decision_counts: dict[str, int] = {}
    severity_counts: dict[str, int] = {"LOW": 0, "MEDIUM": 0, "HIGH": 0}
    ewm_counts: dict[str, int] = {"FULL": 0, "PARTIAL": 0, "NONE": 0}
    for c in cases:
        s = (c.get("status") or "UNKNOWN").upper()
        status_counts[s] = status_counts.get(s, 0) + 1
        d = (c.get("decision") or "").upper()
        if d:
            decision_counts[d] = decision_counts.get(d, 0) + 1
        sev = (c.get("severity") or "").upper()
        if sev in severity_counts:
            severity_counts[sev] += 1
        ewm = (c.get("ewm_status") or "").upper()
        if ewm in ewm_counts:
            ewm_counts[ewm] += 1

    recent_cases = sorted(cases, key=lambda c: c.get("updated_at") or c.get("created_at") or "", reverse=True)[:8]
    pending_prs = [p for p in prs if (p.get("status") or "").upper() == "PENDING_APPROVAL"]

    total_spend = 0.0
    for p in pos:
        try:
            total_spend += float(p.get("total_cost") or 0)
        except Exception:
            pass

    return {
        "counts": {
            "cases": len(cases),
            "maintenance_orders": len(mos),
            "purchase_requisitions": len(prs),
            "pending_prs": len(pending_prs),
            "purchase_orders": len(pos),
            "reservations": len(reservations),
        },
        "status_counts": status_counts,
        "decision_counts": decision_counts,
        "severity_counts": severity_counts,
        "ewm_counts": ewm_counts,
        "total_spend": round(total_spend, 2),
        "recent_cases": recent_cases,
        "inventory": inv,
        "suppliers": suppliers,
        "pending_prs": pending_prs[:6],
    }


def _snapshot_block(snap: dict[str, Any]) -> str:
    if not snap:
        return ""
    counts = snap.get("counts") or {}
    parts: list[str] = ["— LIVE PLATFORM STATE —"]

    parts.append(
        f"Cases: {counts.get('cases', 0)} total · "
        f"status {snap.get('status_counts') or {}} · "
        f"decisions {snap.get('decision_counts') or {}} · "
        f"severity {snap.get('severity_counts') or {}} · "
        f"EWM {snap.get('ewm_counts') or {}}"
    )
    parts.append(
        f"SAP · Maintenance Orders: {counts.get('maintenance_orders', 0)} · "
        f"PRs: {counts.get('purchase_requisitions', 0)} ({counts.get('pending_prs', 0)} pending approval) · "
        f"POs: {counts.get('purchase_orders', 0)} · "
        f"Reservations: {counts.get('reservations', 0)} · "
        f"Total spend: ${snap.get('total_spend', 0):,.2f}"
    )

    rc = snap.get("recent_cases") or []
    if rc:
        parts.append("\nRecent cases (newest first):")
        for c in rc[:8]:
            parts.append(
                f"  - {c.get('case_id')} · {c.get('component') or '—'} / "
                f"{c.get('defect') or '—'} [{(c.get('severity') or '—')}] "
                f"status={c.get('status') or '—'} decision={c.get('decision') or '—'} "
                f"operator={c.get('operator_label') or '—'}"
            )

    inv = snap.get("inventory") or {}
    if inv:
        parts.append("\nEWM inventory on hand:")
        for comp, data in inv.items():
            parts.append(
                f"  - {comp}: {data.get('quantity', 0)} units @ {data.get('location', '—')} "
                f"(unit ${data.get('unit_price', 0)})"
            )

    sups = snap.get("suppliers") or []
    if sups:
        parts.append("\nCertified supplier network (top 6):")
        for s in sups[:6]:
            if not s.get("certified"):
                continue
            parts.append(
                f"  - {s.get('name')} · {s.get('supplier_id')} · quality {s.get('quality')}/10 · "
                f"lead {s.get('delivery_days')}d · {s.get('distance_km')}km · "
                f"components: {', '.join(s.get('components') or [])}"
            )

    pprs = snap.get("pending_prs") or []
    if pprs:
        parts.append("\nPRs awaiting Approval 2:")
        for p in pprs:
            parts.append(
                f"  - {p.get('pr_id')} · case {p.get('case_id')} · "
                f"{p.get('quantity')}× {p.get('component')} from {p.get('supplier_name')}"
            )

    return "\n".join(parts)


def _sanitize_history(messages: Iterable[dict[str, Any]]) -> list[dict[str, str]]:
    clean: list[dict[str, str]] = []
    for m in list(messages or [])[-MAX_HISTORY * 2:]:
        role = (m.get("role") or "").lower()
        content = (m.get("content") or "").strip()
        if role not in {"user", "assistant"} or not content:
            continue
        clean.append({"role": role, "content": content[:4000]})
    # Drop leading assistant messages (conversation must start with user)
    while clean and clean[0]["role"] != "user":
        clean.pop(0)
    return clean


def _canned_fallback(messages: list[dict[str, str]], ctx: dict[str, Any] | None) -> str:
    last = messages[-1]["content"].lower() if messages else ""
    role = (ctx or {}).get("role") or "guest"
    if "replace" in last or "replacement" in last:
        return ("Replace flow: the supervisor clicks Replace on a case → EWM stock is checked "
                "(FULL / PARTIAL / NONE). If FULL, a reservation is made. Otherwise the AI picks "
                "a certified supplier and raises a PR that needs Approval 2 to become a PO.")
    if "repair" in last:
        return ("Repair flow: the supervisor approves Repair → a SAP Maintenance Order is created "
                "UNDER_REPAIR at a priority driven by severity, and the case reaches END.")
    if "login" in last or "role" in last or "sign in" in last:
        return ("Pick a role (Operator or Supervisor) on the login screen, then sign in with your "
                "employment ID + passcode. New users can Create Account on the same screen.")
    if "pr" in last or "po" in last or "procurement" in last:
        return ("Approval 2: the supervisor reviews pending PRs on the second tab. Approving a PR "
                "auto-issues the Purchase Order against the AI-selected supplier.")
    return (f"Groq key not configured — running in offline mode. "
            f"I can still explain the workflow. (You're currently in the {role} context.)")


# ── Public API ──────────────────────────────────────────────────────────────
def chat(messages: list[dict[str, Any]], context: dict[str, Any] | None = None) -> dict[str, Any]:
    """Run a single chat turn. See module docstring for shape details."""
    history = _sanitize_history(messages or [])
    if not history:
        return {
            "reply": "Hi! Ask me anything about the HCL AI Force platform.",
            "source": "fallback",
            "model": "noop",
        }

    # Build the full context block: request-scoped context (user, page,
    # current case) + an authoritative live snapshot of the platform
    # state (cases, inventory, suppliers, PRs, POs) read from storage.
    snapshot = _platform_snapshot()
    system_prompt = (
        _BASE_SYSTEM
        + "\n\n— Live request context —\n" + _context_block(context)
        + "\n\n" + _snapshot_block(snapshot)
        + "\n\nWhen the user asks a data question (\"how many cases are pending?\", "
          "\"what's the stock of turbine blades?\", \"who's the supplier for case X?\"), "
          "answer directly using the LIVE PLATFORM STATE above. If the specific item "
          "isn't in the snapshot, say so plainly instead of making up values."
    )

    if not _groq_available():
        return {
            "reply": _canned_fallback(history, context),
            "source": "fallback",
            "model": "offline",
            "diagnostic": "GROQ_API_KEY is not set in the backend environment.",
        }

    key = os.environ["GROQ_API_KEY"]
    url = f"{BASE_URL}/chat/completions"
    base_messages = [{"role": "system", "content": system_prompt}, *history]

    last_err: str = ""
    last_status: int = 0
    for model in MODEL_CHAIN:
        if not model:
            continue
        payload = {
            "model": model,
            "messages": base_messages,
            "temperature": 0.35,
            "max_tokens": 480,
        }
        try:
            resp = requests.post(
                url,
                headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
                json=payload,
                timeout=25,
            )
            last_status = resp.status_code
            if resp.status_code >= 400:
                # Capture upstream explanation so the UI can tell the user
                # what Groq actually objected to (bad key, dead model, etc.).
                try:
                    err_json = resp.json()
                    last_err = (
                        (err_json.get("error") or {}).get("message")
                        if isinstance(err_json, dict) else None
                    ) or resp.text[:300]
                except Exception:
                    last_err = resp.text[:300]
                # 401/403 = auth problem — no point trying other models.
                if resp.status_code in (401, 403):
                    break
                continue
            data = resp.json()
            msg = ((data.get("choices") or [{}])[0].get("message") or {}).get("content") or ""
            if not msg.strip():
                last_err = "Empty completion from upstream model."
                continue
            return {
                "reply": msg.strip(),
                "source": "groq",
                "model": model,
                "usage": data.get("usage") or {},
            }
        except Exception as exc:
            last_err = f"{type(exc).__name__}: {exc}"
            continue

    # Every model failed — return a best-effort canned response with the
    # upstream diagnostic so the UI can display a clear reason.
    return {
        "reply": _canned_fallback(history, context),
        "source": "fallback",
        "model": "error",
        "diagnostic": (
            f"Groq call failed (status {last_status or 'n/a'}): "
            f"{last_err or 'unknown error'}. "
            f"Rotate GROQ_API_KEY in .env or set GROQ_CHAT_MODEL to a supported model."
        ),
    }
