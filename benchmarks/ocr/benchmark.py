"""M16 benchmark harness — PaddleOCR + M9 date pipeline evaluation.

Runs the actual production OCR engine (`PaddleOCREngine`) and the production
M9 deterministic date pipeline (`detect_page_dates` + `choose_suggested_index`)
against a synthetic benchmark manifest, and reports date-extraction quality.

Usage:
    python benchmarks/ocr/benchmark.py [--output-dir DIR] [--limit N] [--dpi 300]

Writes:
    results.json       machine-readable per-fixture results
    results.csv        flattened table
    summary.md         human-readable report
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
import time
from datetime import date
from pathlib import Path

import django

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
os_env = __import__("os")
os_env.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings.local")
django.setup()


from benchmarks.ocr.fixtures import build_fixtures, render_fixture_bytes  # noqa: E402
from processing.dates import (  # noqa: E402
    CandidateType,
    choose_suggested_index,
    detect_page_dates,
)
from processing.ocr import (  # noqa: E402
    ImagePreprocessor,
    OCREngineUnavailableError,
    PaddleOCREngine,
)

DEFAULT_DPI = 300


def _native_text_from_pdf(content: bytes) -> str:
    import pymupdf

    doc = pymupdf.open(stream=content, filetype="pdf")
    try:
        pages = []
        for page in doc:
            pages.append(page.get_text("text"))
        return "\n".join(pages)
    finally:
        doc.close()


def _render_pdf_page(content: bytes, page_number: int, dpi: int):
    import pymupdf
    from PIL import Image

    doc = pymupdf.open(stream=content, filetype="pdf")
    try:
        page = doc[page_number - 1]
        pix = page.get_pixmap(dpi=dpi, alpha=False)
        return Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
    finally:
        doc.close()


def _ocr_image(engine, preprocessor, source):
    """OCR a source that is either raw image bytes or an already-decoded Image."""
    if isinstance(source, bytes):
        image = preprocessor.prepare(source)
    else:
        image = source
    return engine.extract_image(image)


class OCRUnavailable(Exception):
    pass


def run_pipeline(fixture, engine, preprocessor, *, dpi: int) -> dict:
    """Run production OCR/M9 on a fixture. Returns result metrics dict."""
    rendered = render_fixture_bytes(fixture)
    fmt = rendered["format"]
    started = time.monotonic()

    pages_text = []
    ocr_pages = []
    native_pages = []

    if fmt == "native_pdf":
        pages_text.append(_native_text_from_pdf(rendered["bytes"]))
        native_pages = [1]
    elif fmt == "mixed_pdf":
        import pymupdf

        doc = pymupdf.open(stream=rendered["bytes"], filetype="pdf")
        total = doc.page_count
        doc.close()
        for page_number in range(1, total + 1):
            page_text = _native_text_from_pdf_page(rendered["bytes"], page_number)
            if page_text and page_text.strip():
                pages_text.append(page_text)
                native_pages.append(page_number)
            else:
                image = _render_pdf_page(rendered["bytes"], page_number, dpi)
                result = _ocr_image(engine, preprocessor, image)
                pages_text.append(result.text)
                ocr_pages.append(page_number)
    else:
        # Image-based: render page(s) and OCR.
        if fmt == "image_pdf":
            image = _render_pdf_page(rendered["bytes"], 1, dpi)
            result = _ocr_image(engine, preprocessor, image)
        else:
            result = _ocr_image(engine, preprocessor, rendered["bytes"])
        pages_text.append(result.text)
        ocr_pages.append(1)

    duration_ms = int((time.monotonic() - started) * 1000)
    aggregate = "\n".join(pages_text)

    # M9 pipeline.
    candidates = detect_page_dates(
        aggregate,
        page_number=1,
        source="OCR" if ocr_pages and not native_pages else "PDF_TEXT",
        today=date(2026, 8, 9),
    )
    suggested_index = choose_suggested_index(candidates)
    suggested = candidates[suggested_index] if suggested_index is not None else None

    expected = fixture.expected_report_date
    candidate_dates = {c.detected_date for c in candidates}
    correct_in_candidates = expected is not None and expected in candidate_dates
    correct_suggested = (
        expected is not None
        and suggested is not None
        and suggested.detected_date == expected
    )
    wrong_suggested = (
        suggested is not None
        and expected is not None
        and suggested.detected_date != expected
    )
    dob_selected = (
        suggested is not None
        and suggested.candidate_type == CandidateType.DATE_OF_BIRTH
    )

    # Candidate-type accuracy: expected date's classified type.
    expected_type_ok = None
    if expected is not None and correct_in_candidates:
        matching = [c for c in candidates if c.detected_date == expected]
        expected_type_ok = (
            matching[0].candidate_type.value == fixture.expected_date_label
        )

    return {
        "id": fixture.id,
        "language": fixture.language,
        "format": fmt,
        "quality": fixture.quality,
        "layout": fixture.layout,
        "digits": fixture.digits,
        "expected_report_date": expected.isoformat() if expected else None,
        "expected_label": fixture.expected_date_label,
        "detected_in_candidates": correct_in_candidates,
        "candidate_count": len(candidates),
        "candidate_types": sorted({c.candidate_type.value for c in candidates}),
        "suggested_date": suggested.detected_date.isoformat() if suggested else None,
        "suggested_type": suggested.candidate_type.value if suggested else None,
        "suggested_score": suggested.score if suggested else None,
        "correct_suggested": correct_suggested,
        "wrong_suggested": wrong_suggested,
        "dob_selected": dob_selected,
        "expected_type_ok": expected_type_ok,
        "no_suggestion": suggested is None,
        "ocr_pages": len(ocr_pages),
        "native_pages": len(native_pages),
        "duration_ms": duration_ms,
        "notes": fixture.notes,
        "ocr_text_excerpt": aggregate[:120],
    }


def _native_text_from_pdf_page(content: bytes, page_number: int) -> str:
    import pymupdf

    doc = pymupdf.open(stream=content, filetype="pdf")
    try:
        return doc[page_number - 1].get_text("text")
    finally:
        doc.close()


def summarize(results: list[dict]) -> dict:
    with_expected = [r for r in results if r["expected_report_date"]]
    total = len(with_expected)
    detection = sum(1 for r in with_expected if r["detected_in_candidates"])
    correct = sum(1 for r in with_expected if r["correct_suggested"])
    wrong = sum(1 for r in with_expected if r["wrong_suggested"])
    nosugg = sum(1 for r in with_expected if r["no_suggestion"])
    suggestions = total - nosugg
    dob = sum(1 for r in with_expected if r["dob_selected"])
    type_ok = sum(1 for r in with_expected if r["expected_type_ok"] is True)
    type_checked = sum(1 for r in with_expected if r["expected_type_ok"] is not None)

    return {
        "documents_with_expected_date": total,
        "documents_processed": len(results),
        "date_detection_recall": detection / total if total else None,
        "suggestion_accuracy": correct / total if total else None,
        "suggestion_precision": correct / suggestions if suggestions else None,
        "wrong_suggestion_rate": wrong / total if total else None,
        "no_suggestion_rate": nosugg / total if total else None,
        "dob_false_selection_rate": dob / total if total else None,
        "candidate_type_accuracy": type_ok / type_checked if type_checked else None,
        "wrong_suggestion_count": wrong,
        "failed_documents": [r["id"] for r in with_expected if r["wrong_suggested"]],
        "no_suggestion_documents": [
            r["id"] for r in with_expected if r["no_suggestion"]
        ],
    }


def per_category(results: list[dict], key: str) -> dict:
    out = {}
    for value in sorted({r[key] for r in results}):
        subset = [r for r in results if r[key] == value and r["expected_report_date"]]
        total = len(subset)
        if not total:
            continue
        correct = sum(1 for r in subset if r["correct_suggested"])
        detect = sum(1 for r in subset if r["detected_in_candidates"])
        out[value] = {
            "n": total,
            "detection_recall": round(detect / total, 4),
            "suggestion_accuracy": round(correct / total, 4),
        }
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description="M16 OCR/date pipeline benchmark")
    parser.add_argument(
        "--output-dir", default=str(ROOT / "benchmarks" / "ocr" / "out")
    )
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--dpi", type=int, default=DEFAULT_DPI)
    parser.add_argument(
        "--set",
        choices=("frozen", "expanded", "all"),
        default="frozen",
        help="fixture set: frozen 34, expanded, or all",
    )
    args = parser.parse_args()

    from benchmarks.ocr.fixtures import build_expanded_fixtures

    if args.set == "expanded":
        fixtures = build_expanded_fixtures()
    elif args.set == "all":
        fixtures = build_fixtures() + build_expanded_fixtures()
    else:
        fixtures = build_fixtures()
    if args.limit:
        fixtures = fixtures[: args.limit]

    try:
        engine = PaddleOCREngine()
    except OCREngineUnavailableError as exc:
        print(f"OCR engine unavailable: {exc}")
        return 2
    preprocessor = ImagePreprocessor()

    results = []
    failures = []
    for fixture in fixtures:
        try:
            result = run_pipeline(fixture, engine, preprocessor, dpi=args.dpi)
            results.append(result)
            status = "OK"
        except Exception as exc:  # noqa: BLE001 - benchmark tolerates per-fixture errors
            failures.append({"id": fixture.id, "error": f"{type(exc).__name__}: {exc}"})
            status = f"ERR {type(exc).__name__}"
        print(f"[{status}] {fixture.id}")

    summary = summarize(results)
    out_dir = Path(args.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    (out_dir / "results.json").write_text(
        json.dumps(
            {"summary": summary, "results": results, "failures": failures}, indent=2
        )
    )

    with (out_dir / "results.csv").open("w", newline="") as fh:
        writer = csv.DictWriter(
            fh,
            fieldnames=[
                "id",
                "language",
                "format",
                "quality",
                "layout",
                "digits",
                "expected_report_date",
                "detected_in_candidates",
                "suggested_date",
                "suggested_type",
                "correct_suggested",
                "wrong_suggested",
                "dob_selected",
                "expected_type_ok",
                "no_suggestion",
                "ocr_pages",
                "native_pages",
                "duration_ms",
            ],
        )
        writer.writeheader()
        for r in results:
            writer.writerow({k: r.get(k) for k in writer.fieldnames})

    (out_dir / "summary.md").write_text(render_markdown(results, summary, args))

    print("\n=== SUMMARY ===")
    print(json.dumps(summary, indent=2))
    return 0


def render_markdown(results, summary, args) -> str:
    lines = [
        "# M16 OCR/Date Pipeline Benchmark",
        "",
        f"- Date: {date.today().isoformat()}",
        f"- Engine: PaddleOCR {getattr(__import__('paddleocr'), '__version__', '?')}",
        f"- DPI: {args.dpi}",
        f"- Documents: {len(results)}",
        "",
        "## Primary metrics",
        "",
    ]
    for key, label in (
        ("date_detection_recall", "Date Detection Recall"),
        ("suggestion_accuracy", "Suggestion Accuracy"),
        ("suggestion_precision", "Suggestion Precision"),
        ("wrong_suggestion_rate", "Wrong-Suggestion Rate"),
        ("no_suggestion_rate", "No-Suggestion Rate"),
        ("dob_false_selection_rate", "DOB False-Selection Rate"),
        ("candidate_type_accuracy", "Candidate-Type Accuracy"),
    ):
        value = summary.get(key)
        text = f"{value:.4f}" if isinstance(value, float) else "n/a"
        lines.append(f"- {label}: {text}")

    lines.append("")
    lines.append("## By language")
    lines.append("")
    lines.append("| language | n | detection | suggestion accuracy |")
    lines.append("|---|---|---|---|")
    for lang, stats in per_category(results, "language").items():
        lines.append(
            f"| {lang} | {stats['n']} | {stats['detection_recall']:.4f} | "
            f"{stats['suggestion_accuracy']:.4f} |"
        )

    lines.append("")
    lines.append("## By format")
    lines.append("")
    lines.append("| format | n | detection | suggestion accuracy |")
    lines.append("|---|---|---|---|")
    for fmt, stats in per_category(results, "format").items():
        lines.append(
            f"| {fmt} | {stats['n']} | {stats['detection_recall']:.4f} | "
            f"{stats['suggestion_accuracy']:.4f} |"
        )

    lines.append("")
    lines.append("## By quality")
    lines.append("")
    lines.append("| quality | n | detection | suggestion accuracy |")
    lines.append("|---|---|---|---|")
    for quality, stats in per_category(results, "quality").items():
        lines.append(
            f"| {quality} | {stats['n']} | {stats['detection_recall']:.4f} | "
            f"{stats['suggestion_accuracy']:.4f} |"
        )

    lines.append("")
    lines.append("## Wrong suggestions")
    lines.append("")
    if summary["failed_documents"]:
        for doc_id in summary["failed_documents"]:
            row = next(r for r in results if r["id"] == doc_id)
            lines.append(
                f"- `{doc_id}` expected {row['expected_report_date']} "
                f"got {row['suggested_date']} ({row['suggested_type']})"
            )
    else:
        lines.append("None.")
    lines.append("")
    return "\n".join(lines)


if __name__ == "__main__":
    raise SystemExit(main())
