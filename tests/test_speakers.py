from backend.speakers import (
    UNKNOWN_ID,
    UNKNOWN_NAME,
    assign_speakers,
    expand_coarse_segments,
    plan_cuts,
    reassign_segments,
    rename_speaker,
    render_transcript,
    replace_speaker_label,
)


def test_assign_by_overlap() -> None:
    segments = [
        {"start": 0.0, "end": 2.0, "text": "привет"},
        {"start": 2.2, "end": 4.0, "text": "добрый день"},
    ]
    turns = [
        {"start": 0.0, "end": 2.1, "speaker": "SPEAKER_00"},
        {"start": 2.1, "end": 5.0, "speaker": "SPEAKER_01"},
    ]
    document = assign_speakers(segments, turns)
    assert [item["speaker"] for item in document["segments"]] == ["S1", "S2"]
    assert document["speakers"][0]["name"] == "Спикер 1"
    text = render_transcript(document)
    assert "[00:00] Спикер 1: привет" in text
    assert "[00:02] Спикер 2: добрый день" in text


def test_long_chunk_is_cut_on_voice_boundaries() -> None:
    segment = {"start": 0.0, "end": 24.0, "text": "весь кусок целиком"}
    turns = [
        {"start": 0.0, "end": 11.0, "speaker": "A"},
        {"start": 12.0, "end": 24.0, "speaker": "B"},
    ]
    cuts = plan_cuts(segment, turns)
    assert cuts == [{"start": 0.0, "end": 11.0}, {"start": 12.0, "end": 24.0}]
    expanded = expand_coarse_segments(
        [segment],
        turns,
        lambda start, end: "первая" if start < 12 else "вторая",
    )
    assert [item["text"] for item in expanded] == ["первая", "вторая"]
    document = assign_speakers(expanded, turns)
    assert [item["speaker"] for item in document["segments"]] == ["S1", "S2"]


def test_short_phrase_stays_whole() -> None:
    assert plan_cuts({"start": 0, "end": 3, "text": "коротко"}, [{"start": 0, "end": 1.5, "speaker": "A"}, {"start": 1.5, "end": 3, "speaker": "B"}]) is None


def test_one_voice_in_a_long_chunk_is_not_cut() -> None:
    turns = [{"start": 1.0, "end": 20.0, "speaker": "A"}]
    assert plan_cuts({"start": 0, "end": 24, "text": "монолог"}, turns) is None


def test_unmatched_phrase_is_not_given_to_the_first_speaker() -> None:
    document = assign_speakers(
        [{"start": 0.0, "end": 2.0, "text": "мимо"}],
        [{"start": 10.0, "end": 12.0, "speaker": "A"}],
    )
    assert document["segments"][0]["speaker"] == UNKNOWN_ID
    assert document["speakers"][-1]["name"] == UNKNOWN_NAME
    assert "Неясно: мимо" in render_transcript(document)


def test_without_turns_everything_stays_one_speaker() -> None:
    document = assign_speakers([{"start": 0, "end": 2, "text": "один"}], [])
    assert document["segments"][0]["speaker"] == "S1"
    assert UNKNOWN_ID not in {item["id"] for item in document["speakers"]}


def test_rename_does_not_eat_part_of_another_name() -> None:
    result = {"summary": "Анна сказала. Анна-Мария молчала."}
    updated = replace_speaker_label(result, "Анна", "Мария")
    assert updated["summary"] == "Мария сказала. Анна-Мария молчала."
    result = {"summary": "Спикер 1 сказал. Спикер 10 молчал."}
    updated = replace_speaker_label(result, "Спикер 1", "Мария")
    assert updated["summary"] == "Мария сказал. Спикер 10 молчал."


def test_rename_and_reassign() -> None:
    document = assign_speakers(
        [{"start": 0, "end": 1, "text": "а"}, {"start": 1, "end": 2, "text": "б"}],
        [{"start": 0, "end": 2, "speaker": "A"}],
    )
    renamed, old, new = rename_speaker(document, "S1", "  Анна  ")
    assert old == "Спикер 1"
    assert new == "Анна"
    assert renamed["speakers"][0]["name"] == "Анна"
    moved = reassign_segments(renamed, [1], "new")
    assert moved["segments"][1]["speaker"] == "S2"
    assert moved["speakers"][1]["name"] == "Спикер 2"
