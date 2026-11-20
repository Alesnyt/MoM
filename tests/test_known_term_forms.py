from copy import copy

from backend import glossary, known_term_forms as known
from backend.corporate_context import Context


def entry(term, aliases=(), department=""):
    return {"term": term, "aliases": list(aliases), "department": department}


def matches(context, term, excerpt=None):
    return known.is_known(context, {"variant": term, "normalized": glossary.normalize(term),
                                    "excerpt": excerpt or term})


def test_inflected_phrase_is_covered_by_glossary_inside_its_scope():
    base = Context([entry("витрина данных", department="Аналитика")], [], 1)
    scoped = copy(base)
    scoped.department = glossary.normalize("Аналитика")
    assert "by_form" not in vars(base.discovery_forms)
    assert matches(scoped, "к витрине данных")
    assert not matches(base, "к витрине данных")
    assert not matches(base, "витрина данных")
    assert base.discovery_forms is scoped.discovery_forms


def test_ambiguous_glossary_targets_keep_candidate_visible():
    context = Context([entry("Alpha", ["проекта"]), entry("Beta", ["проектами"])], [], 1)
    assert not matches(context, "проектов")
    assert matches(context, "проекта")


def test_context_suggesting_brand_survives_common_word_match():
    context = Context([entry("контур"), entry("ДВХ"), entry("лук")], [], 1)
    assert matches(context, "контур")
    assert not matches(context, "Контур", "Откроем систему Контур.")
    assert not matches(context, "контур", "Запустим систему «контур».")
    assert matches(context, "двх")
    assert not matches(context, "ЛУК")


def test_use_exact_ad_names_without_adding_them_to_form_index():
    context = Context([], [{"name": "Иван Проектов", "surname": "Проектов"}], 1)
    assert matches(context, "Проектов")
    assert not matches(context, "проектами")
