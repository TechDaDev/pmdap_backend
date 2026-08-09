"""Synthetic report fixture generation for the M16 OCR/date benchmark.

Every fixture is synthetic. English lines are rendered with PIL (DejaVu);
Arabic/mixed lines use PyMuPDF's HTML engine (HarfBuzz shaping + RTL) so the
Arabic glyphs are correctly connected and ordered. Native-text PDFs are a
control group; scanned images and scanned PDFs exercise the real OCR path.

Ground truth is authored here (expected report date + label) — never derived
from the OCR engine.
"""

from __future__ import annotations

import io
from dataclasses import dataclass, field
from datetime import date

from PIL import Image, ImageDraw, ImageEnhance, ImageFilter, ImageFont

ARABIC_INDIC = str.maketrans("0123456789", "٠١٢٣٤٥٦٧٨٩")
PERSIAN = str.maketrans("0123456789", "۰۱۲۳۴۵۶۷۸۹")


@dataclass
class FixtureLine:
    text: str
    kind: str = "text"  # text | report_date | result_date | dob | ...


@dataclass
class Fixture:
    id: str
    language: str  # en | ar | mixed
    format: str  # image_png | image_jpeg | image_pdf | mixed_pdf | native_pdf
    quality: str  # clean | low_contrast | blur | rotation | small_font | noise
    layout: str  # simple | table | multi_column | header_footer |
    #             multiple_dates | lab_header | footer | stamp
    digits: str  # western | arabic_indic | persian
    lines: list[FixtureLine] = field(default_factory=list)
    expected_report_date: date | None = None
    expected_date_label: str = "REPORT_DATE"
    other_dates: list[tuple[date, str]] = field(default_factory=list)
    notes: str = ""

    def render_date(self, value: date) -> str:
        raw = value.strftime("%d/%m/%Y")
        if self.digits == "arabic_indic":
            return raw.translate(ARABIC_INDIC)
        if self.digits == "persian":
            return raw.translate(PERSIAN)
        return raw


ARABIC_FONT = "/usr/share/fonts/truetype/noto/NotoSansArabic-Regular.ttf"
EN_FONT = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"


def _d(date_str):
    day, month, year = date_str.split("/")
    return date(int(year), int(month), int(day))


# ── Fixture library ──────────────────────────────────────────────────


def _en_simple(prefix="m16-en-simple"):
    return Fixture(
        id=f"{prefix}",
        language="en",
        format="image_png",
        quality="clean",
        layout="simple",
        digits="western",
        lines=[
            FixtureLine("MEDICAL LABORATORY REPORT"),
            FixtureLine("Patient: A. Sample"),
            FixtureLine("Report Date: 14/03/2026", "report_date"),
            FixtureLine("Hemoglobin: 13.4 g/dL"),
        ],
        expected_report_date=_d("14/03/2026"),
        expected_date_label="REPORT_DATE",
    )


def _en_multidate():
    return Fixture(
        id="m16-en-multidate",
        language="en",
        format="image_png",
        quality="clean",
        layout="multiple_dates",
        digits="western",
        lines=[
            FixtureLine("HOSPITAL PATIENT REPORT"),
            FixtureLine("Date of Birth: 21/06/1985", "dob"),
            FixtureLine("Collection Date: 12/07/2026", "collection"),
            FixtureLine("Report Date: 14/07/2026", "report_date"),
            FixtureLine("Print Date: 15/07/2026", "print"),
        ],
        expected_report_date=_d("14/07/2026"),
        expected_date_label="REPORT_DATE",
        other_dates=[
            (_d("21/06/1985"), "DOB"),
            (_d("12/07/2026"), "COLLECTION"),
            (_d("15/07/2026"), "PRINT"),
        ],
    )


