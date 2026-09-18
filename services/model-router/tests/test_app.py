"""The HTTP surface, and the compatibility contract it has to keep.

Every assertion here corresponds to a specific caller that breaks if it
changes. They are not style preferences:

  * `x-router-call-id` is read by name in agent/middleware/budget_guard.py
    (CALL_ID_HEADER). Rename it and every task silently reverts from billed
    spend to estimated spend -- with no error anywhere.
  * `metadata.agent_task_id` is put in the body by deep_agent._call_metadata.
    Drop it and the ledger can price a call but never total a task, which is
    how a killed pass used to lose the money it had spent.
  * /health/liveliness is polled by agent/health.py with no credentials.
"""

from __future__ import annotations

import json

import pytest
import yaml
from fastapi import HTTPException
from fastapi.testclient import TestClient

from router import app as app_module
from router import ledger
from router import upstream
from router.config import Registry

CONFIG = {
    "model_list": [
        {"model_name": "agent-coder", "params": {"model": "openrouter/deepseek/flash"}},
        {"model_name": "backup", "params": {"model": "openrouter/anthropic/haiku"}},
    ],
    "router_settings": {"fallbacks": [{"agent-coder": ["backup"]}]},
}


@pytest.fixture
def client(tmp_path, monkeypatch):
    cfg = tmp_path / "config.yaml"
    cfg.write_text(yaml.safe_dump(CONFIG))
    monkeypatch.setattr(app_module, "registry", Registry(cfg))
    monkeypatch.setattr(app_module, "MASTER_KEY", "sk-test")
    monkeypatch.setattr(app_module.ledger, "LOG_PATH", tmp_path / "ledger.jsonl")
    with TestClient(app_module.app) as c:
        yield c


