"""
HCL AI Force — Intelligent Maintenance & Procurement Platform.

Strict layered workflow:

  Image → AI detect → SAP Maintenance Order → Supervisor Inbox (Approval 1)
      ↓                                                 ↓
   operator                              Reject | Resubmit | Repair | Replace
                                                        ↓
                                             Repair: MO under repair → END
                                                        ↓
                                             Replace: EWM check
                                                  ↓         ↓         ↓
                                                FULL     PARTIAL     NONE
                                                 ↓          ↓          ↓
                                            Reservation ↓    AI Supplier Select
                                              (END)    Reserve+PR     PR
                                                             ↓         ↓
                                                Supervisor Approval 2 (PR → PO)
                                                             ↓
                                                       PROCUREMENT_COMPLETED
"""

from __future__ import annotations

import datetime as _dt
import uuid
from pathlib import Path

# Ensure .env is loaded before any module that reads GROQ_API_KEY at import
# time (e.g. backend.assistant via os.environ lookups).
try:
    from dotenv import load_dotenv as _load_dotenv
    _load_dotenv(Path(__file__).resolve().parent.parent / ".env")
except Exception:
    pass

from flask import Flask, Response, jsonify, request, send_from_directory, abort, stream_with_context
from flask_cors import CORS

from backend import cases as case_service
from backend import auth as auth_service
from backend import assistant as assistant_service
from backend.storage import (
    ROOT,
    DATA,
    UPLOADS,
    load as load_json,
    find_record,
)
from image_module import NotAnAircraftComponentError, detect_damage
from inventory_module import ewm_check, ai_select_supplier, create_reservation, rank_suppliers
from sap_integration import (
    create_maintenance_order,
    update_maintenance_order,
    create_purchase_requisition,
    approve_purchase_requisition,
    reject_purchase_requisition,
    get_purchase_requisition,
    create_purchase_order,
)

FRONTEND_DIR = ROOT / "frontend"

app = Flask(
    __name__,
    static_folder=str(FRONTEND_DIR / "assets"),
    static_url_path="/assets",
)
CORS(app)

_SEVERITY_PRIORITY = {"HIGH": "URGENT", "MEDIUM": "NORMAL", "LOW": "LOW"}


def _identity_from_request(default_role: str | None = None) -> dict:
    """Return the authenticated identity for the current request.

    Resolution order:
      1. Session token (Authorization: Bearer / X-Auth-Token) via auth_service
      2. multipart form fields   (operator_username / operator_name / operator_role)
      3. JSON body fields        (same keys)
      4. X-User-* request headers (legacy path, kept for back-compat)

    The token-backed identity is authoritative — the headers/form fields
    are only trusted when there is no active session. This means once
    auth is wired on the frontend, nobody can impersonate another user by
    forging the X-User-* headers.
    """
    token_user = auth_service.user_from_token(_bearer_token())
    if token_user:
        return {
            "username": (token_user.get("username") or "").strip(),
            "name": (token_user.get("name") or "").strip(),
            "role": (token_user.get("role") or default_role or "").strip(),
        }

    username = (
        (request.form.get("operator_username") if request.form else None)
        or ((request.json or {}).get("operator_username") if request.is_json else None)
        or request.headers.get("X-User-Id")
        or ""
    )
    name = (
        (request.form.get("operator_name") if request.form else None)
        or ((request.json or {}).get("operator_name") if request.is_json else None)
        or request.headers.get("X-User-Name")
        or ""
    )
    role = (
        (request.form.get("operator_role") if request.form else None)
        or ((request.json or {}).get("operator_role") if request.is_json else None)
        or request.headers.get("X-User-Role")
        or default_role
        or ""
    )
    return {
        "username": (username or "").strip(),
        "name": (name or "").strip(),
        "role": (role or "").strip(),
    }


