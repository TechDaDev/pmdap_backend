"""M16.1 real-dataset evaluation harness.

Runs the production PDF/JPG processing path + M9 against a PAIRED private
evaluation set. The manifest (source paths + manual ground truth) is passed as
a file argument and must remain OUTSIDE Git. Results are anonymized:
only fixture_id / page / expected / detected / suggested / category / runtime.

The manifest format (private, not committed):

    [
      {
        "id": "real-001-page-01",
        "pdf": "/abs/path/to.pdf",
        "page": 1,
        "jpg": "/abs/path/to.jpg",
        "expected_report_date": "2025-09-17",   # manual ground truth, or null
        "expected_label": "REPORT_DATE"
      }
    ]

Usage:
    python benchmarks/ocr/real_benchmark.py --manifest /tmp/manifest.json [--out DIR]
"""

from __future__ import annotations

import argparse
import json
import time
from datetime import date
from pathlib import Path

import django

ROOT = Path(__file__).resolve().parents[2]
import sys  # noqa: E402

sys.path.insert(0, str(ROOT))
import os  # noqa: E402

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings.local")
django.setup()

from processing.dates import (  # noqa: E402
    choose_suggested_index,
    detect_page_dates,
)
from processing.ocr import (  # noqa: E402
    ImagePreprocessor,
    PaddleOCREngine,
)

TODAY = date(2026, 8, 10)


def _pdf_native_text(path: str, page: int) -> str:
    import pymupdf

    doc = pymupdf.open(path)
    try:
        return doc[page - 1].get_text("text")
    finally:
        doc.close()


def _pdf_render_page(path: str, page: int, dpi: int = 300):
    import pymupdf
    from PIL import Image

    doc = pymupdf.open(path)
    try:
        pix = doc[page - 1].get_pixmap(dpi=dpi, alpha=False)
        return Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
    finally:
        doc.close()


def _run_m9(text: str) -> dict:
    candidates = detect_page_dates(text, page_number=1, source="OCR", today=TODAY)
    index = choose_suggested_index(candidates)
    suggested = candidates[index] if index is not None else None
    return {
        "candidate_dates": {c.detected_date.isoformat() for c in candidates},
        "candidate_types": sorted({c.candidate_type.value for c in candidates}),
        "suggested_date": suggested.detected_date.isoformat() if suggested else None,
        "suggested_type": suggested.candidate_type.value if suggested else None,
        "candidate_count": len(candidates),
    }


def evaluate_pdf(entry, engine, preprocessor) -> dict:
    """Production-like: use native text if present, else OCR the rendered page."""
    started = time.monotonic()
    native = _pdf_native_text(entry["pdf"], entry["page"])
    if native.strip():
        m9 = _run_m9(native)
        m9["source"] = "PDF_TEXT"
    else:
        image = _pdf_render_page(entry["pdf"], entry["page"])
        result = engine.extract_image(preprocessor.prepare(_img_bytes(image)))
        m9 = _run_m9(result.text)
        m9["source"] = "OCR"
    m9["runtime_ms"] = int((time.monotonic() - started) * 1000)
    return m9


def evaluate_jpg(entry, engine, preprocessor) -> dict:
    started = time.monotonic()
    with open(entry["jpg"], "rb") as fh:
        content = fh.read()
    result = engine.extract_image(preprocessor.prepare(content))
    m9 = _run_m9(result.text)
    m9["source"] = "OCR"
    m9["runtime_ms"] = int((time.monotonic() - started) * 1000)
    return m9


def _img_bytes(image):
    import io

    buf = io.BytesIO()
    image.save(buf, format="PNG")
    return buf.getvalue()


