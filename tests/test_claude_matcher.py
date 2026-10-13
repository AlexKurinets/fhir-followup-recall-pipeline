"""The model-backed matcher, with the Anthropic client replaced by a stub (no network, no cost)."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from recall.config import ROOT
from recall.phrases import ClaudeMatcher, MatcherError, build_catalog
from recall.rulesheet import load_rulesheet

CATALOG = build_catalog(load_rulesheet(ROOT / "config" / "rules.yaml"))


class StubMessages:
    def __init__(self, reply: str | None = None, stop_reason: str = "end_turn", error: Exception | None = None):
        self.reply, self.stop_reason, self.error = reply, stop_reason, error
        self.calls: list[dict] = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        if self.error:
            raise self.error
        return SimpleNamespace(stop_reason=self.stop_reason,
                               content=[SimpleNamespace(type="text", text=self.reply)])


def matcher(**stub) -> tuple[ClaudeMatcher, StubMessages]:
    messages = StubMessages(**stub)
    return ClaudeMatcher(CATALOG, "claude-opus-5-5", client=SimpleNamespace(messages=messages)), messages


def test_returns_hits_for_catalog_ids():
    m, stub = matcher(reply=json.dumps({"matches": [{"entry_id": "RTC_4W"}, {"entry_id": "SESSION_SHOCKWAVE"}]}))
    hits = m.match("Shockwave to L heel today. See her back in about a month.")
    assert sorted(h.entry_id for h in hits) == ["RTC_4W", "SESSION_SHOCKWAVE"]
    assert next(h for h in hits if h.entry_id == "RTC_4W").interval_days == 28
    assert all(h.matcher == "claude:claude-opus-5-5" for h in hits)


def test_request_is_constrained_to_the_catalog():
    m, stub = matcher(reply=json.dumps({"matches": []}))
    m.match("note text")
    call = stub.calls[0]
    assert call["model"] == "claude-opus-5-5"
    schema = call["output_config"]["format"]["schema"]
    allowed = schema["properties"]["matches"]["items"]["properties"]["entry_id"]["enum"]
    assert set(allowed) == {e.id for e in CATALOG}
    assert call["messages"][0]["content"] == "<note>\nnote text\n</note>"
    assert "RTC_4W" in call["system"]


def test_unknown_ids_are_ignored():
    m, _ = matcher(reply=json.dumps({"matches": [{"entry_id": "SOMETHING_ELSE"}]}))
    assert m.match("text") == []


@pytest.mark.parametrize("stub", [
    dict(reply="not json"),
    dict(reply=json.dumps({"matches": []}), stop_reason="refusal"),
    dict(reply=json.dumps({"matches": []}), stop_reason="max_tokens"),
])
def test_anything_but_a_clean_answer_is_an_error_not_a_match(stub):
    m, _ = matcher(**stub)
    with pytest.raises(MatcherError):
        m.match("text")


def test_api_errors_become_matcher_errors():
    import anthropic
    import httpx2

    request = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
    m, _ = matcher(error=anthropic.APIConnectionError(request=request))
    with pytest.raises(MatcherError, match="unreachable"):
        m.match("text")


def test_matcher_errors_fail_closed_in_extraction():
    import base64

    from recall.extract import Extractor

    m, _ = matcher(reply="garbage")
    ex = Extractor(load_rulesheet(ROOT / "config" / "rules.yaml"), m)
    doc = {"resourceType": "DocumentReference", "id": "d1", "subject": {"reference": "Patient/p-1"},
           "date": "2026-09-01T12:00:00-04:00",
           "content": [{"attachment": {"contentType": "text/plain", "data": base64.b64encode(b"RTC 4 wks").decode()}}]}
    assert ex.document_reference(doc) == []
    assert ex.stats.matcher_errors == 1