def _audit_actor(default_role: str) -> str:
    """Compose a short string like 'Jane Doe (OP-8821)' for history rows."""
    ident = _identity_from_request(default_role=default_role)
    name = ident["name"]
    user = ident["username"]
    role = ident["role"] or default_role
    if name and user:
        return f"{name} ({user})"
    if name:
        return name
    if user:
        return user
    return role or "system"


# ─── Frontend routes ─────────────────────────────────────────────────────────
# Root (/) now serves the login page so every visitor lands on Sign-In
# first. The old marketing/landing page is still available at /home.
@app.route("/")
@app.route("/login")
@app.route("/login.html")
def login_page():
    return send_from_directory(FRONTEND_DIR, "login.html")


@app.route("/home")
@app.route("/home.html")
@app.route("/index.html")
def index():
    return send_from_directory(FRONTEND_DIR, "index.html")


# ─── Auth helpers ────────────────────────────────────────────────────────────
def _bearer_token() -> str | None:
    """Pull the session token from Authorization header or X-Auth-Token."""
    hdr = request.headers.get("Authorization") or ""
    if hdr.lower().startswith("bearer "):
        return hdr[7:].strip() or None
    return request.headers.get("X-Auth-Token") or None


def _require_user(expected_role: str | None = None) -> dict:
    token = _bearer_token()
    user = auth_service.user_from_token(token)
    if not user:
        abort(401)
    if expected_role and (user.get("role") or "").lower() != expected_role.lower():
        abort(403)
    return user


# ─── API: auth ───────────────────────────────────────────────────────────────
@app.route("/api/auth/register", methods=["POST"])
def auth_register():
    body = request.json or {}
    try:
        user, token = auth_service.register(
            username=body.get("username", ""),
            password=body.get("password", ""),
            name=body.get("name", ""),
            role=body.get("role", ""),
            email=body.get("email"),
        )
    except auth_service.AuthError as err:
        return jsonify({"error": err.code, "message": err.message}), err.status
    return jsonify({"user": user, "token": token}), 201


@app.route("/api/auth/login", methods=["POST"])
def auth_login():
    body = request.json or {}
    try:
        user, token = auth_service.login(
            username=body.get("username", ""),
            password=body.get("password", ""),
            expected_role=body.get("role") or None,
        )
    except auth_service.AuthError as err:
        return jsonify({"error": err.code, "message": err.message}), err.status
    return jsonify({"user": user, "token": token})


@app.route("/api/auth/logout", methods=["POST"])
def auth_logout():
    token = _bearer_token()
    ok = auth_service.logout(token or "")
    return jsonify({"ok": ok})


@app.route("/api/auth/me", methods=["GET"])
def auth_me():
    token = _bearer_token()
    user = auth_service.user_from_token(token)
    if not user:
        return jsonify({"error": "unauthenticated", "message": "No active session."}), 401
    return jsonify({"user": user})


# ─── API: in-app AI assistant (conversational) ──────────────────────────────
@app.route("/api/assistant/chat", methods=["POST"])
def assistant_chat():
    """
    Conversational chat endpoint for the floating HCL AI Assistant widget.

    Body:
        {
          "messages": [ {"role":"user"|"assistant","content":"..."}, ... ],
          "context":  {                                   (optional)
              "role": "operator"|"supervisor"|"guest",
              "page": "home"|"operator"|"supervisor"|"login",
              "current_case": { ... partial case payload ... }
          }
        }
    Response:
        { "reply": str, "source": "groq"|"fallback", "model": str, "usage": {...} }
    """
    body = request.json or {}
    messages = body.get("messages") or []
    context = body.get("context") or {}

    # Enrich context with the authenticated user so the AI knows who it's
    # talking to, even if the frontend didn't pass `user` explicitly.
    token_user = auth_service.user_from_token(_bearer_token())
    if token_user and "user" not in context:
        context["user"] = token_user
    if token_user and not context.get("role"):
        context["role"] = token_user.get("role")

    result = assistant_service.chat(messages=messages, context=context)
    return jsonify(result)