def _en_footer():
    return Fixture(
        id="m16-en-footer",
        language="en",
        format="image_png",
        quality="clean",
        layout="footer",
        digits="western",
        lines=[
            FixtureLine("PATHOLOGY DEPARTMENT"),
            FixtureLine("Specimen: CBC EDTA"),
            FixtureLine("Result: Normal range"),
            FixtureLine("Issued 14/03/2026", "report_date"),
            FixtureLine("Page 1 of 1"),
        ],
        expected_report_date=_d("14/03/2026"),
        expected_date_label="REPORT_DATE",
    )


def _en_lab_header():
    return Fixture(
        id="m16-en-labheader",
        language="en",
        format="image_png",
        quality="clean",
        layout="lab_header",
        digits="western",
        lines=[
            FixtureLine("LABORATORY RESULT"),
            FixtureLine("Report Date: 14/03/2026", "report_date"),
            FixtureLine("Test | Result | Unit"),
            FixtureLine("WBC  | 7.4   | K/uL"),
        ],
        expected_report_date=_d("14/03/2026"),
        expected_date_label="REPORT_DATE",
    )


def _en_table():
    return Fixture(
        id="m16-en-table",
        language="en",
        format="image_png",
        quality="clean",
        layout="table",
        digits="western",
        lines=[
            FixtureLine("BIOCHEMISTRY PANEL"),
            FixtureLine("Report Date: 02/04/2026", "report_date"),
            FixtureLine("Analyte  Value  Ref"),
            FixtureLine("Glucose  5.2    3.9-6.1"),
            FixtureLine("ALT      22     7-56"),
        ],
        expected_report_date=_d("02/04/2026"),
        expected_date_label="REPORT_DATE",
    )


def _ar_simple():
    return Fixture(
        id="m16-ar-simple",
        language="ar",
        format="image_pdf",
        quality="clean",
        layout="simple",
        digits="western",
        lines=[
            FixtureLine("مختبر التحاليل الطبية"),
            FixtureLine("اسم المريض: عينة"),
            FixtureLine("تاريخ التقرير: 14/03/2026", "report_date"),
            FixtureLine("الهيموغلوبين: 13.4"),
        ],
        expected_report_date=_d("14/03/2026"),
        expected_date_label="REPORT_DATE",
    )


def _ar_separate_line():
    return Fixture(
        id="m16-ar-separate-line",
        language="ar",
        format="image_pdf",
        quality="clean",
        layout="simple",
        digits="western",
        lines=[
            FixtureLine("تقرير المختبر الطبي"),
            FixtureLine("تاريخ التقرير"),
            FixtureLine("14/03/2026", "report_date"),
            FixtureLine("الهيموغلوبين: 13.4"),
        ],
        expected_report_date=_d("14/03/2026"),
        expected_date_label="REPORT_DATE",
        notes="Date on its own line below the Arabic label.",
    )


def _ar_separate_indic():
    return Fixture(
        id="m16-ar-separate-indic",
        language="ar",
        format="image_pdf",
        quality="clean",
        layout="simple",
        digits="arabic_indic",
        lines=[
            FixtureLine("مستشفى المدينة"),
            FixtureLine("تاريخ التقرير"),
            FixtureLine("١٤/٠٣/٢٠٢٦", "report_date"),
        ],
        expected_report_date=_d("14/03/2026"),
        expected_date_label="REPORT_DATE",
        notes="Arabic-Indic date on its own line.",
    )


def _ar_indic():
    return Fixture(
        id="m16-ar-indic",
        language="ar",
        format="image_pdf",
        quality="clean",
        layout="simple",
        digits="arabic_indic",
        lines=[
            FixtureLine("مستشفى المدينة"),
            FixtureLine("تاريخ التقرير: 14/03/2026", "report_date"),
            FixtureLine("تاريخ الميلاد: 21/06/1985", "dob"),
        ],
        expected_report_date=_d("14/03/2026"),
        expected_date_label="REPORT_DATE",
        other_dates=[(_d("21/06/1985"), "DOB")],
        notes="Arabic-Indic digits in report and DOB.",
    )


