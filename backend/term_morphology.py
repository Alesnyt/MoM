"""Conservative keys for reviewing Russian inflections in the discovery queue."""
from __future__ import annotations

import re
from functools import lru_cache
from threading import Lock
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pymorphy3 import MorphAnalyzer


_RUSSIAN_PHRASE = re.compile(r"[а-яё]+(?: +[а-яё]+){0,5}")
_DICTIONARY_LOCK = Lock()
_NAME_TAGS = {"Name", "Surn", "Patr", "Geox", "Orgn", "Trad", "Abbr"}
_QUOTED_PHRASE = re.compile(r'«[^«»]*»|„[^„“]*“|“[^“”]*”|"[^"]*"')
_NAME_PREFIX = re.compile(
    r'(?:[«„“"]\s*|\b(?:под названием|называется|называем|'
    r'систем(?:а|у|ы|е|ой)|сервис(?:а|у|е|ом)?|портал(?:а|у|е|ом)?|'
    r'приложени(?:е|я|и|ем)|продукт(?:а|у|е|ом)?|платформ(?:а|у|ы|е|ой))\s*[:—–]?\s+)$',
    re.IGNORECASE,
)


@lru_cache(maxsize=1)
def _load_dictionary() -> MorphAnalyzer:
    # No guesses about unknown words or ranking of competing dictionary readings.
    from pymorphy3 import MorphAnalyzer
    from pymorphy3.units import DictionaryAnalyzer

    return MorphAnalyzer(lang="ru", units=[DictionaryAnalyzer()], probability_estimator_cls=None)


def _dictionary() -> MorphAnalyzer:
    # lru_cache permits concurrent first calls. Normalized Parse objects carry
    # their analyzer instance and must all come from the same dictionary.
    with _DICTIONARY_LOCK:
        return _load_dictionary()


def dictionary_word(text: str) -> bool:
    """Whether this exact Cyrillic spelling has a dictionary entry, even if ambiguous."""
    return (len(text) <= 80 and re.fullmatch(r"[А-Яа-яЁё]+", text) is not None
            and _dictionary().word_is_known(text, strict=True))


@lru_cache(maxsize=8192)
def _readings(word: str) -> tuple:
    readings = _dictionary().parse(word)
    if any(r.word != word or r.tag.grammemes & _NAME_TAGS for r in readings):
        return ()
    return tuple(readings)


@lru_cache(maxsize=8192)
def _lexeme(word: str):
    # A single word can have several case readings within the same paradigm.
    normalized = {r.normalized for r in _readings(word)}
    if len(normalized) != 1:
        return None
    token = normalized.pop()
    if token.tag.POS not in {"NOUN", "ADJF"}:
        return None
    # Different inflected homonyms can have the same printed dictionary form.
    dictionary_forms = {r.normalized for r in _readings(token.word)}
    if dictionary_forms != {token}:
        return None
    return token


def name_context(text: str, start: int) -> bool:
    """Whether this exact occurrence has a nearby naming cue or lies inside quotes."""
    return (bool(_NAME_PREFIX.search(text, 0, start))
            or any(phrase.start() < start < phrase.end() for phrase in _QUOTED_PHRASE.finditer(text)))


def _name_in_excerpt(text: str, excerpt: str) -> bool:
    if not excerpt:
        return False
    for match in re.finditer(r"(?<!\w)" + re.escape(text) + r"(?!\w)", excerpt, re.IGNORECASE):
        if match.group() != text or name_context(excerpt, match.start()):
            return True
    return False


def _agrees(adjective, noun) -> bool:
    return (adjective.case == noun.case and adjective.number == noun.number
            and (noun.number == "plur" or adjective.gender == noun.gender))


def form_key(text: str, excerpt: str = "") -> str | None:
    """A review-only key, or None for spellings requiring phonetic/manual review."""
    if len(text) > 80 or not _RUSSIAN_PHRASE.fullmatch(text) or _name_in_excerpt(text, excerpt):
        return None
    words = text.split()
    dative = words[0] in {"к", "ко"}
    if dative:
        words = words[1:]
    if not words:
        return None
    if len(words) == 1 and not dative:
        reading = _lexeme(words[0])
        return f"{reading.tag.POS}:{reading.word}" if reading else None

    # Inflect only the nominal head and its agreeing adjectives. Keep dependents
    # (including ambiguous but unchanged words such as «данных») verbatim.
    adjectives = []
    for index, word in enumerate(words):
        head = _lexeme(word)
        if head is None:
            return None
        if head.tag.POS == "NOUN":
            break
        adjectives.append(word)
    else:
        return None
    tail = words[index + 1:]
    if any(not _readings(word) for word in tail):
        return None
    noun_tags = [r.tag for r in _readings(words[index]) if not dative or r.tag.case == "datv"]
    if not any(all(any(_agrees(a.tag, noun) for a in _readings(word))
                   for word in adjectives) for noun in noun_tags):
        return None
    target = {"nomn", head.tag.number}
    if head.tag.number == "sing" and head.tag.gender:
        target.add(head.tag.gender)
    label = []
    for word in adjectives:
        agreed = _lexeme(word).inflect(target)
        if agreed is None:
            return None
        label.append(agreed.word)
    return "NOUN:" + " ".join([*label, head.word, *tail])


def form_label(key: str) -> str:
    """Human-readable nominative phrase carried in a morphology key."""
    return key.partition(":")[2]
