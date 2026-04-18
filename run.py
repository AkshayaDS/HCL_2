"""Launch the HCL AI Force Flask application.

Run from the project root so the `backend`, `image_module`, `decision_engine`,
`inventory_module` and `sap_integration` packages are importable.

    python run.py
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from backend.app import app  # noqa: E402


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "5000"))
    host = os.environ.get("HOST", "0.0.0.0")
    print(f"\n  HCL AI Force — Intelligent Maintenance Platform")
    print(f"  Running on  http://localhost:{port}")
    print(f"  Operator    http://localhost:{port}/operator")
    print(f"  Supervisor  http://localhost:{port}/supervisor\n")
    app.run(host=host, port=port, debug=True, use_reloader=True)
