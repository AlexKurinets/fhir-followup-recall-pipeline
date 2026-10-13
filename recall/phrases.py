"""Match clinical notes against the signed phrase list.

Two matchers share one interface:

* ``ListMatcher`` (default) normalizes the note and looks for each signed phrase, skipping hits that
  are negated ("declined shockwave session") or, for treatment sessions, only planned ("plan: EMTT
  session next visit"). It is deterministic and runs offline.
* ``ClaudeMatcher`` sends the note to a model behind a BAA-covered endpoint and asks which catalog
  entries it documents. It returns entry ids from a closed list and nothing else, so the model can
  match a paraphrase but cannot invent a rule, a date or a sentence.

Either way, the note text is held in memory for the duration of the call and never stored. What is
stored is the entry id, the note's date and its resource id.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Protocol

from .facts import RETURN_REQUEST
from .rulesheet import RuleSheet

NUMBER_WORDS = {
    "one": "1", "two": "2", "three": "3", "four": "4", "five": "5", "six": "6", "seven": "7",
    "eight": "8", "nine": "9", "ten": "10", "eleven": "11", "twelve": "12",
}
FILLER = {"in", "about", "approximately", "approx", "around", "the", "a", "an", "for", "x"}
NEGATORS = {"no", "not", "declined", "declines", "deferred", "defer", "cancelled", "canceled", "refused",
            "refuses", "without", "held", "hold", "never", "denies", "missed"}
NOT_PERFORMED = {"plan", "planned", "consider", "considering", "discussed", "discuss", "recommend",
                 "recommended", "schedule", "scheduled", "will", "candidate", "next"}
LOOKBACK_TOKENS = 4
_REWRITES = [
    (re.compile(r"\bf/u\b"), " follow up "),
    (re.compile(r"\bfollow-?up\b"), " follow up "),
    (re.compile(r"\brtc\b"), " return to clinic "),
    (re.compile(r"\bre-?check\b"), " recheck "),
    (re.compile(r"\b(wks?|weeks?)\b"), " weeks "),
    (re.compile(r"\b(mos?|mths?|months?)\b"), " months "),
    (re.compile(r"\bsessions\b"), " session "),
    (re.compile(r"[.;!?\n]+"), " . "),
    (re.compile(r"[^a-z0-9. ]+"), " "),
]


def normalize(text: str) -> list[str]:
    text = text.lower()
    for pattern, replacement in _REWRITES:
        text = pattern.sub(replacement, text)
    tokens = [NUMBER_WORDS.get(t, t) for t in text.split()]
    return [t for t in tokens if t not in FILLER]


@dataclass(frozen=True)
class CatalogEntry:
    id: str
    kind: str                     # a modality key, or "return_request"
    label: str
    phrases: tuple[str, ...]
    interval_days: int | None = None

    @property
    def requires_performed(self) -> bool:
        return self.kind != RETURN_REQUEST


@dataclass(frozen=True)
class PhraseHit:
    entry_id: str
    kind: str
    phrase: str
    matcher: str
    interval_days: int | None = None


def build_catalog(sheet: RuleSheet) -> tuple[CatalogEntry, ...]:
    entries = [
        CatalogEntry(id=f"SESSION_{m.key.upper()}", kind=m.key, label=m.label, phrases=m.note_phrases)
        for m in sheet.modalities.values() if m.note_phrases
    ]
    entries += [
        CatalogEntry(id=r.id, kind=RETURN_REQUEST, label=f"Return in {r.interval_days} days",
                     phrases=r.phrases, interval_days=r.interval_days)
        for r in sheet.return_requests
    ]
    return tuple(entries)


class Matcher(Protocol):
    name: str

    def match(self, text: str) -> list[PhraseHit]: ...


class MatcherError(RuntimeError):
    pass


class ListMatcher:
    name = "phrase-list"

    def __init__(self, catalog: tuple[CatalogEntry, ...]):
        self.catalog = catalog
        self._compiled = [(e, [(p, normalize(p)) for p in e.phrases]) for e in catalog]

    def match(self, text: str) -> list[PhraseHit]:
        tokens = normalize(text)
        hits = []
        for entry, phrases in self._compiled:
            for phrase, ptoks in phrases:
                if self._affirmed_occurrence(tokens, ptoks, entry.requires_performed):
                    hits.append(PhraseHit(entry.id, entry.kind, phrase, self.name, entry.interval_days))
                    break
        return hits

    @staticmethod
    def _affirmed_occurrence(tokens: list[str], ptoks: list[str], requires_performed: bool) -> bool:
        n = len(ptoks)
        if n == 0:
            return False
        for i in range(len(tokens) - n + 1):
            if tokens[i:i + n] != ptoks:
                continue
            window = []
            for t in reversed(tokens[max(0, i - LOOKBACK_TOKENS):i]):
                if t == ".":
                    break
                window.append(t)
            if NEGATORS.intersection(window):
                continue
            if requires_performed and NOT_PERFORMED.intersection(window):
                continue
            return True
        return False


CLAUDE_SYSTEM_PROMPT = """You review one outpatient clinical note at a time for a pain management and \
physical therapy practice. You are given a closed catalog of entries. Report which entries the note \
documents, using only ids from the catalog.

