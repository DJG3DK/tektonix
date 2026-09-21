"""Per-seam route modules, included by agent/server.py.

`server.py` is past 5,600 lines. docs/playbooks/README.md forbids flattening
it in one pass, and `tests/test_route_inventory.py` is the reason that rule is
safe to follow: it pins every route's path, method and auth dependency, so a
seam that moves out is either identical afterwards or the snapshot fails.

Each module here owns one `APIRouter` and the request models only its own
handlers use. What stays in `server.py` is everything the seams share --
`app`, its state, `require_full_auth`, the audit helper -- which is why a
router takes what it needs through `Depends` or reads it off `request.app`
rather than importing `app` back from `server` and making the import a cycle.

Extraction order is smallest-first on purpose: the first seam is there to
prove the pattern against the inventory, not to move the most code. The named
seams in docs/todo.md (auth, tasks, planning, github, settings, uploads)
follow once the shape here has survived a few changes.
"""