# ─── API: voice synthesis via ElevenLabs ────────────────────────────────────
@app.route("/api/assistant/voice", methods=["POST"])
def assistant_voice():
    """
    Synthesise speech from the given text using ElevenLabs and stream
    the audio/mpeg bytes back to the client. The ElevenLabs key lives
    only on the server — the browser never sees it.

    Body:
        { "text": str, "voice_id": str (optional), "model_id": str (optional) }
    Response:
        200 audio/mpeg stream, or 503 JSON error if ElevenLabs is not
        configured / the upstream call failed.
    """
    body = request.json or {}
    text = (body.get("text") or "").strip()
    voice_id = body.get("voice_id")
    model_id = body.get("model_id")
    if not text:
        return jsonify({"error": "empty_text", "message": "No text supplied."}), 400

    upstream, detail = assistant_service.tts_stream(text, voice_id=voice_id, model_id=model_id)
    if not upstream:
        return jsonify({"error": "tts_unavailable", "message": detail}), 503

    def generate():
        try:
            for chunk in upstream.iter_content(chunk_size=4096):
                if chunk:
                    yield chunk
        finally:
            upstream.close()

    return Response(
        stream_with_context(generate()),
        mimetype="audio/mpeg",
        headers={
            "Cache-Control": "no-store",
            "X-Accel-Buffering": "no",  # disable buffering on proxies
        },
    )


@app.route("/api/assistant/voices", methods=["GET"])
def assistant_voices():
    """Return the voices that the configured ElevenLabs account can use,
    so the UI can populate a voice picker. Gracefully returns an empty
    list when the key is missing or the call fails."""
    return jsonify(assistant_service.list_voices())

@app.route("/operator")
@app.route("/operator.html")
def operator_page():
    return send_from_directory(FRONTEND_DIR, "operator.html")


@app.route("/supervisor")
@app.route("/supervisor.html")
def supervisor_page():
    return send_from_directory(FRONTEND_DIR, "supervisor.html")


@app.route("/uploads/<path:filename>")
def uploaded_file(filename: str):
    return send_from_directory(str(UPLOADS), filename)


# ─── API: health ─────────────────────────────────────────────────────────────
@app.route("/api/health")
def health():
    return jsonify({
        "status": "ok",
        "service": "hcl-ai-maintenance",
        "time": _dt.datetime.now().isoformat(timespec="seconds"),
    })


# ─── API: case creation (Operator path) ─────────────────────────────────────
@app.route("/api/cases", methods=["POST"])
def create_case_endpoint():
    """Operator submits an image.

    Layer flow: image → AI detection → auto Maintenance Order → Case → Supervisor Inbox.
    """
    if "image" not in request.files:
        return jsonify({"error": "No image uploaded"}), 400

    upload = request.files["image"]
    if not upload.filename:
        return jsonify({"error": "Empty filename"}), 400

    ext = Path(upload.filename).suffix.lower() or ".jpg"
    if ext not in {".jpg", ".jpeg", ".png", ".webp"}:
        ext = ".jpg"

    new_name = f"{uuid.uuid4().hex[:12]}{ext}"
    dest = UPLOADS / new_name
    upload.save(str(dest))

    try:
        ai_report = detect_damage(str(dest))
    except NotAnAircraftComponentError as rej:
        # Image was accepted by the network but the vision model said this
        # isn't an aircraft component. Do NOT create a case, do NOT raise
        # a maintenance order, and delete the upload so we don't accumulate
        # non-aviation photos.
        try:
            dest.unlink(missing_ok=True)
        except Exception:
            pass
        return jsonify({
            "error": "not_aircraft_component",
            "message": rej.reason,
            "observed_subject": rej.observed_subject,
            "hint": "Please upload a clear photo of an aircraft, engine, or "
                    "aerospace component (e.g. turbine blade, compressor, "
                    "fan blade, bearing, shaft, combustor, nozzle).",
        }), 422
    except Exception as exc:
        return jsonify({"error": f"AI detection failed: {exc}"}), 500

    # 1) Create the case with the rule-based recommendation + Grok validation.
    #    No Maintenance Order is raised here — MO is deferred until the
    #    supervisor explicitly clicks "Repair" (per workflow spec).
    operator = _identity_from_request(default_role="operator")
    case = case_service.create_case(
        ai_report,
        image_path=f"uploads/{new_name}",
        operator=operator,
    )

    return jsonify({"case": case, "maintenance_order": None}), 201


