"""Look up term-discovery suggestions in the active corporate glossary."""
from __future__ import annotations

from functools import cached_property
from typing import TYPE_CHECKING

from . import glossary, term_morphology

if TYPE_CHECKING:
    from .corporate_context import Context


def _spellings(entry: dict):
    yield entry["term"]
    yield from entry["aliases"]
    yield from entry.get("replacements", ())


class GlossaryFormIndex:
    """Shared by meetings using the same glossary snapshot, built on first discovery."""

    def __init__(self, terms: list[dict]):
        self.terms = terms

    @cached_property
    def by_form(self) -> dict[tuple[str, str], str | None]:
        targets = {}
        for entry in self.terms:
            department = glossary.normalize(entry.get("department", ""))
            canonical = entry["term"]
            for spelling in _spellings(entry):
                key = term_morphology.form_key(spelling)
                if key is None:
                    continue
                identity = key, department
                if targets.setdefault(identity, canonical) != canonical:
                    targets[identity] = None
        return targets

    def recognizes(self, key: str, department: str) -> bool:
        targets = {self.by_form[(key, scope)] for scope in {"", department}
                   if (key, scope) in self.by_form}
        return len(targets) == 1 and None not in targets


def is_known(context: Context, item: dict) -> bool:
    variant = " ".join(item["variant"].split())
    key = item["form_key"] if "form_key" in item else term_morphology.form_key(item["variant"], item["excerpt"])
    for index in context.index.get(item["normalized"], ()):
        entry = context.entries[index]
        # Keep existing exact directory-name and surname matches.
        if entry["source"] == "ad":
            return True
        if glossary.normalize(entry.get("department", "")) not in ("", context.department):
            continue
        for spelling in _spellings(entry):
            spelling = " ".join(spelling.split())
            if spelling == variant:
                # A quoted product or a nearby naming cue can give a common word a new meaning.
                if key is not None or term_morphology.form_key(spelling) is None:
                    return True
            elif (spelling.casefold() == variant.casefold()
                  and not any(term_morphology.dictionary_word(word) for word in variant.casefold().split())):
                # Different capitalization of the same non-word (e.g. ДВХ / двх).
                return True
    return key is not None and context.discovery_forms.recognizes(key, context.department)
