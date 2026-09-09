#!/usr/bin/env python
"""PantryRelay Live Web Dashboard Server.

Provides a visual dashboard for the Agents for Humans hackathon demonstration,
showcasing the morning triage of the seeded donation offers, the four seeded
pantries' live capacities, and the Coordinator Gate interlock.

Zero external dependencies: uses Python standard library `http.server`.

Usage:
    py -3.14 web_app.py
    py -3.14 web_app.py --port 8080
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any

# Ensure src/ is on sys.path
sys.path.insert(0, str(Path(__file__).parent / "src"))

# Reconfigure Windows stdout/stderr for unicode support
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
    except (AttributeError, OSError):
        pass

from pantryrelay.data import LEDGER, OUTBOX, PANTRIES, reset  # noqa: E402
from pantryrelay.gate import CoordinatorGate  # noqa: E402
from pantryrelay.resolution import apply_resolution, coerce_choice  # noqa: E402
from pantryrelay.trace import run_morning  # noqa: E402

#: The gate from the most recent /api/run. A coordinator's answer belongs to the
#: run that raised the question, so the escalations it resolves are the objects
#: that run actually produced — not a fresh set built to match.
SESSION: dict[str, Any] = {"gate": None}

WEB_DIR = Path(__file__).parent / "web"
INDEX_HTML = WEB_DIR / "index.html"

TOTAL_CAPACITIES = {
    "stjohns": {"ambient": 1000.0, "refrigerated": 400.0, "frozen": 120.0},
    "eastside": {"ambient": 1400.0, "refrigerated": 90.0, "frozen": 0.0},
    "riverside": {"ambient": 300.0, "refrigerated": 260.0, "frozen": 800.0},
    "graceave": {"ambient": 650.0, "refrigerated": 0.0, "frozen": 0.0},
}


class DashboardHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        if self.path in ("/", "/index.html"):
            self.serve_file(INDEX_HTML, "text/html; charset=utf-8")
        elif self.path == "/api/state":
            self.send_json({
                "pantries": [
                    {
                        "id": p.id,
                        "name": p.name,
                        "address": p.address,
                        "distance_km": p.distance_km,
                        "contact": p.contact,
                        "needs": p.needs,
                        "free_lbs": p.free_lbs,
                        "total_lbs": TOTAL_CAPACITIES.get(p.id, {}),
                    }
                    for p in PANTRIES
                ],
                "ledger": LEDGER,
                "outbox": OUTBOX,
            })
        else:
            self.send_error(404, "File Not Found")

    def do_POST(self) -> None:
        if self.path == "/api/reset":
            reset()
            SESSION["gate"] = None
            self.send_json({"status": "ok", "message": "Pantry state reset to the 08:00 baseline"})
        elif self.path == "/api/run":
            # The page asks for a run; it does not describe one. Everything the
            # dashboard renders below comes back out of route_offer and the real
            # CoordinatorGate, so what a viewer watches is what the gate did.
            gate = CoordinatorGate()
            trace = run_morning(gate)
            SESSION["gate"] = gate
            self.send_json(trace)
        elif self.path == "/api/resolve":
            self.handle_resolve()
        else:
            self.send_error(404, "Endpoint Not Found")

    def read_json(self) -> dict:
        try:
            length = int(self.headers.get("Content-Length") or 0)
            return json.loads(self.rfile.read(length) or b"{}")
        except (ValueError, TypeError, json.JSONDecodeError):
            return {}

    def handle_resolve(self) -> None:
        """Carry out a coordinator's answer to one held escalation."""
        gate = SESSION.get("gate")
        if gate is None:
            self.send_json({"error": "no run in progress — start the morning first"})
            return

        body = self.read_json()
        choice = str(body.get("choice", "decline"))
        try:
            index = int(body.get("id", -1))
            esc = gate.escalations[index]
        except (ValueError, TypeError, IndexError):
            self.send_json({"error": "no held decision with that id"})
            return

        results = apply_resolution(esc, coerce_choice(esc, choice))
        gate.escalations.remove(esc)
        self.send_json({
            "results": results,
            "pantries": [
                {"id": p.id, "name": p.name, "free_lbs": dict(p.free_lbs)} for p in PANTRIES
            ],
            "remaining": len(gate.escalations),
            "bookings": len(LEDGER),
            "messages": len(OUTBOX),
        })

    def serve_file(self, file_path: Path, content_type: str) -> None:
        if not file_path.exists():
            self.send_error(404, "File Not Found")
            return
        content = file_path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(content)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(content)

    def send_json(self, data: dict) -> None:
        raw = json.dumps(data).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(raw)

    def log_message(self, format: str, *args) -> None:
        # Keep terminal output clean
        return


def main() -> int:
    default_port = int(os.environ.get("PORT", 8000))
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=default_port, help=f"port to bind server (default: {default_port})")
    parser.add_argument("--no-browser", action="store_true", help="do not open browser automatically")
    args = parser.parse_args()

    port = args.port
    host = "0.0.0.0" if os.environ.get("PORT") else "127.0.0.1"
    server = None
    for attempt_port in range(port, port + 10):
        try:
            server = HTTPServer((host, attempt_port), DashboardHandler)
            port = attempt_port
            break
        except OSError:
            continue

    if not server:
        print(f"Error: Could not bind to any port in range {args.port}-{args.port + 9}", file=sys.stderr)
        return 1

    url = f"http://localhost:{port}"
    print()
    print("----------------------------------------------------------------")
    print("   PantryRelay Live Web Dashboard                               ")
    print("   Good Neighbor Agents Track · Agents for Humans               ")
    print(f"   Dashboard Running: {url}")
    print("   Press Ctrl+C to stop                                         ")
    print("----------------------------------------------------------------")
    print()

    if not args.no_browser:
        try:
            webbrowser.open(url)
        except Exception:
            pass

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping dashboard server...")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