@app.route("/api/cases", methods=["GET"])
def list_cases_endpoint():
    status = request.args.get("status")
    severity = request.args.get("severity")
    stage = request.args.get("stage")
    include_drafts = (request.args.get("include_drafts") or "").lower() in {"1", "true", "yes"}
    cases = case_service.list_cases(status=status, severity=severity, stage=stage)
    if not include_drafts and not status:
        # Hide DRAFT cases from the default listings (e.g. Supervisor Inbox).
        cases = [c for c in cases if (c.get("status") or "").upper() != "DRAFT"]
    return jsonify(cases)


@app.route("/api/cases/<case_id>/submit", methods=["POST"])
def submit_case_endpoint(case_id: str):
    """Operator clicks Submit in the result panel → case moves DRAFT → NEW."""
    actor = _audit_actor(default_role="operator")
    operator = _identity_from_request(default_role="operator")
    case = case_service.submit_to_inbox(case_id, by=actor, operator=operator)
    if case is None:
        abort(404)
    return jsonify({"case": case})


@app.route("/api/cases/<case_id>", methods=["GET"])
def get_case_endpoint(case_id: str):
    case = case_service.get_case(case_id)
    if case is None:
        abort(404)
    # enrich
    equipment = load_json("equipment.json")
    case = dict(case)
    case["equipment"] = equipment.get(case.get("equipment_id"), None)
    if case.get("initial_mo_id"):
        case["initial_mo"] = find_record("maintenance_orders.json", "maintenance_order_id", case["initial_mo_id"])
    if case.get("reservation_id"):
        case["reservation"] = find_record("reservations.json", "reservation_id", case["reservation_id"])
    if case.get("pr_id"):
        case["purchase_requisition"] = find_record("purchase_requisitions.json", "pr_id", case["pr_id"])
    if case.get("po_id"):
        case["purchase_order"] = find_record("purchase_orders.json", "po_id", case["po_id"])
    return jsonify(case)


# ─── API: Approval 1 actions ────────────────────────────────────────────────
@app.route("/api/cases/<case_id>/reject", methods=["POST"])
def reject_case(case_id: str):
    body = request.json or {}
    note = body.get("note")
    actor = _audit_actor(default_role="supervisor")
    case = case_service.get_case(case_id)
    if case is None:
        abort(404)

    # Cancel the initial maintenance order
    if case.get("initial_mo_id"):
        update_maintenance_order(case["initial_mo_id"], {"status": "CANCELLED"})

    case = case_service.transition(
        case_id,
        action="REJECTED",
        by=actor,
        note=note or "Case rejected by supervisor",
        patch={"status": "REJECTED", "stage": "END", "decision": "REJECT", "decision_note": note},
        approval=("APPROVAL_1", "REJECT"),
    )
    return jsonify(case)


@app.route("/api/cases/<case_id>/resubmit", methods=["POST"])
def resubmit_case(case_id: str):
    body = request.json or {}
    note = body.get("note")
    actor = _audit_actor(default_role="supervisor")
    case = case_service.get_case(case_id)
    if case is None:
        abort(404)

    if case.get("initial_mo_id"):
        update_maintenance_order(case["initial_mo_id"], {"status": "AWAITING_RESUBMIT"})

    case = case_service.transition(
        case_id,
        action="RESUBMIT_REQUESTED",
        by=actor,
        note=note or "Returned to operator for a clearer image",
        patch={"status": "RESUBMIT", "stage": "OPERATOR", "decision": "RESUBMIT", "decision_note": note},
        approval=("APPROVAL_1", "RESUBMIT"),
    )
    return jsonify(case)


