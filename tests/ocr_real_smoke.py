"""Generate synthetic M8 fixtures and run the real PaddleOCR adapter.

Run after installing ``requirements/ocr.txt``:

    DJANGO_SETTINGS_MODULE=config.settings.test \
      python -m tests.ocr_real_smoke
"""

import json
import os
import tempfile
from pathlib import Path

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings.test")

import django  # noqa: E402
import pymupdf  # noqa: E402
from PIL import Image, ImageDraw, ImageFont, features  # noqa: E402

django.setup()

from processing.ocr import (  # noqa: E402
    ImagePreprocessor,
    PaddleOCREngine,
    PDFPageRenderer,
)

FIXTURE_TEXT = {
    "english": "Patient Report\nReport Date: 14/03/2026",
    "arabic": "تقرير طبي\nتاريخ التقرير: ١٤/٠٣/٢٠٢٦",
    "mixed": "Medical Report\nتاريخ: ١٤/٠٣/٢٠٢٦",
}
FONT_CANDIDATES = (
    Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"),
    Path("/usr/share/fonts/dejavu/DejaVuSans.ttf"),
)


def _font():
    for path in FONT_CANDIDATES:
        if path.exists():
            return ImageFont.truetype(str(path), 54)
    raise RuntimeError("A DejaVu Sans font is required for synthetic OCR fixtures.")


def _write_image(path, lines):
    image = Image.new("RGB", (1600, 440), "white")
    draw = ImageDraw.Draw(image)
    font = _font()
    for index, (text, language) in enumerate(lines):
        y = 70 + index * 150
        if language == "ar" and features.check("raqm"):
            draw.text(
                (1520, y),
                text,
                fill="black",
                font=font,
                anchor="ra",
                direction="rtl",
                language="ar",
            )
        else:
            draw.text((80, y), text, fill="black", font=font)
    image.save(path, format="PNG")
    image.close()


def generate_fixtures(root):
    root.mkdir(parents=True, exist_ok=True)
    _write_image(
        root / "english.png",
        (("Patient Report", "en"), ("Report Date: 14/03/2026", "en")),
    )
    _write_image(
        root / "arabic.png",
        (("تقرير طبي", "ar"), ("تاريخ التقرير: ١٤/٠٣/٢٠٢٦", "ar")),
    )
    _write_image(
        root / "mixed.png",
        (("Medical Report", "en"), ("تاريخ: ١٤/٠٣/٢٠٢٦", "ar")),
    )

    image_only = pymupdf.open()
    page = image_only.new_page(width=800, height=220)
    page.insert_image(page.rect, filename=str(root / "mixed.png"))
    image_only.save(root / "image-only.pdf")
    image_only.close()

    mixed = pymupdf.open()
    native_page = mixed.new_page(width=800, height=220)
    native_page.insert_text((50, 80), "Native synthetic report page")
    scanned_page = mixed.new_page(width=800, height=220)
    scanned_page.insert_image(scanned_page.rect, filename=str(root / "arabic.png"))
    mixed.save(root / "mixed.pdf")
    mixed.close()


def _extract_image(engine, preprocessor, path):
    prepared = preprocessor.prepare(path.read_bytes())
    try:
        return engine.extract_image(prepared).text
    finally:
        prepared.close()


def _extract_pdf_page(engine, renderer, path, page_number):
    rendered = renderer.render(path.read_bytes(), page_number)
    try:
        return engine.extract_image(rendered).text
    finally:
        rendered.close()


def main():
    with tempfile.TemporaryDirectory(prefix="pmdap-m8-ocr-") as directory:
        root = Path(directory)
        generate_fixtures(root)
        engine = PaddleOCREngine()
        preprocessor = ImagePreprocessor()
        renderer = PDFPageRenderer()
        observed = {
            name: _extract_image(engine, preprocessor, root / f"{name}.png")
            for name in FIXTURE_TEXT
        }
        observed["image_only_pdf"] = _extract_pdf_page(
            engine, renderer, root / "image-only.pdf", 1
        )
        observed["mixed_pdf_scanned_page"] = _extract_pdf_page(
            engine, renderer, root / "mixed.pdf", 2
        )
        print(json.dumps(observed, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
