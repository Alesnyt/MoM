"""Regression cases for the packaged Russian dictionary, including ambiguous readings."""
import pytest

from backend.term_morphology import dictionary_word, form_key, form_label, name_context


@pytest.mark.parametrize("forms, title", [
    (["проект", "проекта", "проектов", "проектами"], "проект"),
    (["витрина данных", "витрину данных", "к витрине данных"], "витрина данных"),
    (["единое окно", "единого окна", "к единому окну"], "единое окно"),
    (["проекта без ошибок", "проектами без ошибок"], "проект без ошибок"),
])
def test_case_and_number_forms_share_a_readable_key(forms, title):
    base = form_key(forms[0])
    assert base and form_label(base) == title
    assert all(form_key(form) == base for form in forms)


@pytest.mark.parametrize("term", [
    "стали", "лук", "все", "всё", "елка", "АНАЛИТИКА", "Контур", "FineBI", "SAP-2",
    "мвд", "двх", "дэ вэ ха", "файн би", "проект-2025", "проект файн", "«витрина»",
    "анна", "москва", "к витрина данных", "сетевую проект",
])
def test_uncertain_terms_stay_for_manual_review(term):
    assert form_key(term) is None


def test_keep_original_word_order_dependents_and_negation():
    key = form_key("витрина данных")
    assert key
    for other in ["данные витрины", "витрина проекта", "витрина без данных", "не витрина данных"]:
        assert form_key(other) != key
    assert form_key("ёлка") == form_key("ёлками") != form_key("елка")


@pytest.mark.parametrize("context", [
    "Заявку завели в системе контур.",
    "Открой «контур» и посмотри.",
    "На встрече показали «внутренний контур предприятия».",
    "Работаем в приложении Контур.",
    "Эту систему называем контур.",
    "В один контур входят разные продукты, в том числе сервис контур.",
])
def test_named_use_keeps_the_original_evidence_for_review(context):
    assert form_key("контур", context) is None
    assert form_key("контур", "Обсудили внутренний контур безопасности.") == form_key("контура")


def test_detect_named_use_at_the_exact_occurrence():
    text = "Проверь контур и открой сервис контур."
    assert not name_context(text, text.index("контур"))
    assert name_context(text, text.rindex("контур"))


def test_exact_dictionary_membership_includes_ambiguous_readings():
    for word in ["ЛУК", "МИР", "стали", "ёлка"]:
        assert dictionary_word(word)
    for word in ["двх", "елка", "витрина данных", "FineBI"]:
        assert not dictionary_word(word)
