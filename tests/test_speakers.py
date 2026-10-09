from backend.speakers import assign_speakers, reassign_segments, rename_speaker, render_transcript, replace_speaker_label


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
