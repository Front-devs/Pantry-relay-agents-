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
import secrets
import sys
import threading
import time
import webbrowser
from collections import OrderedDict
from contextlib import contextmanager
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
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
from pantryrelay.trace import LiveUnavailable, run_morning, run_morning_live  # noqa: E402

#: Whether this deployment will call Bedrock at all. Off unless asked for,
#: because the offline run is the one that must never fail: it is what keeps the
#: public link working when a key expires the night before judging.
LIVE_ENABLED = os.environ.get("PANTRYRELAY_LIVE", "").strip().lower() in {"1", "true", "yes"}

#: A live run is roughly sixteen model calls. On a public URL that is a bill
#: anyone can run up, so the whole deployment gets a fixed allowance per hour and
#: falls back to the offline run once it is spent. This is deliberately crude;
#: the point is that there is a ceiling, not that it is finely tuned.
LIVE_RUNS_PER_HOUR = int(os.environ.get("PANTRYRELAY_LIVE_BUDGET", "20"))
_live_runs: list[float] = []


#: At most one live run at a time, and callers do not queue behind it.
#:
#: A live run is sixteen model calls against Bedrock, and it holds the state lock
#: for all of them because the agents mutate the same seeded network every other
#: request reads. That is a real limitation: while one live run is in flight the
#: deployment is effectively single-user. Letting requests queue would turn a
#: slow demo into an unresponsive one, so a second live request is refused
#: immediately and told to take the offline run instead.
LIVE_SLOT = threading.BoundedSemaphore(1)

_budget_lock = threading.Lock()


def live_budget_denial() -> str | None:
    """Spend one unit of the live-run allowance, or say why it cannot be spent."""
    now = time.time()
    with _budget_lock:
        _live_runs[:] = [t for t in _live_runs if now - t < 3600]
        if len(_live_runs) >= LIVE_RUNS_PER_HOUR:
            return (
                f"Live runs are capped at {LIVE_RUNS_PER_HOUR} an hour for this "
                "demo and the allowance is spent."
            )
        _live_runs.append(now)
    return None


def refund_live_budget() -> None:
    """Give back an allowance unit for a run that never reached Bedrock."""
    with _budget_lock:
        if _live_runs:
            _live_runs.pop()

COOKIE_NAME = "pantryrelay_sid"

#: How many viewers' runs to keep. A public demo link gets opened by strangers
#: and never closed, so the oldest session is evicted rather than letting this
#: grow without limit.
MAX_SESSIONS = 200


class Session:
    """One viewer's morning.

    The pantry network, the ledger and the outbox are module-level state in
    ``data.py``, which is right for a single-process demo and wrong for a public
    URL two people can open at once. Each viewer therefore keeps their own copy
    of that state here, and a request swaps it in for the moment it runs.

    The gate belongs to the session for a stronger reason than tidiness. A
    coordinator's answer belongs to the run that raised the question, so the
    escalation a resolution acts on has to be the object that run actually
    produced — not an equivalent one rebuilt to match.
    """

    def __init__(self) -> None:
        self.gate: CoordinatorGate | None = None
        self.capacity: dict[str, dict[str, float]] | None = None
        self.ledger: list[dict[str, Any]] = []
        self.outbox: list[dict[str, Any]] = []


SESSIONS: OrderedDict[str, Session] = OrderedDict()

#: Serialises access to the shared module-level network while a session's state
#: is swapped in. Every run is a few milliseconds of pure computation, so one
#: lock costs nothing and removes the whole class of interleaving bug.
STATE_LOCK = threading.Lock()


@contextmanager
def session_state(session: Session):
    """Run a block against one viewer's pantry network, then put it back."""
    with STATE_LOCK:
        reset()
        if session.capacity is not None:
            for pantry in PANTRIES:
                pantry.free_lbs = dict(session.capacity[pantry.id])
            LEDGER.extend(session.ledger)
            OUTBOX.extend(session.outbox)
        try:
            yield
        finally:
            session.capacity = {p.id: dict(p.free_lbs) for p in PANTRIES}
            session.ledger = list(LEDGER)
            session.outbox = list(OUTBOX)
            reset()

WEB_DIR = Path(__file__).parent / "web"
INDEX_HTML = WEB_DIR / "index.html"

TOTAL_CAPACITIES = {
    "stjohns": {"ambient": 1000.0, "refrigerated": 400.0, "frozen": 120.0},
    "eastside": {"ambient": 1400.0, "refrigerated": 90.0, "frozen": 0.0},
    "riverside": {"ambient": 300.0, "refrigerated": 260.0, "frozen": 800.0},
    "graceave": {"ambient": 650.0, "refrigerated": 0.0, "frozen": 0.0},
}