def classify(entry, m9) -> dict:
    expected = entry.get("expected_report_date")
    expected_date = date.fromisoformat(expected) if expected else None
    suggested = (
        date.fromisoformat(m9["suggested_date"]) if m9["suggested_date"] else None
    )
    if expected_date is None:
        correct_suggested = m9["suggested_date"] is None  # blank field → no suggestion
        detected = True  # nothing to detect; treat as clean
    else:
        detected_dates = {date.fromisoformat(d) for d in m9["candidate_dates"]}
        detected = expected_date in detected_dates
        correct_suggested = suggested == expected_date
    return {
        "detected_in_candidates": detected,
        "correct_suggested": correct_suggested,
        "wrong_suggested": bool(
            suggested is not None
            and expected_date is not None
            and suggested != expected_date
        ),
        "no_suggestion": m9["suggested_date"] is None,
        "dob_selected": m9["suggested_type"] == "DATE_OF_BIRTH",
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--out", default=str(ROOT / "benchmarks" / "ocr" / "out"))
    parser.add_argument("--dpi", type=int, default=300)
    args = parser.parse_args()

    manifest = json.loads(Path(args.manifest).read_text())
    engine = PaddleOCREngine()
    preprocessor = ImagePreprocessor()

    rows = []
    for entry in manifest:
        pdf_m9 = evaluate_pdf(entry, engine, preprocessor)
        jpg_m9 = evaluate_jpg(entry, engine, preprocessor)
        row = {
            "id": entry["id"],
            "page": entry["page"],
            "expected": entry.get("expected_report_date"),
            "pdf_source": pdf_m9["source"],
            "pdf_candidates": sorted(pdf_m9["candidate_dates"]),
            "pdf_types": pdf_m9["candidate_types"],
            "pdf_suggested": pdf_m9["suggested_date"],
            "pdf_suggested_type": pdf_m9["suggested_type"],
            "pdf_runtime_ms": pdf_m9["runtime_ms"],
            "jpg_candidates": sorted(jpg_m9["candidate_dates"]),
            "jpg_types": jpg_m9["candidate_types"],
            "jpg_suggested": jpg_m9["suggested_date"],
            "jpg_suggested_type": jpg_m9["suggested_type"],
            "jpg_runtime_ms": jpg_m9["runtime_ms"],
        }
        row.update({f"pdf_{k}": v for k, v in classify(entry, pdf_m9).items()})
        row.update({f"jpg_{k}": v for k, v in classify(entry, jpg_m9).items()})
        rows.append(row)
        print(
            f"[{entry['id']}] pdf_sugg={row['pdf_suggested']} "
            f"jpg_sugg={row['jpg_suggested']} exp={row['expected']}"
        )

    # Aggregates.
    n = len(rows)
    pdf_det = sum(1 for r in rows if r["pdf_detected_in_candidates"])
    pdf_acc = sum(1 for r in rows if r["pdf_correct_suggested"])
    pdf_wrong = sum(1 for r in rows if r["pdf_wrong_suggested"])
    pdf_nos = sum(1 for r in rows if r["pdf_no_suggestion"])
    jpg_det = sum(1 for r in rows if r["jpg_detected_in_candidates"])
    jpg_acc = sum(1 for r in rows if r["jpg_correct_suggested"])
    jpg_wrong = sum(1 for r in rows if r["jpg_wrong_suggested"])
    jpg_nos = sum(1 for r in rows if r["jpg_no_suggestion"])
    pdf_dob = sum(1 for r in rows if r["pdf_dob_selected"])
    jpg_dob = sum(1 for r in rows if r["jpg_dob_selected"])

    # Pair consistency: both PDF and JPG suggested the correct expected date.
    valid_pairs = [r for r in rows if r["expected"] is not None]
    pair_consistent = sum(
        1
        for r in valid_pairs
        if r["pdf_correct_suggested"] and r["jpg_correct_suggested"]
    )

    summary = {
        "pages": n,
        "pdf_date_detection_recall": round(pdf_det / n, 4) if n else None,
        "pdf_suggestion_accuracy": round(pdf_acc / n, 4) if n else None,
        "pdf_wrong_suggestion_rate": round(pdf_wrong / n, 4) if n else None,
        "pdf_no_suggestion_rate": round(pdf_nos / n, 4) if n else None,
        "pdf_dob_false_selection": pdf_dob,
        "jpg_date_detection_recall": round(jpg_det / n, 4) if n else None,
        "jpg_suggestion_accuracy": round(jpg_acc / n, 4) if n else None,
        "jpg_wrong_suggestion_rate": round(jpg_wrong / n, 4) if n else None,
        "jpg_no_suggestion_rate": round(jpg_nos / n, 4) if n else None,
        "jpg_dob_false_selection": jpg_dob,
        "pair_consistency_rate": (
            round(pair_consistent / len(valid_pairs), 4) if valid_pairs else None
        ),
    }
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    (out / "real_summary.json").write_text(json.dumps(summary, indent=2))
    # page-level rows are kept local; not persisted to avoid medical content.
    print("\n=== REAL SUMMARY ===")
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
