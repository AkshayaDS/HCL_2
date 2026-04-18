# HCL AI Force — Intelligent Maintenance & Procurement Platform

> Enterprise-grade AI platform that turns a single component photo into a fully orchestrated SAP PM + EWM + MM workflow — with two-stage supervisor approval. Built by **HCL Tech** for aviation MRO.

---

## 1. Strict Layered Architecture

```
 Operator (Inspector)                   Supervisor
 ────────────────────                   ──────────
 1. Capture / upload photo       →       3. Approval 1 — Inbox
 2. AI detects defect (Vision)           ├─ Reject  (END)
    │                                    ├─ Resubmit (back to operator)
    ▼                                    ├─ Repair  → SAP PM MO "UNDER_REPAIR" (END)
 auto-raised SAP Maintenance             └─ Replace → Decision Engine ▼
 Order (priority from severity)
 → Case lands in Supervisor Inbox                      EWM Availability Check
                                             ┌─────────────┼─────────────┐
                                            FULL        PARTIAL         NONE
                                             │             │              │
                                      Reservation   Reserve + PR    PR (full qty)
                                        (END)             │              │
                                                          ▼              ▼
                                                  AI Supplier Selection (certified only)
                                                          │
                                                          ▼
                                             4. Approval 2 — PR Queue
                                             ├─ Approve → auto-issue PO → PROCUREMENT_COMPLETED
                                             └─ Reject  → PR_REJECTED
```

Every layer is audit-trailed on the case: `audit_trail[]` for system events and `approvals[]` for the two supervisor decisions (Approval 1 and Approval 2).

---

## 2. Role Strictness

| Role                     | What they see                                                                           | Prohibited                                         |
|--------------------------|----------------------------------------------------------------------------------------|----------------------------------------------------|
| **Operator · Inspector** | Capture & upload screen only. AI result card. Case ID + MO ID confirmation.            | No inbox. No approval buttons. No dashboard.       |
| **Supervisor**           | Approval-1 Inbox, Approval-2 PR Queue, Dashboard, MOs, Procurement ledger, full modal. | Never captures images (read-only on that flow).    |

---

## 3. Case State Machine

```
NEW ─Reject──→ REJECTED (END)
NEW ─Resubmit→ RESUBMIT (back to operator)
NEW ─Repair─→ UNDER_REPAIR (END)  ← same SAP MO transitioned to UNDER_REPAIR
NEW ─Replace→ ┬ FULL   ─→ RESERVED (END)
              ├ PARTIAL─→ PR_PENDING_APPROVAL ─approve→ PROCUREMENT_COMPLETED (END)
              └ NONE   ─→ PR_PENDING_APPROVAL ─reject─→ PR_REJECTED (END)
```

Stages: `OPERATOR`, `APPROVAL_1`, `APPROVAL_2`, `END`.

---

## 4. Folder Layout

```
backend/            Flask orchestrator + case service + atomic JSON storage
image_module/       Groq Llama-4 Scout vision with deterministic offline mock
decision_engine/    Severity + safety-critical rules → REPAIR / REPLACE
inventory_module/   EWM (FULL/PARTIAL/NONE) + reservation + AI supplier scoring
sap_integration/    SAP PM (MN + MO) and SAP MM (PR → PO with Approval 2)
frontend/           Landing, Operator, Supervisor — dark theme, blue→purple gradient
data/               Seed + live JSON: cases, MOs, reservations, PRs, POs, uploads, suppliers, inventory, equipment
_test_e2e_core.py   End-to-end service-layer test (REPAIR, FULL, PARTIAL, NONE, PR approve/reject, reject, resubmit)
```

---

## 5. Core Data Objects

| Object               | Key fields                                                                                                               |
|----------------------|--------------------------------------------------------------------------------------------------------------------------|
| `Case`               | case_id, status, stage, component, defect, severity, confidence, initial_mo_id, repair_mo_id, required_qty, ewm_status, reservation_id, shortage_qty, supplier, pr_id, pr_status, po_id, total_cost, audit_trail[], approvals[] |
| `Maintenance Order`  | maintenance_order_id, case_id, equipment_id, issue_description, priority, status (OPEN / UNDER_REPAIR / CANCELLED / SUPERSEDED_BY_REPLACE / AWAITING_RESUBMIT) |
| `Reservation`        | reservation_id, case_id, component, quantity, location, status                                                            |
| `Purchase Requisition` | pr_id, case_id, component, quantity, supplier, status (PENDING_APPROVAL / APPROVED / REJECTED), approved_at, approver    |
| `Purchase Order`     | po_id, pr_id, case_id, supplier, quantity, unit_price, total_cost, eta_days                                              |

---

## 6. Endpoints (Single Flask App on :5000)