def _ar_persian():
    return Fixture(
        id="m16-ar-persian",
        language="ar",
        format="image_pdf",
        quality="clean",
        layout="simple",
        digits="persian",
        lines=[
            FixtureLine("تقرير المختبر"),
            FixtureLine("تاريخ التقرير: 14/03/2026", "report_date"),
        ],
        expected_report_date=_d("14/03/2026"),
        expected_date_label="REPORT_DATE",
        notes="Persian digit forms.",
    )


def _ar_result_date():
    return Fixture(
        id="m16-ar-result",
        language="ar",
        format="image_pdf",
        quality="clean",
        layout="multiple_dates",
        digits="western",
        lines=[
            FixtureLine("تقرير فحص الدم"),
            FixtureLine("تاريخ الفحص: 13/03/2026", "exam"),
            FixtureLine("تاريخ النتيجة: 14/03/2026", "report_date"),
            FixtureLine("تاريخ الطباعة: 15/03/2026", "print"),
        ],
        expected_report_date=_d("14/03/2026"),
        expected_date_label="RESULT_DATE",
        other_dates=[(_d("13/03/2026"), "EXAMINATION"), (_d("15/03/2026"), "PRINT")],
    )


def _mixed_simple():
    return Fixture(
        id="m16-mixed-simple",
        language="mixed",
        format="image_pdf",
        quality="clean",
        layout="simple",
        digits="western",
        lines=[
            FixtureLine("MEDICAL REPORT / تقرير طبي"),
            FixtureLine("Report Date: 14/03/2026", "report_date"),
            FixtureLine("تاريخ التقرير: 14/03/2026", "report_date"),
            FixtureLine("Hemoglobin: 13.4 g/dL"),
        ],
        expected_report_date=_d("14/03/2026"),
        expected_date_label="REPORT_DATE",
    )


def _mixed_mixeddigits():
    return Fixture(
        id="m16-mixed-digits",
        language="mixed",
        format="image_pdf",
        quality="clean",
        layout="simple",
        digits="western",
        lines=[
            FixtureLine("Report Date: 14/03/2026", "report_date"),
            FixtureLine("تاريخ التقرير: 14/03/2026", "report_date"),
        ],
        expected_report_date=_d("14/03/2026"),
        expected_date_label="REPORT_DATE",
        notes="Same date in English and Arabic (western digits).",
    )


def _en_degraded(quality):
    return Fixture(
        id=f"m16-en-{quality}",
        language="en",
        format="image_png",
        quality=quality,
        layout="simple",
        digits="western",
        lines=[
            FixtureLine("MEDICAL LABORATORY REPORT"),
            FixtureLine("Report Date: 14/03/2026", "report_date"),
            FixtureLine("Hemoglobin: 13.4 g/dL"),
        ],
        expected_report_date=_d("14/03/2026"),
        expected_date_label="REPORT_DATE",
        notes=f"English clean-render with {quality} degradation.",
    )


def _ar_stamp():
    return Fixture(
        id="m16-ar-stamp",
        language="ar",
        format="image_pdf",
        quality="clean",
        layout="stamp",
        digits="western",
        lines=[
            FixtureLine("مختبر الأمل"),
            FixtureLine("تاريخ التقرير: 14/03/2026", "report_date"),
            FixtureLine("ختم المختبر"),
            FixtureLine("تاريخ الطباعة: 16/03/2026", "print"),
        ],
        expected_report_date=_d("14/03/2026"),
        expected_date_label="REPORT_DATE",
        other_dates=[(_d("16/03/2026"), "PRINT")],
    )


def _native_pdf():
    return Fixture(
        id="m16-native-pdf",
        language="en",
        format="native_pdf",
        quality="clean",
        layout="simple",
        digits="western",
        lines=[
            FixtureLine("MEDICAL LABORATORY REPORT"),
            FixtureLine("Report Date: 14/03/2026", "report_date"),
            FixtureLine("Hemoglobin: 13.4 g/dL"),
        ],
        expected_report_date=_d("14/03/2026"),
        expected_date_label="REPORT_DATE",
        notes="Native-text PDF control group (no OCR).",
    )


