"""The server module must actually import.

`ast.parse` was used as the pre-deploy syntax check and it is not sufficient:
`await` outside an async function parses fine and only fails at COMPILE time.
That shipped a server that pm2 could not start (2026-08-26, get_model_config
made to await a coroutine while still a plain def). Importing the module is the
only check that covers the whole class.
"""
import importlib


def test_server_module_imports():
    importlib.import_module("agent.server")


def test_load_config_does_not_require_the_retired_model_env_vars(monkeypatch):
    """MODEL_PLAN / MODEL_EXECUTE / MODEL_REFLECT were required at startup
    long after the pipeline stopped reading them. A .env copied from an
    older template that omitted them (or a fresh one that never had them)
    should still boot."""
    from agent.config import load_config

    for key in ("MODEL_PLAN", "MODEL_EXECUTE", "MODEL_REFLECT"):
        monkeypatch.delenv(key, raising=False)
    cfg = load_config()
    assert cfg.router_api_key
    assert not hasattr(cfg, "model_plan")


def test_model_config_module_imports():
    importlib.import_module("agent.model_config")


def test_every_route_handler_that_awaits_is_async():
    """Cheap structural guard for the exact defect: a route function whose body
    contains `await` must be declared `async def`."""
    import ast
    import pathlib

    src = pathlib.Path("agent/server.py").read_text()
    tree = ast.parse(src)
    offenders = []
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef):   # sync def only
            if any(isinstance(n, ast.Await) for n in ast.walk(node)):
                offenders.append(node.name)
    assert not offenders, f"sync route handlers containing await: {offenders}"