def _ok(model="deepseek/flash", cost=0.001):
    return {"id": "gen-1", "model": model, "provider": "TestProvider",
            "choices": [{"message": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 2, "cost": cost,
                      "prompt_tokens_details": {"cached_tokens": 4}}}


def _stub(monkeypatch, results, *, status=500):
    """results: list of (ok, payload_or_error), consumed in order.

    `status` is the failure code, which decides whether the router retries the
    same deployment (transient) or moves straight on (permanent). Running out
    of results repeats the last one -- a provider that is down stays down.
    """
    calls = []

    async def fake(client, api_key, body, model, extra_body, timeout_s):
        calls.append({"model": model, "body": body, "timeout": timeout_s})
        ok, data = results[min(len(calls) - 1, len(results) - 1)]
        if ok:
            return upstream.Attempt(alias="", model=model, ok=True, status=200,
                                    duration_s=0.1, payload=data,
                                    usage=upstream.Usage.from_payload(data))
        return upstream.Attempt(alias="", model=model, ok=False, status=status,
                                duration_s=0.1, error=str(data))

    monkeypatch.setattr(app_module.upstream, "call_once", fake)
    monkeypatch.setattr(app_module, "BACKOFF_S", 0.0)   # no real sleeping in tests
    return calls


def _post(client, **over):
    body = {"model": "agent-coder", "messages": [{"role": "user", "content": "hi"}]}
    body.update(over)
    return client.post("/v1/chat/completions", json=body,
                       headers={"Authorization": "Bearer sk-test"})


# ---------------------------------------------------------------------------
# auth and routing
# ---------------------------------------------------------------------------

def test_liveliness_needs_no_key(client):
    r = client.get("/health/liveliness")
    assert r.status_code == 200 and r.json()["status"] == "alive"


def test_a_bad_key_is_rejected(client):
    r = client.post("/v1/chat/completions", json={"model": "agent-coder", "messages": []},
                    headers={"Authorization": "Bearer wrong"})
    assert r.status_code == 401


def test_a_missing_key_is_rejected(client):
    r = client.post("/v1/chat/completions", json={"model": "agent-coder", "messages": []})
    assert r.status_code == 401


def test_an_unknown_alias_is_404_not_a_wasted_upstream_call(client, monkeypatch):
    calls = _stub(monkeypatch, [])
    r = _post(client, model="no-such-alias")
    assert r.status_code == 404
    assert calls == [], "an unknown alias must not reach the provider"


def test_the_alias_resolves_to_its_model(client, monkeypatch):
    calls = _stub(monkeypatch, [(True, _ok())])
    assert _post(client).status_code == 200
    assert calls[0]["model"] == "deepseek/flash"


# ---------------------------------------------------------------------------
# the compatibility contract
# ---------------------------------------------------------------------------

def test_the_call_id_header_is_the_name_budget_guard_reads(client, monkeypatch):
    _stub(monkeypatch, [(True, _ok())])
    r = _post(client)
    assert app_module.CALL_ID_HEADER == "x-router-call-id"
    assert r.headers.get("x-router-call-id")


def test_the_ledger_line_matches_the_call_id_header(client, monkeypatch, tmp_path):
    _stub(monkeypatch, [(True, _ok())])
    r = _post(client)
    row = json.loads((tmp_path / "ledger.jsonl").read_text().splitlines()[-1])
    assert row["call_id"] == r.headers["x-router-call-id"], (
        "budget_guard matches spend on exactly this pairing")


def test_the_task_id_is_recorded_and_not_forwarded(client, monkeypatch, tmp_path):
    calls = _stub(monkeypatch, [(True, _ok())])
    _post(client, metadata={"agent_task_id": "T1", "agent_session_id": "S1"})
    row = json.loads((tmp_path / "ledger.jsonl").read_text().splitlines()[-1])
    assert row["task_id"] == "T1" and row["session_id"] == "S1"
    sent = upstream.build_body(calls[0]["body"], "m", {})
    assert "metadata" not in sent, "our routing metadata is not the provider's business"


def test_the_billed_cost_is_the_providers_not_a_rate_table(client, monkeypatch, tmp_path):
    _stub(monkeypatch, [(True, _ok(cost=0.0424))])
    _post(client)
    row = json.loads((tmp_path / "ledger.jsonl").read_text().splitlines()[-1])
    assert row["cost"] == 0.0424
    assert row["cached_tokens"] == 4


# ---------------------------------------------------------------------------
# fallbacks
# ---------------------------------------------------------------------------

def test_a_permanent_failure_falls_through_immediately(client, monkeypatch):
    """A 400 will fail identically on a second attempt; retrying it only adds
    latency to a certain failure."""
    calls = _stub(monkeypatch, [(False, "bad request"), (True, _ok("anthropic/haiku"))], status=400)
    r = _post(client)
    assert r.status_code == 200
    assert [c["model"] for c in calls] == ["deepseek/flash", "anthropic/haiku"]
    assert r.headers["x-router-deployment"] == "backup"


def test_a_transient_failure_retries_the_same_deployment_first(client, monkeypatch):
    """A 429 is the provider asking us to wait, not a reason to abandon the
    model the operator pinned. config.yaml records two 429s a minute apart
    taking a whole demo down."""
    calls = _stub(monkeypatch, [(False, "rate limited"), (True, _ok())], status=429)
    r = _post(client)
    assert r.status_code == 200
    assert [c["model"] for c in calls] == ["deepseek/flash", "deepseek/flash"]
    assert r.headers["x-router-deployment"] == "agent-coder", "stayed on the pinned model"


def test_retries_are_bounded_then_it_falls_back(client, monkeypatch):
    calls = _stub(monkeypatch, [(False, "429")], status=429)
    r = _post(client)
    assert r.status_code == 502
    # RETRIES_PER_DEPLOYMENT + 1 tries on each of the two deployments
    expected = (app_module.RETRIES_PER_DEPLOYMENT + 1) * 2
    assert len(calls) == expected, f"{len(calls)} attempts, expected {expected}"
    assert [c["model"] for c in calls[:3]] == ["deepseek/flash"] * 3
    assert calls[-1]["model"] == "anthropic/haiku"


def test_both_attempts_are_recorded(client, monkeypatch, tmp_path):
    """The failed attempt is part of what happened, and the Analytics error
    rate is only honest if it is written down."""
    _stub(monkeypatch, [(False, "boom"), (True, _ok("anthropic/haiku"))], status=400)
    _post(client)
    rows = [json.loads(line) for line in (tmp_path / "ledger.jsonl").read_text().splitlines()]
    assert len(rows) == 2
    assert rows[0]["error"] is True and rows[0]["attempt"] == 1
    assert rows[1].get("error") is None and rows[1]["attempt"] == 2


def test_the_whole_chain_failing_is_a_502_with_the_chain_named(client, monkeypatch):
    _stub(monkeypatch, [(False, "a"), (False, "b")], status=400)
    r = _post(client)
    assert r.status_code == 502
    assert r.json()["error"]["chain"] == ["agent-coder", "backup"]
    assert r.headers.get("x-router-call-id")


def test_one_ledger_line_per_attempt_shares_the_call_id(client, monkeypatch, tmp_path):
    """One logical call, one id -- so a fallback cannot be double-charged."""
    _stub(monkeypatch, [(False, "a"), (True, _ok())], status=400)
    r = _post(client)
    rows = [json.loads(line) for line in (tmp_path / "ledger.jsonl").read_text().splitlines()]
    assert {row["call_id"] for row in rows} == {r.headers["x-router-call-id"]}


# ---------------------------------------------------------------------------
# the table endpoints
# ---------------------------------------------------------------------------

def test_model_info_reports_the_openrouter_form(client):
    r = client.get("/v1/model/info", headers={"Authorization": "Bearer sk-test"})
    names = {m["model_name"]: m["params"]["model"] for m in r.json()["data"]}
    assert names["agent-coder"] == "openrouter/deepseek/flash"


def test_model_info_needs_a_key(client):
    assert client.get("/v1/model/info").status_code == 401


# ---------------------------------------------------------------------------
# streaming
#
# The rule is not "streams cannot fall back". A response is committed the
# moment a byte reaches the client; an upstream refusing with a 429 does so
# before any body exists, and falling back there is as safe as it is for a
# buffered call. Only a mid-stream failure is unrecoverable, because retrying
# would splice two completions into one response.
#
# This matters because the roles that stream here -- summarizer, cartographer,
# consolidator -- are exactly the ones with fallbacks configured, after real
# 429s took their providers out.
# ---------------------------------------------------------------------------

def _stream_stub(monkeypatch, plans):
    """plans: per attempt, either a list of chunks or an Exception to raise.

    An Exception raised before any chunk models a refusal; one raised after a
    chunk models a mid-stream failure.
    """
    seen = []

    async def fake(client, api_key, body, model, extra_body, timeout_s, usage_out):
        plan = plans[min(len(seen), len(plans) - 1)]
        seen.append(model)
        if isinstance(plan, Exception):
            raise plan
        for c in plan:
            if isinstance(c, Exception):
                raise c
            yield c.encode()

    monkeypatch.setattr(app_module.upstream, "stream_once", fake)
    monkeypatch.setattr(app_module, "BACKOFF_S", 0.0)
    return seen


def _stream(client, **over):
    body = {"model": "agent-coder", "stream": True,
            "messages": [{"role": "user", "content": "hi"}]}
    body.update(over)
    return client.post("/v1/chat/completions", json=body,
                       headers={"Authorization": "Bearer sk-test"})


def test_a_stream_that_works_is_passed_through(client, monkeypatch):
    _stream_stub(monkeypatch, [["data: a\n", "data: b\n"]])
    r = _stream(client)
    assert r.status_code == 200
    assert "data: a" in r.text and "data: b" in r.text


def test_a_refusal_before_the_first_byte_falls_back(monkeypatch, client):
    """Nothing has reached the client, so another deployment is safe."""
    class Boom(Exception):
        pass
    seen = _stream_stub(monkeypatch, [Boom("429 rate limited"), ["data: ok\n"]])
    r = _stream(client)
    assert r.status_code == 200
    assert "data: ok" in r.text
    assert len(seen) > 1, "it tried more than one attempt"


def test_a_failure_midstream_is_not_retried(monkeypatch, client):
    """Committed. Splicing a second completion onto a partial one would be
    worse than the truncation the client already sees."""
    class Boom(Exception):
        pass
    seen = _stream_stub(monkeypatch, [["data: partial\n", Boom("died")], ["data: second\n"]])
    r = _stream(client)
    assert "data: partial" in r.text
    assert "data: second" not in r.text, "must not splice two completions"
    assert len(seen) == 1


def test_the_call_id_header_is_present_on_a_stream(client, monkeypatch):
    _stream_stub(monkeypatch, [["data: a\n"]])
    r = _stream(client)
    assert r.headers.get("x-router-call-id")


def test_a_streamed_call_is_ledgered_once(client, monkeypatch, tmp_path):
    _stream_stub(monkeypatch, [["data: a\n"]])
    _stream(client, metadata={"agent_task_id": "T9"})
    rows = [json.loads(line) for line in (tmp_path / "ledger.jsonl").read_text().splitlines()]
    assert len(rows) == 1 and rows[0]["task_id"] == "T9"


# ---------------------------------------------------------------------------
# per-consumer keys (2026-09-16, replacing a single shared master key)
# ---------------------------------------------------------------------------

def test_consumer_keys_parse_to_secret_keyed_labels():
    """Keyed BY THE SECRET, not by label: a lookup is one dict hit rather than
    a loop whose timing varies with how many consumers are configured."""
    got = app_module._parse_consumer_keys("mail=sk-a, demo=sk-b ,,broken,=sk-c,label=")
    assert got == {"sk-a": "mail", "sk-b": "demo"}


def test_each_key_authorises_to_its_own_label(monkeypatch):
    monkeypatch.setattr(app_module, "MASTER_KEY", "sk-master")
    monkeypatch.setattr(app_module, "CONSUMER_KEYS", {"sk-mail": "mail", "sk-demo": "demo"})
    assert app_module._authorise("Bearer sk-master") == "master"
    assert app_module._authorise("Bearer sk-mail") == "mail"
    assert app_module._authorise("Bearer sk-demo") == "demo"


@pytest.mark.parametrize("header", [None, "", "sk-mail", "Basic sk-mail", "Bearer wrong"])
def test_anything_else_is_rejected(monkeypatch, header):
    monkeypatch.setattr(app_module, "MASTER_KEY", "sk-master")
    monkeypatch.setattr(app_module, "CONSUMER_KEYS", {"sk-mail": "mail"})
    with pytest.raises(HTTPException) as e:
        app_module._authorise(header)
    assert e.value.status_code == 401


def test_revoking_one_consumer_does_not_affect_the_others(monkeypatch):
    """The whole point of the split. Dropping one label must leave every other
    caller working -- with a single shared key this was impossible."""
    monkeypatch.setattr(app_module, "MASTER_KEY", "sk-master")
    monkeypatch.setattr(app_module, "CONSUMER_KEYS", {"sk-demo": "demo"})
    assert app_module._authorise("Bearer sk-demo") == "demo"
    with pytest.raises(HTTPException):
        app_module._authorise("Bearer sk-mail")
    assert app_module._authorise("Bearer sk-master") == "master"


def test_no_keys_configured_is_still_an_open_dev_run(monkeypatch):
    monkeypatch.setattr(app_module, "MASTER_KEY", "")
    monkeypatch.setattr(app_module, "CONSUMER_KEYS", {})
    assert app_module._authorise(None) == "dev"


def test_the_ledger_records_which_consumer_called(tmp_path):
    """Attribution is the reason the label exists: spend has to be answerable
    per consumer, not just per role."""
    path = tmp_path / "routing.jsonl"
    ledger.record(call_id="c1", alias="another-alias", model="m", caller="other-app", path=path)
    entry = json.loads(path.read_text().splitlines()[0])
    assert entry["caller"] == "other-app"


def test_caller_is_optional_so_old_readers_are_unaffected(tmp_path):
    path = tmp_path / "routing.jsonl"
    ledger.record(call_id="c1", alias="a", model="m", path=path)
    assert json.loads(path.read_text())["caller"] is None
