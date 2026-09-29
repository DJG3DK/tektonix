"""Every router's audit write goes through agent.routers.audit_store.

The helper exists so an audit write can never be the reason a request 500s:
it answers None before the lifespan has attached the store, and agent/audit
.record treats None as "nothing to write to". A router that reaches for
`request.app.state.store` directly skips that, and three of them did
(audit 2026-09-29, B24). Pinned by source, so the next seam move that
copies the old pattern fails here."""
import inspect
import pathlib
import re

from agent import routers

_DIRECT = re.compile(r"audit\.record\(\s*request\.app\.state\.store")


def test_no_router_records_audit_through_app_state_directly():
    root = pathlib.Path(inspect.getfile(routers)).parent
    offenders = [p.name for p in sorted(root.glob("*.py")) if _DIRECT.search(p.read_text())]
    assert offenders == [], f"audit.record(request.app.state.store, ...) in {offenders}; use audit_store(request)"