A treatment-session entry applies only when the note documents that session as performed at this \
visit. Do not report it when the session was planned, discussed, recommended, declined, cancelled, \
deferred or mentioned as history.

A return-request entry applies when the note asks the patient to come back after that interval. \
Treat clear equivalents as a match (for example "see her back in a month" for a 4-week entry). If \
the requested interval is not one of the catalog intervals, report nothing for it.

If nothing in the catalog applies, return an empty list. Do not explain."""


class ClaudeMatcher:
    """Model-backed matcher. Only use it against an endpoint covered by a BAA."""

    name = "claude"

    def __init__(self, catalog: tuple[CatalogEntry, ...], model: str, client=None):
        if client is None:
            import anthropic

            client = anthropic.Anthropic()
        self.client = client
        self.model = model
        self.catalog = catalog
        self._by_id = {e.id: e for e in catalog}
        lines = [f"- {e.id}: {e.label}. Example wording: {'; '.join(e.phrases)}" for e in catalog]
        self.system = CLAUDE_SYSTEM_PROMPT + "\n\nCatalog:\n" + "\n".join(lines)
        self.schema = {
            "type": "object",
            "properties": {
                "matches": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {"entry_id": {"type": "string", "enum": [e.id for e in catalog]}},
                        "required": ["entry_id"],
                        "additionalProperties": False,
                    },
                }
            },
            "required": ["matches"],
            "additionalProperties": False,
        }

    def match(self, text: str) -> list[PhraseHit]:
        import anthropic

        try:
            response = self.client.messages.create(
                model=self.model,
                max_tokens=4096,
                system=self.system,
                messages=[{"role": "user", "content": f"<note>\n{text}\n</note>"}],
                output_config={"effort": "low", "format": {"type": "json_schema", "schema": self.schema}},
            )
        except anthropic.APIStatusError as exc:
            raise MatcherError(f"model call failed with HTTP {exc.status_code}") from exc
        except anthropic.APIConnectionError as exc:
            raise MatcherError("model endpoint unreachable") from exc

        # A refusal or a truncated answer is treated as "no decision", never as a match.
        if response.stop_reason != "end_turn":
            raise MatcherError(f"model stopped with {response.stop_reason}")
        body = next((b.text for b in response.content if b.type == "text"), "")
        try:
            ids = {m["entry_id"] for m in json.loads(body)["matches"]}
        except (ValueError, KeyError, TypeError) as exc:
            raise MatcherError("model returned an unreadable answer") from exc

        hits = []
        for entry_id in sorted(ids):
            entry = self._by_id.get(entry_id)
            if entry is not None:
                hits.append(PhraseHit(entry.id, entry.kind, entry.label, f"{self.name}:{self.model}",
                                      entry.interval_days))
        return hits


def build_matcher(name: str, sheet: RuleSheet, model: str) -> Matcher:
    catalog = build_catalog(sheet)
    if name == "claude":
        return ClaudeMatcher(catalog, model)
    return ListMatcher(catalog)