@app.route("/api/cases/<case_id>/repair", methods=["POST"])
def repair_case(case_id: str):
    """Supervisor approves REPAIR → SAP Maintenance Order is created NOW.

    MOs are only ever raised via this endpoint. The image-upload flow does
    NOT create an MO — the case sits in Approval 1 until the supervisor
    clicks Repair, at which point a fresh SAP PM order is opened in
    UNDER_REPAIR state.
    """
    body = request.json or {}
    actor = _audit_actor(default_role="supervisor")
    case = case_service.get_case(case_id)
    if case is None:
        abort(404)

    # Create the Maintenance Order on repair approval (business rule)
    priority = _SEVERITY_PRIORITY.get((case.get("severity") or "").upper(), "NORMAL")
    if case.get("initial_mo_id"):
        # Defensive: if somehow an MO already exists for this case, reuse it.
        update_maintenance_order(case["initial_mo_id"], {"status": "UNDER_REPAIR"})
        repair_mo_id = case["initial_mo_id"]
    else:
        mo = create_maintenance_order(case, priority=priority, status="UNDER_REPAIR")
        repair_mo_id = mo["maintenance_order_id"]

    # Override note — emitted when the supervisor consciously picks REPAIR even
    # though the AI recommended REPLACE (or vice versa). The frontend posts
    # { override: { ai_recommendation, justification } } in that case.
    override = body.get("override") or None
    note = (
        f"Repair approved by {actor} — Maintenance Order {repair_mo_id} created "
        f"and UNDER_REPAIR (priority {priority})."
    )
    if override:
        note += (
            f" ⚠ Supervisor OVERRIDE: AI recommended "
            f"{override.get('ai_recommendation') or '—'} but supervisor chose REPAIR. "
            f"Justification: {override.get('justification') or '(none provided)'}."
        )

    case = case_service.transition(
        case_id,
        action="REPAIR_STARTED",
        by=actor,
        note=note,
        patch={
            "status": "UNDER_REPAIR",
            "stage": "END",
            "decision": "REPAIR",
            "initial_mo_id": repair_mo_id,
            "repair_mo_id": repair_mo_id,
            **({"override": {**override, "by": actor}} if override else {}),
        },
        approval=("APPROVAL_1", "REPAIR"),
    )
    mo_record = find_record("maintenance_orders.json", "maintenance_order_id", repair_mo_id)
    return jsonify({"case": case, "maintenance_order": mo_record})