class DashboardHandler(BaseHTTPRequestHandler):
    #: Set on the response when this request minted a new session id.
    _new_sid: str | None = None

    def session(self) -> Session:
        """This viewer's session, creating and cookie-ing one if needed."""
        raw = self.headers.get("Cookie")
        sid = None
        if raw:
            try:
                sid = SimpleCookie(raw).get(COOKIE_NAME)
                sid = sid.value if sid else None
            except Exception:
                sid = None

        with STATE_LOCK:
            if sid and sid in SESSIONS:
                SESSIONS.move_to_end(sid)
                return SESSIONS[sid]
            sid = secrets.token_urlsafe(16)
            session = Session()
            SESSIONS[sid] = session
            while len(SESSIONS) > MAX_SESSIONS:
                SESSIONS.popitem(last=False)

        self._new_sid = sid
        return session

    def do_GET(self) -> None:
        if self.path in ("/", "/index.html"):
            self.serve_file(INDEX_HTML, "text/html; charset=utf-8")
        elif self.path == "/api/state":
            session = self.session()
            with session_state(session):
                payload = {
                    "pantries": [
                        {
                            "id": p.id,
                            "name": p.name,
                            "address": p.address,
                            "distance_km": p.distance_km,
                            "contact": p.contact,
                            "needs": p.needs,
                            "free_lbs": dict(p.free_lbs),
                            "total_lbs": TOTAL_CAPACITIES.get(p.id, {}),
                        }
                        for p in PANTRIES
                    ],
                    "ledger": list(LEDGER),
                    "outbox": list(OUTBOX),
                }
            self.send_json(payload)
        else:
            self.send_error(404, "File Not Found")

    def do_POST(self) -> None:
        session = self.session()
        if self.path == "/api/reset":
            with STATE_LOCK:
                session.gate = None
                session.capacity = None
                session.ledger = []
                session.outbox = []
            self.send_json({"status": "ok", "message": "Pantry state reset to the 08:00 baseline"})
        elif self.path == "/api/run":
            # The page asks for a run; it does not describe one. Everything the
            # dashboard renders comes back out of the real CoordinatorGate, so
            # what a viewer watches is what the gate did.
            live = str(self.read_json().get("mode", "")) == "live"
            gate = CoordinatorGate()

            if live:
                if not LIVE_ENABLED:
                    self.send_json({
                        "error": "Live mode is off for this deployment.",
                        "remedy": "Start with --live-enabled, or set PANTRYRELAY_LIVE=1.",
                    })
                    return
                denied = live_budget_denial()
                if denied:
                    self.send_json({"error": denied, "remedy": "Try the offline run."})
                    return
                if not LIVE_SLOT.acquire(blocking=False):
                    refund_live_budget()
                    self.send_json({
                        "error": "A live run is already in progress.",
                        "remedy": "Wait for it to finish, or take the offline run.",
                    })
                    return
                try:
                    with session_state(session):
                        trace = run_morning_live(gate)
                except LiveUnavailable as exc:
                    refund_live_budget()
                    self.send_json({"error": exc.problem, "remedy": exc.remedy})
                    return
                finally:
                    LIVE_SLOT.release()
            else:
                with session_state(session):
                    trace = run_morning(gate)

            session.gate = gate
            self.send_json(trace)
        elif self.path == "/api/resolve":
            self.handle_resolve(session)
        else:
            self.send_error(404, "Endpoint Not Found")

    def read_json(self) -> dict:
        try:
            length = int(self.headers.get("Content-Length") or 0)
            return json.loads(self.rfile.read(length) or b"{}")
        except (ValueError, TypeError, json.JSONDecodeError):
            return {}

    def handle_resolve(self, session: Session) -> None:
        """Carry out a coordinator's answer to one held escalation."""
        gate = session.gate
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

        # The resolution runs against this viewer's own pantry network, so two
        # people answering the same held decision do not spend each other's
        # freezer space.
        with session_state(session):
            results = apply_resolution(esc, coerce_choice(esc, choice))
            payload = {
                "results": results,
                "pantries": [
                    {"id": p.id, "name": p.name, "free_lbs": dict(p.free_lbs)}
                    for p in PANTRIES
                ],
                "bookings": len(LEDGER),
                "messages": len(OUTBOX),
            }
        gate.escalations.remove(esc)
        payload["remaining"] = len(gate.escalations)
        self.send_json(payload)

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
        # No wildcard CORS here: the session cookie below is what separates one
        # viewer's morning from another's, and a wildcard origin plus cookies is
        # the combination browsers refuse anyway.
        if self._new_sid:
            self.send_header(
                "Set-Cookie",
                f"{COOKIE_NAME}={self._new_sid}; Path=/; SameSite=Lax; Max-Age=86400",
            )
            self._new_sid = None
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
    parser.add_argument(
        "--live-enabled",
        action="store_true",
        help=(
            "allow the dashboard to run the reader and router agents against "
            "Bedrock. Needs AWS credentials in the environment. Equivalent to "
            "PANTRYRELAY_LIVE=1."
        ),
    )
    args = parser.parse_args()

    global LIVE_ENABLED
    LIVE_ENABLED = LIVE_ENABLED or args.live_enabled

    port = args.port
    host = "0.0.0.0" if os.environ.get("PORT") else "127.0.0.1"
    server = None
    for attempt_port in range(port, port + 10):
        try:
            # Threaded: one slow client must not stall every other viewer of a
            # public demo link. Shared state is guarded by STATE_LOCK.
            server = ThreadingHTTPServer((host, attempt_port), DashboardHandler)
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