def _mixed_pdf():
    return Fixture(
        id="m16-mixed-pdf",
        language="en",
        format="mixed_pdf",
        quality="clean",
        layout="simple",
        digits="western",
        lines=[
            FixtureLine("MEDICAL LABORATORY REPORT"),
            FixtureLine("Report Date: 14/03/2026", "report_date"),
            FixtureLine("Hemoglobin: 13.4 g/dL"),
        ],
        expected_report_date=_d("14/03/2026"),
        expected_date_label="REPORT_DATE",
        notes="Page1 native text, page2 scanned image, page3 native text.",
    )


def _ar_ambiguous():
    return Fixture(
        id="m16-ar-ambiguous",
        language="ar",
        format="image_pdf",
        quality="clean",
        layout="multiple_dates",
        digits="western",
        lines=[
            FixtureLine("تقرير المختبر"),
            FixtureLine("التاريخ: 03/04/2026", "generic"),
            FixtureLine("تاريخ الميلاد: 21/06/1985", "dob"),
        ],
        expected_report_date=_d("03/04/2026"),
        expected_date_label="UNKNOWN",
        notes="Ambiguous 03/04/2026 under generic label; DOB must not win.",
    )


# ── Aggregation ──────────────────────────────────────────────────────


def build_fixtures() -> list[Fixture]:
    fixtures = [
        _en_simple(),
        _en_multidate(),
        _en_footer(),
        _en_lab_header(),
        _en_table(),
        _ar_simple(),
        _ar_separate_line(),
        _ar_separate_indic(),
        _ar_indic(),
        _ar_persian(),
        _ar_result_date(),
        _ar_stamp(),
        _mixed_simple(),
        _mixed_mixeddigits(),
        _native_pdf(),
        _mixed_pdf(),
        _ar_ambiguous(),
    ]
    # English variants across formats.
    for fmt, suffix in (
        ("image_png", "png"),
        ("image_jpeg", "jpg"),
        ("image_pdf", "pdf"),
    ):
        fixtures.append(
            Fixture(
                id=f"m16-en-simple-{suffix}",
                language="en",
                format=fmt,
                quality="clean",
                layout="simple",
                digits="western",
                lines=[
                    FixtureLine("MEDICAL LABORATORY REPORT"),
                    FixtureLine("Report Date: 14/03/2026", "report_date"),
                    FixtureLine("Hemoglobin: 13.4 g/dL"),
                ],
                expected_report_date=_d("14/03/2026"),
                expected_date_label="REPORT_DATE",
            )
        )
    # English degraded qualities.
    for quality in ("low_contrast", "blur", "rotation", "small_font", "noise"):
        fixtures.append(_en_degraded(quality))
    # Arabic degraded variants.
    for quality in ("low_contrast", "blur", "rotation", "small_font"):
        fixtures.append(
            Fixture(
                id=f"m16-ar-{quality}",
                language="ar",
                format="image_pdf",
                quality=quality,
                layout="simple",
                digits="western",
                lines=[
                    FixtureLine("تقرير المختبر الطبي"),
                    FixtureLine("تاريخ التقرير: 14/03/2026", "report_date"),
                ],
                expected_report_date=_d("14/03/2026"),
                expected_date_label="REPORT_DATE",
                notes=f"Arabic clean-render with {quality} degradation.",
            )
        )
    # More multi-date English variants.
    fixtures.extend(
        [
            Fixture(
                id="m16-en-admit-discharge",
                language="en",
                format="image_png",
                quality="clean",
                layout="multiple_dates",
                digits="western",
                lines=[
                    FixtureLine("ADMISSION SUMMARY"),
                    FixtureLine("Admission Date: 01/08/2026"),
                    FixtureLine("Discharge Date: 05/08/2026"),
                    FixtureLine("Report Date: 06/08/2026", "report_date"),
                ],
                expected_report_date=_d("06/08/2026"),
                expected_date_label="REPORT_DATE",
                other_dates=[
                    (_d("01/08/2026"), "ADMISSION"),
                    (_d("05/08/2026"), "DISCHARGE"),
                ],
            ),
            Fixture(
                id="m16-en-multicolumn",
                language="en",
                format="image_png",
                quality="clean",
                layout="multi_column",
                digits="western",
                lines=[
                    FixtureLine("Patient  Result      Date"),
                    FixtureLine("A001     Negative    14/03/2026", "report_date"),
                    FixtureLine("A002     Positive    15/03/2026"),
                ],
                expected_report_date=_d("14/03/2026"),
                expected_date_label="REPORT_DATE",
            ),
            Fixture(
                id="m16-en-namedmonth",
                language="en",
                format="image_png",
                quality="clean",
                layout="simple",
                digits="western",
                lines=[
                    FixtureLine("REPORT"),
                    FixtureLine("Report Date: 14 March 2026", "report_date"),
                ],
                expected_report_date=_d("14/03/2026"),
                expected_date_label="REPORT_DATE",
            ),
            Fixture(
                id="m16-en-ambiguous-bare",
                language="en",
                format="image_png",
                quality="clean",
                layout="simple",
                digits="western",
                lines=[
                    FixtureLine("03/04/2026", "bare"),
                    FixtureLine("Sample result text here"),
                ],
                expected_report_date=None,
                expected_date_label="UNKNOWN",
                notes="Bare ambiguous date with no label — M9 may withhold.",
            ),
            Fixture(
                id="m16-en-dob-vs-report",
                language="en",
                format="image_png",
                quality="clean",
                layout="multiple_dates",
                digits="western",
                lines=[
                    FixtureLine("Date of Birth: 21/06/1985", "dob"),
                    FixtureLine("Report Date: 14/03/2026", "report_date"),
                ],
                expected_report_date=_d("14/03/2026"),
                expected_date_label="REPORT_DATE",
                other_dates=[(_d("21/06/1985"), "DOB")],
            ),
        ]
    )
    return fixtures