```
POST /api/cases                         (operator) image upload → AI detect → auto-MO → case
POST /api/cases/<id>/reject             (supervisor Approval 1) cancels MO, case END
POST /api/cases/<id>/resubmit           (supervisor Approval 1) returns case to operator
POST /api/cases/<id>/repair             (supervisor Approval 1) same MO → UNDER_REPAIR
POST /api/cases/<id>/replace            (supervisor Approval 1) EWM → reserve/PR, accepts {required_qty}
POST /api/cases/<id>/approve-pr         (supervisor Approval 2) issues PO, case END
POST /api/cases/<id>/reject-pr          (supervisor Approval 2) PR_REJECTED

GET  /api/cases                         list + filters (status, severity, stage)
GET  /api/cases/<id>                    enriched with equipment, initial_mo, reservation, pr, po
GET  /api/inventory/check?component=&qty=   EWM FULL/PARTIAL/NONE snapshot
GET  /api/suppliers/rank?component=     ranked certified suppliers
GET  /api/reservations                  all reservations
GET  /api/purchase-requisitions?status=PENDING_APPROVAL
GET  /api/maintenance-orders
GET  /api/purchase-orders
GET  /api/dashboard/stats               EWM counts, totals, spend
```

---

## 7. Run It Locally

**Prerequisites:** Python 3.10+

```bash
python -m venv venv
# Windows:
venv\Scripts\activate
# macOS / Linux:
source venv/bin/activate

pip install -r requirements.txt
python run.py
```

Open `http://localhost:5000` — landing page with Operator and Supervisor tiles.

**Shortcuts:** `run.bat` (Windows) or `./run.sh` (macOS/Linux) creates the venv, installs deps, and launches in one step.

### Real-time Groq Llama-4 Scout vision + Aircraft-only gate

The detector in `image_module/detector.py` calls **Groq's Llama-4 Scout
17B-16e-instruct** in real time. The prompt enforces a two-phase contract:

1. **Aircraft gate.** The model first decides whether the image is actually
   an aircraft / engine / aerospace component. Photos of people, cars,
   food, memes, screenshots, text-only images, etc. cause the backend to
   respond with **HTTP 422 `not_aircraft_component`** and the operator UI
   shows an inline *"Not an aircraft component — please resubmit"* banner
   with a one-click resubmit button. **No case, no Maintenance Order, no
   SAP transaction is ever created from a non-aircraft image.**
2. **Full diagnostic.** Only after the gate passes does the model return
   `component`, `defect`, `damaged_area`, `severity`, `confidence` and a
   short technical `ai_report` — which then drives the rest of the
   PM + EWM + MM workflow.

Setup: copy `.env.example` → `.env` and set `GROQ_API_KEY`. If the key is
missing or Groq is unreachable, the detector falls back to a deterministic
SHA-256-based mock so the demo never freezes — but the aircraft gate is
enforced exclusively by the real model, not the mock.

---

## 8. Demo Script (5 minutes)

1. **Landing (`/`)** — show the hero "Image to Action. Engine to SAP." and two role cards.
2. **Operator (`/operator`)** — upload any engine/turbine/bolt photo. Workflow bar animates through 6 steps; result card shows AI finding + MO confirmation.
3. **Supervisor → Approval 1 Inbox** — open the case. Modal shows photo + damage box, AI report, EWM-ready equipment, timeline.
4. **Repair** — click Repair → case becomes `UNDER_REPAIR`, original MO escalated (no duplicate MO).
5. **Replace · FULL** — new case, Replace, qty=1 on a well-stocked component (e.g. bolt). EWM returns FULL; case goes straight to `RESERVED`.
6. **Replace · PARTIAL** — new case on `shaft` with qty=3 (only 1 in stock). EWM returns PARTIAL; 1 reserved, PR raised for 2; Approval 2 queue lights up.
7. **Approval 2** — approve the PR → auto-issues PO; case becomes `PROCUREMENT_COMPLETED`.
8. **Replace · NONE** — new case on `fan blade` (0 stock). PR raised for full qty; supervisor can approve or reject.
9. **Dashboard** — 4 stat cards, charts (defects, decisions, severity, EWM), full ledger with every case.

---

## 9. Verifying the Build

```bash
python _test_e2e_core.py
```

Exercises every branch: REPAIR, REPLACE-FULL, REPLACE-PARTIAL, REPLACE-NONE, Approve PR → PO, Reject PR, Reject case, Resubmit, Dashboard aggregates. A passing run prints `✅ ALL CHECKS PASSED` and restores data files for a clean demo state.

---

## 10. Key Engineering Choices

- **Single-origin Flask app** — no CORS, one port.
- **Atomic JSON writes** (`tempfile` + `os.replace` under thread lock) — safe under concurrent demos, no DB required.
- **MO reuse on Repair** — the initial MO created on detection is the same MO executed under Repair (status transitions to UNDER_REPAIR). No duplicate MOs. On Replace the initial MO is marked `SUPERSEDED_BY_REPLACE` so the audit trail is preserved.
- **PR-for-shortage only** — under PARTIAL, only the shortage qty is procured; the available qty is reserved immediately.
- **Dual approval tracked** — `approvals[]` records `{stage, decision, by, at, note}` for both Approval 1 and Approval 2.
- **Deterministic offline AI** — SHA-256 seeds the mock so the same image always yields the same component/defect/severity — perfect for repeatable demos.
- **Composite supplier score** = 40% quality + 30% delivery speed + 30% proximity, only over certified suppliers that stock the component.
- **Safety-critical escalation** — turbine/compressor/fan blade, disk, rotor, shaft at HIGH severity escalate to REPLACE regardless of confidence.

---

*HCL Tech · Intelligent Maintenance & Procurement · 2026*
