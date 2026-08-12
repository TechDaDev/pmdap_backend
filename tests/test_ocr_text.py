"""ocr_text() parsing tests — fake engine, synthetic text only."""
from identities import extraction


class _FakeResult:
    def __init__(self, rec_texts):
        self.rec_texts = rec_texts


class _FakeEngine:
    def __init__(self, results):
        self._results = results

    def predict(self, image_path):
        return iter(self._results)


def test_ocr_text_parses_rec_texts_from_paddlex_results(monkeypatch):
    engine = _FakeEngine(
        [
            _FakeResult(["SYNTHETIC TEST DOCUMENT", "NATIONAL NO: 012345678901234"]),
            _FakeResult(["FAMILY NO: 1234"]),
        ]
    )
    monkeypatch.setattr(extraction, "_load_ocr", lambda: engine)

    lines = extraction.ocr_text("/tmp/nonexistent.png")
    assert lines == [
        "SYNTHETIC TEST DOCUMENT",
        "NATIONAL NO: 012345678901234",
        "FAMILY NO: 1234",
    ]


def test_ocr_text_handles_dict_results_and_blank_lines(monkeypatch):
    engine = _FakeEngine(
        [
            {"rec_texts": ["  A123456789  ", "", "ISSUE DATE: 2021-01-15"]},
        ]
    )
    monkeypatch.setattr(extraction, "_load_ocr", lambda: engine)

    lines = extraction.ocr_text("/tmp/nonexistent.png")
    assert lines == ["A123456789", "ISSUE DATE: 2021-01-15"]


def test_ocr_text_missing_rec_texts_is_safe(monkeypatch):
    engine = _FakeEngine([{"box": "x"}, _FakeResult(None)])
    monkeypatch.setattr(extraction, "_load_ocr", lambda: engine)
    assert extraction.ocr_text("/tmp/nonexistent.png") == []


def test_ocr_text_returns_empty_when_engine_unavailable(monkeypatch):
    monkeypatch.setattr(extraction, "_load_ocr", lambda: None)
    assert extraction.ocr_text("/tmp/nonexistent.png") == []


def test_ocr_text_returns_empty_on_predict_failure(monkeypatch):
    class _Broken:
        def predict(self, image_path):
            raise RuntimeError("boom")

    monkeypatch.setattr(extraction, "_load_ocr", lambda: _Broken())
    assert extraction.ocr_text("/tmp/nonexistent.png") == []