# ── Renderers ────────────────────────────────────────────────────────


def _render_en_lines_image(
    lines: list[FixtureLine], *, font_size: int = 30
) -> Image.Image:
    width, height = 900, 60 + 46 * len(lines)
    img = Image.new("RGB", (width, height), "white")
    draw = ImageDraw.Draw(img)
    font = ImageFont.truetype(EN_FONT, font_size)
    y = 40
    for line in lines:
        draw.text((40, y), line.text, fill="black", font=font)
        y += 46
    return img


def _render_ar_lines_image(
    lines: list[FixtureLine], *, font_size: int = 34
) -> Image.Image:
    import pymupdf

    doc = pymupdf.open()
    page = doc.new_page(width=595, height=min(1600, 100 + 70 * len(lines)))
    body = []
    for line in lines:
        if any("\u0600" <= ch <= "\u06ff" for ch in line.text):
            body.append(f'<p dir="rtl" style="font-size:{font_size}pt">{line.text}</p>')
        else:
            body.append(f'<p dir="ltr" style="font-size:{font_size}pt">{line.text}</p>')
    page.insert_htmlbox(pymupdf.Rect(40, 40, 555, page.rect.height - 40), "".join(body))
    pix = page.get_pixmap(dpi=300, alpha=False)
    doc.close()
    return Image.frombytes("RGB", (pix.width, pix.height), pix.samples)