# ─── API: REPLACE path (EWM + supplier + PR) ────────────────────────────────
@app.route("/api/cases/<case_id>/replace", methods=["POST"])
def replace_case(case_id: str):
    """Trigger the replacement pipeline: EWM check → reserve/PR → wait for Approval 2."""
    actor = _audit_actor(default_role="supervisor")
    case = case_service.get_case(case_id)
    if case is None:
        abort(404)

    body = request.json if request.is_json else {}
    required_qty = int(body.get("required_qty") or 1)
    override = body.get("override") or None

    component = case.get("component") or ""
    ewm = ewm_check(component, required_qty)
    status = ewm["status"]  # FULL | PARTIAL | NONE

    # Supersede initial MO — procurement is now the active workflow
    if case.get("initial_mo_id"):
        update_maintenance_order(case["initial_mo_id"], {"status": "SUPERSEDED_BY_REPLACE"})

    # Record the approval 1 decision + initial EWM snapshot
    note = f"Replacement chosen by {actor} · EWM status {status} · {ewm['message']}"
    if override:
        note += (
            f" ⚠ Supervisor OVERRIDE: AI recommended "
            f"{override.get('ai_recommendation') or '—'} but supervisor chose REPLACE. "
            f"Justification: {override.get('justification') or '(none provided)'}."
        )
    case_service.transition(
        case_id,
        action="REPLACE_INITIATED",
        by=actor,
        note=note,
        patch={
            "decision": "REPLACE",
            "required_qty": required_qty,
            "ewm_status": status,
            "ewm_snapshot": ewm,
            **({"override": {**override, "by": actor}} if override else {}),
        },
        approval=("APPROVAL_1", "REPLACE"),
    )

    # ── FULL: full reservation, END ─────────────────────────────────────────
    if status == "FULL":
        reservation = create_reservation(case, component, ewm["reservable_qty"], ewm["location"])
        case = case_service.transition(
            case_id,
            action="RESERVATION_CREATED",
            by=f"system (for {actor})",
            note=f"Full reservation {reservation['reservation_id']} for {ewm['reservable_qty']} × {component}.",
            patch={
                "status": "RESERVED",
                "stage": "END",
                "reservation_id": reservation["reservation_id"],
                "reserved_qty": reservation["quantity"],
                "shortage_qty": 0,
            },
        )
        return jsonify({
            "case": case,
            "ewm": ewm,
            "reservation": reservation,
            "supplier_selection": None,
            "pr": None,
        })

    # ── PARTIAL: reserve what we have, create PR for shortage ───────────────
    # ── NONE: no reservation, create PR for full qty ────────────────────────
    reservation = None
    reserved_qty = 0
    if status == "PARTIAL" and ewm["reservable_qty"] > 0:
        reservation = create_reservation(case, component, ewm["reservable_qty"], ewm["location"])
        reserved_qty = reservation["quantity"]
        case_service.transition(
            case_id,
            action="RESERVATION_CREATED",
            by=f"system (for {actor})",
            note=f"Partial reservation {reservation['reservation_id']} for {reserved_qty} × {component}.",
            patch={
                "reservation_id": reservation["reservation_id"],
                "reserved_qty": reserved_qty,
            },
        )

    shortage_qty = ewm["shortage_qty"]
    sev = (case.get("severity") or "").upper()
    urgency = "URGENT" if sev == "HIGH" else ("NORMAL" if sev == "MEDIUM" else "LOW")
    supplier_sel = ai_select_supplier(component, qty=shortage_qty, urgency=urgency)
    if "error" in supplier_sel:
        return jsonify({"error": supplier_sel["error"], "ewm": ewm}), 400

    supplier = supplier_sel["supplier"]
    pr = create_purchase_requisition(
        case_service.get_case(case_id),
        supplier,
        quantity=shortage_qty,
        note=f"Shortage of {shortage_qty} × {component} — EWM {status}",
    )

    case = case_service.transition(
        case_id,
        action="PR_RAISED",
        by=f"system (for {actor})",
        note=(
            f"AI supplier selection → {supplier['name']}. "
            f"PR {pr['pr_id']} raised for {shortage_qty} units, awaiting Approval 2."
        ),
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

    return jsonify({
        "case": case,
        "ewm": ewm,
        "reservation": reservation,
        "supplier_selection": supplier_sel,
        "pr": pr,
    })


# ─── API: Approval 2 — approve / reject PR ──────────────────────────────────
@app.route("/api/cases/<case_id>/approve-pr", methods=["POST"])
def approve_pr(case_id: str):
    actor = _audit_actor(default_role="supervisor")
    case = case_service.get_case(case_id)
    if case is None:
        abort(404)
    if not case.get("pr_id"):
        return jsonify({"error": "Case has no active PR"}), 400
    if case.get("pr_status") != "PENDING_APPROVAL":
        return jsonify({"error": f"PR already {case.get('pr_status')}"}), 400

    approve_purchase_requisition(case["pr_id"], approver=actor)
    pr = get_purchase_requisition(case["pr_id"])
    supplier = case.get("supplier") or {}
    unit_price = float((case.get("ewm_snapshot") or {}).get("unit_price") or 1000)
    po = create_purchase_order(case, pr, supplier, unit_price=unit_price)

    case = case_service.transition(
        case_id,
        action="PROCUREMENT_COMPLETED",
        by=actor,
        note=(
            f"PR {pr['pr_id']} approved → PO {po['po_id']} issued. "
            f"{pr['quantity']} × {case.get('component')} from {supplier.get('name')} "
            f"— ${po['total_cost']:.2f}, ETA {supplier.get('delivery_days')}d."
        ),
        patch={
            "status": "PROCUREMENT_COMPLETED",
            "stage": "END",
            "pr_status": "APPROVED",
            "po_id": po["po_id"],
            "total_cost": po["total_cost"],
        },
        approval=("APPROVAL_2", "APPROVE_PR"),
    )
    return jsonify({"case": case, "pr": pr, "po": po})


@app.route("/api/cases/<case_id>/reject-pr", methods=["POST"])
def reject_pr(case_id: str):
    actor = _audit_actor(default_role="supervisor")
    case = case_service.get_case(case_id)
    if case is None:
        abort(404)
    if not case.get("pr_id"):
        return jsonify({"error": "Case has no active PR"}), 400

    note = (request.json or {}).get("note") if request.is_json else None
    reject_purchase_requisition(case["pr_id"], approver=actor, reason=note)
    case = case_service.transition(
        case_id,
        action="PR_REJECTED",
        by=actor,
        note=note or "PR rejected by supervisor",
        patch={"status": "PR_REJECTED", "stage": "END", "pr_status": "REJECTED"},
        approval=("APPROVAL_2", "REJECT_PR"),
    )
    return jsonify(case)


# ─── API: read-only helpers ────────────────────────────────────────────────
@app.route("/api/inventory", methods=["GET"])
def get_inventory():
    return jsonify(load_json("inventory.json"))


@app.route("/api/inventory/check", methods=["GET"])
def inventory_check():
    component = request.args.get("component", "")
    qty = int(request.args.get("qty") or 1)
    return jsonify(ewm_check(component, qty))


@app.route("/api/suppliers", methods=["GET"])
def get_suppliers():
    return jsonify(load_json("suppliers.json"))


@app.route("/api/suppliers/rank", methods=["GET"])
def supplier_rank():
    component = request.args.get("component", "")
    return jsonify({"component": component, "ranked": rank_suppliers(component)})


@app.route("/api/replace-preview", methods=["GET"])
def replace_preview():
    """
    Preview endpoint used by the enhanced 'Replace' modal on the Supervisor
    console. In a single request it returns:

      * EWM availability (FULL | PARTIAL | NONE) for the component
      * The AI-ranked supplier list (top supplier highlighted, plus
        alternatives) with a natural-language reason string
      * A cost breakdown that combines on-hand inventory + PR shortage

    Nothing is committed — the UI calls this purely to render the modal.
    The actual commit happens when the supervisor confirms and POSTs to
    /api/cases/<id>/replace.
    """
    component = request.args.get("component", "").strip()
    qty = max(1, int(request.args.get("qty") or 1))
    case_id = request.args.get("case_id") or None
    urgency = (request.args.get("urgency") or "NORMAL").upper()

    if not component:
        return jsonify({"error": "component is required"}), 400

    ewm = ewm_check(component, qty)
    supplier_sel = ai_select_supplier(component, qty=max(1, ewm.get("shortage_qty") or qty), urgency=urgency)

    # Cost preview — reservation stock at cost price + shortage * supplier mult.
    unit_price = float(ewm.get("unit_price") or 0)

    # If EWM has no unit_price (component not catalogued, or the ewm_check
    # returned zero), fall back to the raw inventory.json entry (fuzzy match)
    # so the supplier cost is still meaningful. If that too is missing,
    # apply a conservative per-component default indexed on name heuristics
    # so the supervisor never sees a bare $0 estimate.
    if not unit_price:
        try:
            inv = load_json("inventory.json") or {}
            comp_key = (component or "").strip().lower()
            item = inv.get(comp_key)
            if item is None:
                for k, v in inv.items():
                    if k in comp_key or comp_key in k:
                        item = v
                        break
            if item and item.get("unit_price"):
                unit_price = float(item["unit_price"])
        except Exception:
            pass
    if not unit_price:
        cl = (component or "").lower()
        unit_price = (
            12000.0 if any(t in cl for t in ("disk", "rotor", "casing", "combustor"))
            else 6000.0 if any(t in cl for t in ("shaft", "blade", "nozzle", "housing"))
            else 500.0 if any(t in cl for t in ("seal", "bracket", "bearing"))
            else 25.0 if any(t in cl for t in ("bolt", "fastener"))
            else 1500.0
        )

    reservable_qty = int(ewm.get("reservable_qty") or 0)
    shortage_qty = int(ewm.get("shortage_qty") or 0)
    supplier = (supplier_sel or {}).get("supplier") or {}
    cost_multiplier = float(supplier.get("cost_multiplier", 1.10) or 1.10)
    supplier_unit = round(unit_price * cost_multiplier, 2)
    reserve_cost = round(reservable_qty * unit_price, 2)
    procurement_cost = round(shortage_qty * supplier_unit, 2)
    total_cost = round(reserve_cost + procurement_cost, 2)

    # Decorate the supplier object in place so the frontend card can show
    # unit price, cost multiplier, and per-supplier line total without
    # having to reach into cost_preview.
    if supplier:
        supplier["unit_price"] = supplier_unit
        supplier["cost_multiplier"] = cost_multiplier
        supplier["line_total"] = procurement_cost
        supplier["currency"] = "USD"

    return jsonify({
        "component": component,
        "required_qty": qty,
        "case_id": case_id,
        "ewm": ewm,
        "supplier_selection": supplier_sel,
        "cost_preview": {
            "ewm_unit_price": unit_price,
            "supplier_unit_price": supplier_unit,
            "supplier_cost_multiplier": cost_multiplier,
            "reservable_qty": reservable_qty,
            "reserve_cost": reserve_cost,
            "shortage_qty": shortage_qty,
            "procurement_cost": procurement_cost,
            "total_cost": total_cost,
            "currency": "USD",
        },
    })


@app.route("/api/maintenance-orders", methods=["GET"])
def maintenance_orders_list():
    return jsonify(load_json("maintenance_orders.json"))


@app.route("/api/reservations", methods=["GET"])
def reservations_list():
    return jsonify(load_json("reservations.json"))


@app.route("/api/purchase-orders", methods=["GET"])
def purchase_orders_list():
    return jsonify(load_json("purchase_orders.json"))


@app.route("/api/purchase-requisitions", methods=["GET"])
def purchase_requisitions_list():
    status = request.args.get("status")
    data = load_json("purchase_requisitions.json")
    if status:
        data = [p for p in data if (p.get("status") or "").upper() == status.upper()]
    return jsonify(data)


@app.route("/api/dashboard/stats", methods=["GET"])
def dashboard_stats():
    return jsonify(case_service.dashboard_stats())


@app.route("/api/equipment/<equipment_id>", methods=["GET"])
def get_equipment(equipment_id: str):
    eq = load_json("equipment.json").get(equipment_id)
    if not eq:
        abort(404)
    return jsonify(eq)


# ─── Error handlers ────────────────────────────────────────────────────────
@app.errorhandler(404)
def not_found(_err):
    if request.path.startswith("/api/"):
        return jsonify({"error": "Not found"}), 404
    return send_from_directory(FRONTEND_DIR, "index.html")


@app.errorhandler(500)
def internal_error(err):
    return jsonify({"error": "Internal server error", "detail": str(err)}), 500


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)
