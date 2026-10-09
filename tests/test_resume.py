from backend.pipeline import has_saved_transcript


def test_resume_only_when_transcript_exists() -> None:
    assert has_saved_transcript({"transcript": "Спикер 1: текст"})
    assert has_saved_transcript({"transcript": "  "}) is False
    assert has_saved_transcript({}) is False