def apply_quality(image: Image.Image, quality: str) -> Image.Image:
    if quality in ("clean", "native"):
        return image
    if quality == "low_contrast":
        enhancer = ImageEnhance.Contrast(image)
        return enhancer.enhance(0.55)
    if quality == "blur":
        return image.filter(ImageFilter.GaussianBlur(radius=1.4))
    if quality == "small_font":
        return image.resize((image.width * 3 // 4, image.height * 3 // 4))
    if quality == "noise":
        import random

        pixels = image.load()
        for _ in range(4000):
            x = random.randint(0, image.width - 1)
            y = random.randint(0, image.height - 1)
            value = 40 if random.random() < 0.5 else 220
            pixels[x, y] = (value, value, value)
        return image
    if quality == "rotation":
        return image.rotate(4, expand=True, fillcolor="white")
    raise ValueError(f"unknown quality: {quality}")


def render_fixture_bytes(fixture: Fixture) -> dict:
    """Render fixture to bytes. Returns {'format': ..., 'bytes': ..., 'pages': n}."""
    text_lines = [line.text for line in fixture.lines]
    uses_arabic = any("\u0600" <= ch <= "\u06ff" for ch in "".join(text_lines))

    if fixture.format == "native_pdf":
        import pymupdf

        doc = pymupdf.open()
        page = doc.new_page(width=595, height=842)
        body = []
        for line in text_lines:
            if any("\u0600" <= ch <= "\u06ff" for ch in line):
                body.append(f'<p dir="rtl" style="font-size:24pt">{line}</p>')
            else:
                body.append(f'<p dir="ltr" style="font-size:24pt">{line}</p>')
        page.insert_htmlbox(pymupdf.Rect(40, 60, 555, 800), "".join(body))
        content = doc.tobytes()
        doc.close()
        return {
            "format": "native_pdf",
            "bytes": content,
            "pages": 1,
            "text": "\n".join(text_lines),
        }

    if fixture.format == "mixed_pdf":
        import pymupdf

        doc = pymupdf.open()
        # page 1: native text
        page1 = doc.new_page(width=595, height=842)
        page1.insert_htmlbox(
            pymupdf.Rect(40, 60, 555, 400),
            '<p dir="ltr" style="font-size:22pt">MEDICAL LABORATORY REPORT</p>'
            '<p dir="ltr" style="font-size:22pt">Patient: A. Sample</p>',
        )
        # page 2: scanned image (the dated line)
        img = _render_en_lines_image(
            [
                FixtureLine("Report Date: 14/03/2026", "report_date"),
                FixtureLine("Hemoglobin: 13.4 g/dL"),
            ]
        )
        img = apply_quality(img, "clean")
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        page2 = doc.new_page(width=595, height=842)
        page2.insert_image(pymupdf.Rect(40, 60, 555, 300), stream=buf.getvalue())
        # page 3: native text
        page3 = doc.new_page(width=595, height=842)
        page3.insert_htmlbox(
            pymupdf.Rect(40, 60, 555, 300),
            '<p dir="ltr" style="font-size:22pt">End of report</p>',
        )
        content = doc.tobytes()
        doc.close()
        return {
            "format": "mixed_pdf",
            "bytes": content,
            "pages": 3,
            "native_pages": [1, 3],
            "ocr_pages": [2],
        }

    # Image-based fixtures.
    if uses_arabic:
        base = _render_ar_lines_image(fixture.lines)
    else:
        base = _render_en_lines_image(fixture.lines)
    base = apply_quality(base, fixture.quality)

    if fixture.format == "image_pdf":
        import pymupdf

        doc = pymupdf.open()
        page = doc.new_page(width=595, height=842)
        buf = io.BytesIO()
        base.save(buf, format="PNG")
        # Embed as large as legible so the 300 DPI rasterization keeps text.
        target_w = 515
        target_h = round(base.height * target_w / base.width)
        page.insert_image(
            pymupdf.Rect(40, 60, 40 + target_w, 60 + target_h),
            stream=buf.getvalue(),
        )
        content = doc.tobytes()
        doc.close()
        return {
            "format": "image_pdf",
            "bytes": content,
            "pages": 1,
            "text": "\n".join(text_lines),
        }

    if fixture.format == "image_jpeg":
        buf = io.BytesIO()
        base.save(buf, format="JPEG", quality=75)
        return {
            "format": "image_jpeg",
            "bytes": buf.getvalue(),
            "pages": 1,
            "text": "\n".join(text_lines),
        }

    buf = io.BytesIO()
    base.save(buf, format="PNG")
    return {
        "format": "image_png",
        "bytes": buf.getvalue(),
        "pages": 1,
        "text": "\n".join(text_lines),
    }
