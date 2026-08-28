"""Iraqi National Card structured extraction tests — SYNTHETIC data only.

Every value here is invented (TESTNAME / TESTFATHER / 123456789012 /
H12345678 / TESTFAMILY123456 / 1990-05-17 ...). Real owner card values are
never used. Covers the field matrix plus the critical regression guards:
family number must never come from MRZ noise, and the H... body number must
never populate family_number.
"""

import pytest

from identities import extraction, mrz
from identities.regions import REGIONS, IraqiNationalCardRegionExtractor

# --------------------------------------------------------------------------- #
# Synthetic helpers
# --------------------------------------------------------------------------- #


def _cd(value):
    return str(mrz.check_digit(value))


def _iraqi_mrz_lines(dob="900517", sex="M", expiry="360101", doc="H12345678"):
    line1 = ("ID" + "IRQ" + doc + _cd(doc) + "900101202601").ljust(30, "<")
    line2 = (dob + _cd(dob) + sex + expiry + _cd(expiry) + "IRQ").ljust(30, "<")
    line3 = "TESTGRANDFATHER<<TESTNAME".ljust(30, "<")
    return [line1, line2, line3]


def _front(
    name="الاسم اناو TESTNAME",
    father="اباوك TESTFATHER",
    grandfather="ابابيرTESTGRAND",
    sex="الجنس اركمز ذكر",
    card="123456789012",
    body="H12345678",
):
    return [
        extraction.SideLine("FRONT", name, 0.9),
        extraction.SideLine("FRONT", father, 0.9),
        extraction.SideLine("FRONT", "الاب", 0.9),
        extraction.SideLine("FRONT", grandfather, 0.9),
        extraction.SideLine("FRONT", "الجد", 0.9),
        extraction.SideLine("FRONT", "اللقب انازناو TESTTITLE", 0.9),
        extraction.SideLine("FRONT", "ادايك TESTMOTHER", 0.9),
        extraction.SideLine("FRONT", "الأم", 0.9),
        extraction.SideLine("FRONT", sex, 0.9),
        extraction.SideLine("FRONT", card, 0.9),
        extraction.SideLine("FRONT", body, 0.9),
    ]


def _card_lines(
    *,
    blood=("ROI_BLOOD", "O+"),
    dob=("ROI_DOB", "1990/05/17"),
    family=("ROI_FAMILY", "TESTFAMILY123456"),
    mrz=True,
    lines=None,
):
    lines = list(lines) if lines is not None else _front()
    lines.append(extraction.SideLine("BACK", "تاريخ الاصدار: 2024/02/03", 0.9))
    lines.append(extraction.SideLine("BACK", "تاريخ النفاذ: 2036/01/01", 0.9))
    lines.append(extraction.SideLine("BACK", "تأريخ الولادة ارؤزى لهدايك بوون", 0.8))
    lines.append(extraction.SideLine("BACK", "الرقملعانليمارى خاني", 0.7))
    if blood:
        lines.append(extraction.SideLine(blood[0], blood[1], 0.9))
    if dob:
        lines.append(extraction.SideLine(dob[0], dob[1], 0.9))
    if family:
        lines.append(extraction.SideLine(family[0], family[1], 0.9))
    if mrz:
        for line in _iraqi_mrz_lines():
            lines.append(extraction.SideLine("ROI_MRZ", line, 0.9))
    return lines


def _run(lines):
    return extraction.extract_identity("UNIFIED_NATIONAL_CARD", lines)


# --------------------------------------------------------------------------- #
# Name components
# --------------------------------------------------------------------------- #


def test_name_father_grandfather_extracted_distinct():
    fields, warnings, _ = _run(_card_lines(mrz=False))
    assert fields["name"]["value"] == "TESTNAME"
    assert fields["name"]["source"] == "FRONT_PRINTED"
    assert fields["father_name"]["value"] == "TESTFATHER"
    assert fields["grandfather_name"]["value"] == "TESTGRAND"


def test_name_label_variants():
    fields, _, _ = _run(_card_lines(mrz=False))
    # Alternate spacing/connector rendering must still resolve.
    alt = _card_lines(mrz=False)
    alt[0] = extraction.SideLine("FRONT", "الاسم: اناو TESTNAME", 0.8)
    fields2, _, _ = _run(alt)
    assert fields["name"]["value"] == "TESTNAME"
    assert fields2["name"]["value"] == "TESTNAME"


def test_paternal_not_maternal_grandfather():
    # Mother + maternal grandfather appear AFTER the father chain; the
    # extractor must pick the paternal (pre-mother) grandfather only.
    lines = [
        extraction.SideLine("FRONT", "الاسم اناو TESTNAME", 0.9),
        extraction.SideLine("FRONT", "اباوك TESTFATHER", 0.9),
        extraction.SideLine("FRONT", "ابابيرPATERNALGRAND", 0.9),
        extraction.SideLine("FRONT", "ادايك TESTMOTHER", 0.9),
        extraction.SideLine("FRONT", "ابيرMATERNALGRAND", 0.9),
        extraction.SideLine("FRONT", "الجنس اركمز ذكر", 0.9),
        extraction.SideLine("FRONT", "123456789012", 0.9),
        extraction.SideLine("FRONT", "H12345678", 0.9),
    ]
    fields, _, _ = _run(lines)
    assert fields["grandfather_name"]["value"] == "PATERNALGRAND"
    assert fields["grandfather_name"]["value"] != "MATERNALGRAND"
    assert "MATERNALGRAND" not in [f["value"] for f in fields.values()]


def test_mother_name_split_line_label_then_value():
    lines = [
        extraction.SideLine("FRONT", "الاسم اناو TESTNAME", 0.9),
        extraction.SideLine("FRONT", "اباوك TESTFATHER", 0.9),
        extraction.SideLine("FRONT", "ابابيرTESTGRAND", 0.9),
        extraction.SideLine("FRONT", "الام", 0.9),
        extraction.SideLine("FRONT", "TESTMOTHER", 0.9),
        extraction.SideLine("FRONT", "الجنس اركمز ذكر", 0.9),
    ]

    fields, _, _ = _run(lines)

    assert fields["mother_name"]["value"] == "TESTMOTHER"


def test_mother_name_observed_kurdish_label_variant():
    fields, _, _ = _run(
        [
            extraction.SideLine("FRONT", "الاسم اناو TESTNAME", 0.9),
            extraction.SideLine("FRONT", "اباوك TESTFATHER", 0.9),
            extraction.SideLine("FRONT", "ابابيرTESTGRAND", 0.9),
            extraction.SideLine("FRONT", "دايك TESTMOTHER", 0.9),
        ]
    )

    assert fields["mother_name"]["value"] == "TESTMOTHER"


@pytest.mark.parametrize(
    "next_line",
    (
        "الجنسية عراقية",
        "فصيلة الدم O+",
        "123456789012",
        "H12345678",
    ),
)
def test_mother_split_line_rejects_non_name_administrative_values(next_line):
    lines = [
        extraction.SideLine("FRONT", "الاسم اناو TESTNAME", 0.9),
        extraction.SideLine("FRONT", "اباوك TESTFATHER", 0.9),
        extraction.SideLine("FRONT", "ابابيرTESTGRAND", 0.9),
        extraction.SideLine("FRONT", "الام", 0.9),
        extraction.SideLine("FRONT", next_line, 0.9),
        extraction.SideLine("FRONT", "الجنس اركمز ذكر", 0.9),
    ]

    fields, _, _ = _run(lines)

    assert "mother_name" not in fields


def test_father_label_alone_ignored():
    # A bare "الاب" label line must not swallow a later name value.
    lines = [
        extraction.SideLine("FRONT", "الاسم اناو TESTNAME", 0.9),
        extraction.SideLine("FRONT", "الاب", 0.9),
        extraction.SideLine("FRONT", "ابابيرTESTGRAND", 0.9),
        extraction.SideLine("FRONT", "الجنس اركمز ذكر", 0.9),
        extraction.SideLine("FRONT", "123456789012", 0.9),
        extraction.SideLine("FRONT", "H12345678", 0.9),
    ]
    fields, _, _ = _run(lines)
    assert fields["name"]["value"] == "TESTNAME"
    assert fields["grandfather_name"]["value"] == "TESTGRAND"


# --------------------------------------------------------------------------- #
# Sex
# --------------------------------------------------------------------------- #


def test_sex_male_from_front_and_mrz_agree():
    fields, warnings, _ = _run(_card_lines())
    assert fields["sex"]["value"] == "MALE"
    assert fields["sex"]["confidence"] >= 0.9
    assert fields["sex"]["cross_check"] == "MRZ_AGREE"


def test_sex_female_from_front():
    lines = _card_lines(mrz=False)
    lines[8] = extraction.SideLine("FRONT", "الجنس اركمز انثى", 0.9)
    fields, _, _ = _run(lines)
    assert fields["sex"]["value"] == "FEMALE"


def test_sex_conflict_lowers_confidence_and_warns():
    lines = _card_lines()
    lines[8] = extraction.SideLine("FRONT", "الجنس اركمز انثى", 0.9)  # front F, MRZ M
    fields, warnings, _ = _run(lines)
    assert fields["sex"]["value"] == "FEMALE"
    assert fields["sex"]["confidence"] < 0.6
    assert fields["sex"]["cross_check"] == "MRZ_MISMATCH"
    assert "SOURCE_MISMATCH" in warnings


def test_sex_from_mrz_only():
    lines = _card_lines(mrz=True)
    # drop the front sex line
    lines = [ln for ln in lines if "الجنس" not in ln.text]
    fields, _, _ = _run(lines)
    assert fields["sex"]["value"] == "MALE"
    assert fields["sex"]["source"] == "MRZ"


# --------------------------------------------------------------------------- #
# Blood group
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("O+", "O+"),
        ("O-", "O-"),
        ("A+", "A+"),
        ("A-", "A-"),
        ("B+", "B+"),
        ("B-", "B-"),
        ("AB+", "AB+"),
        ("AB-", "AB-"),
    ],
)
def test_blood_group_all_groups(raw, expected):
    fields, _, _ = _run(_card_lines(blood=("ROI_BLOOD", raw), mrz=False))
    assert fields["blood_group"]["value"] == expected


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("0+", "O+"),
        ("O +", "O+"),
        ("AB +", "AB+"),
        ("0-", "O-"),
    ],
)
def test_blood_group_ocr_variants(raw, expected):
    fields, _, _ = _run(_card_lines(blood=("ROI_BLOOD", raw), mrz=False))
    assert fields["blood_group"]["value"] == expected


def test_blood_group_invalid_not_normalized():
    fields, warnings, _ = _run(_card_lines(blood=("ROI_BLOOD", "1O+"), mrz=False))
    assert "blood_group" not in fields
    assert "BLOOD_GROUP_NOT_FOUND" in warnings


def test_blood_group_missing_warns():
    fields, warnings, _ = _run(_card_lines(blood=None, mrz=False))
    assert "blood_group" not in fields
    assert "BLOOD_GROUP_NOT_FOUND" in warnings


# --------------------------------------------------------------------------- #
# National / card number + O/0 confusion
# --------------------------------------------------------------------------- #


def test_national_card_number_extracted():
    fields, _, _ = _run(_card_lines(mrz=False))
    assert fields["national_card_number"]["value"] == "123456789012"
    assert fields["document_number"]["value"] == "H12345678"
    assert fields["card_body_number"]["value"] == "H12345678"


def test_card_number_o_zero_normalized():
    lines = _card_lines(mrz=False)
    lines[9] = extraction.SideLine("FRONT", "12345678901O", 0.9)
    fields, warnings, _ = _run(lines)
    assert fields["national_card_number"]["value"] == "123456789010"
    assert "OCR_CHARACTER_NORMALIZED" in warnings
    # Confidence must drop after a normalization correction.
    normal_fields, _, _ = _run(_card_lines(mrz=False))
    assert (
        fields["national_card_number"]["confidence"]
        < normal_fields["national_card_number"]["confidence"]
    )


def test_body_number_case_canonicalized_not_normalized():
    # Body number format is H + digits; it is uppercased, never run through
    # the O/0 confusable correction used for the numeric card number.
    lines = _card_lines(mrz=False)
    lines[10] = extraction.SideLine("FRONT", "h12345678", 0.9)
    fields, warnings, _ = _run(lines)
    assert fields["unique_card_body_number"]["value"] == "H12345678"
    assert "OCR_CHARACTER_NORMALIZED" not in warnings


# --------------------------------------------------------------------------- #
# Unique card body number
# --------------------------------------------------------------------------- #


def test_unique_body_number_set_not_family():
    fields, _, _ = _run(_card_lines())
    assert fields["unique_card_body_number"]["value"] == "H12345678"
    assert fields["unique_card_body_number"]["cross_check"] == "MRZ_AGREE"
    assert fields["family_number"]["value"] == "TESTFAMILY123456"
    assert fields["family_number"]["value"] != "H12345678"


def test_unique_body_number_without_mrz():
    fields, _, _ = _run(_card_lines(mrz=False))
    assert fields["unique_card_body_number"]["value"] == "H12345678"
    assert "cross_check" not in fields["unique_card_body_number"]


# --------------------------------------------------------------------------- #
# Family number
# --------------------------------------------------------------------------- #


def test_family_number_from_back():
    fields, _, _ = _run(_card_lines())
    assert fields["family_number"]["value"] == "TESTFAMILY123456"
    assert fields["family_number"]["source"] == "BACK_PRINTED"


def test_family_number_missing_with_mrz_noise_only():
    # MRZ noise alone must NEVER become a family number.
    lines = _front()
    lines.append(extraction.SideLine("BACK", "IDIRQH1234567890123456789<<<", 0.9))
    lines.append(extraction.SideLine("BACK", "9005170M3601012IRQ<<<<555<<<<5", 0.9))
    fields, warnings, _ = _run(lines)
    assert "family_number" not in fields
    assert "FAMILY_NUMBER_NOT_FOUND" in warnings


@pytest.mark.parametrize("noise", ["555", "1234"])
def test_family_number_never_short_digit_group(noise):
    lines = _front()
    lines.append(extraction.SideLine("BACK", f"some label {noise}", 0.9))
    fields, _, _ = _run(lines)
    assert "family_number" not in fields


def test_family_number_never_equals_body_number():
    # If the only long alphanumeric candidate is the body number, it must NOT
    # be reported as the family number.
    lines = _card_lines(family=("ROI_FAMILY", "H12345678"), mrz=False)
    fields, _, _ = _run(lines)
    assert "family_number" not in fields
    assert fields["unique_card_body_number"]["value"] == "H12345678"


# --------------------------------------------------------------------------- #
# Date of birth
# --------------------------------------------------------------------------- #


def test_dob_printed_and_mrz_agree():
    fields, _, _ = _run(_card_lines())
    assert fields["date_of_birth"]["value"] == "1990-05-17"
    assert fields["date_of_birth"]["confidence"] >= 0.9
    assert fields["date_of_birth"]["cross_check"] == "MRZ_AGREE"


def test_dob_printed_only():
    fields, _, _ = _run(_card_lines(mrz=False))
    assert fields["date_of_birth"]["value"] == "1990-05-17"
    assert fields["date_of_birth"]["confidence"] < 0.9
    assert fields["date_of_birth"]["source"] == "BACK_PRINTED"


def test_dob_mrz_only():
    lines = _card_lines(dob=None)
    fields, _, _ = _run(lines)
    assert fields["date_of_birth"]["value"] == "1990-05-17"
    assert fields["date_of_birth"]["source"] == "MRZ"


def test_dob_conflict_lowers_confidence_and_warns():
    lines = _card_lines(dob=("ROI_DOB", "1991/05/17"))
    fields, warnings, _ = _run(lines)
    assert fields["date_of_birth"]["value"] == "1991-05-17"
    assert fields["date_of_birth"]["cross_check"] == "MRZ_MISMATCH"
    assert fields["date_of_birth"]["confidence"] < 0.6
    assert "SOURCE_MISMATCH" in warnings


def test_dob_future_rejected():
    # A future printed DOB must not be silently accepted.
    lines = _card_lines(dob=("ROI_DOB", "2099/05/17"), mrz=False)
    fields, _, _ = _run(lines)
    assert "date_of_birth" not in fields


def test_dob_invalid_month_ignored():
    lines = _card_lines(dob=("ROI_DOB", "1990/13/17"), mrz=False)
    fields, _, _ = _run(lines)
    assert "date_of_birth" not in fields


def test_expiry_prefers_mrz_on_printed_truncation():
    # A truncated/ambiguous printed expiry must not win over the validated MRZ.
    lines = _card_lines()
    lines = [ln for ln in lines if "تاريخ النفاذ" not in ln.text]
    lines.append(extraction.SideLine("ROI_DATES", "تاريخ النفاذ2036/07/01", 0.9))
    fields, warnings, _ = _run(lines)
    assert fields["expiry_date"]["value"] == "2036-01-01"  # MRZ authoritative
    assert fields["expiry_date"]["cross_check"] == "MRZ_MISMATCH"
    assert "SOURCE_MISMATCH" in warnings


def test_issue_and_expiry_require_explicit_labels_and_support_glued_digits():
    lines = _front() + [
        extraction.SideLine("BACK", "تأريخ الاصدار٢٠٢٤/٠٢/٠٣", 0.9),
        extraction.SideLine("BACK", "تاريخ النفاذ۲۰۳۴/۰۲/۰۲", 0.9),
    ]

    fields, _, _ = _run(lines)

    assert fields["issue_date"]["value"] == "2024-02-03"
    assert fields["expiry_date"]["value"] == "2034-02-02"


def test_date_labels_and_latin_roi_values_pair_by_printed_row_order():
    lines = _front() + [
        extraction.SideLine("BACK", "تأريخ الاصدار روژى دەرچوون", 0.9),
        extraction.SideLine("BACK", "تأريخ النفاذ ڕۆژی بەسەرچوون", 0.9),
        extraction.SideLine("ROI_DATES", "2024/02/03", 0.9),
        extraction.SideLine("ROI_DATES", "2034/02/02", 0.9),
    ]

    fields, _, _ = _run(lines)

    assert fields["issue_date"]["value"] == "2024-02-03"
    assert fields["expiry_date"]["value"] == "2034-02-02"


def test_unlabeled_dates_never_become_issue_expiry_or_dob():
    lines = _front() + [
        extraction.SideLine("ROI_DATES", "2024/02/03", 0.9),
        extraction.SideLine("ROI_DATES", "2034/02/02", 0.9),
    ]

    fields, _, _ = _run(lines)

    assert "issue_date" not in fields
    assert "expiry_date" not in fields
    assert "date_of_birth" not in fields


# --------------------------------------------------------------------------- #
# MRZ parser
# --------------------------------------------------------------------------- #


def test_iraqi_mrz_parses_all_fields():
    lines = _iraqi_mrz_lines()
    result = mrz.parse_iraqi_national_card_mrz(lines)
    assert result.detected is True
    assert result.valid is True
    assert result.checks_passed is True
    assert result.document_number == "H12345678"
    assert result.date_of_birth.isoformat() == "1990-05-17"
    assert result.sex == "M"
    assert result.expiry_date.isoformat() == "2036-01-01"
    assert result.nationality == "IRQ"
    assert "TESTNAME" in (result.name or "")


def test_iraqi_mrz_partial_without_line3():
    lines = _iraqi_mrz_lines()[:2]
    result = mrz.parse_iraqi_national_card_mrz(lines)
    assert result.detected is True
    assert result.date_of_birth is not None
    assert result.sex == "M"
    assert "MRZ_PARTIAL" in result.warnings


def test_iraqi_mrz_bad_check_lowers_doc_confidence():
    # Store a check digit that does NOT match the document number.
    doc = "H12345678"
    wrong_check = "0"
    assert _cd(doc) != wrong_check
    line1 = ("ID" + "IRQ" + doc + wrong_check + "900101202601").ljust(30, "<")
    lines = _iraqi_mrz_lines()
    lines[0] = line1
    result = mrz.parse_iraqi_national_card_mrz(lines)
    assert "document_number" in result.low_confidence_fields
    assert result.valid is False


def test_iraqi_mrz_not_detected():
    result = mrz.parse_iraqi_national_card_mrz(["garbage", "not an mrz"])
    assert result.detected is False
    assert "MRZ_NOT_DETECTED" in result.warnings


def test_iraqi_mrz_line3_mangled_arabic_digits_sanitized():
    # Arabic-Indic digits and noise in line 3 must be tolerated.
    lines = _iraqi_mrz_lines()
    lines[2] = "MFRJSSASMAEYLS٢>٢SSSS٢<<<<<"
    result = mrz.parse_iraqi_national_card_mrz(lines)
    assert result.detected is True
    assert result.date_of_birth.isoformat() == "1990-05-17"
    assert result.sex == "M"


# --------------------------------------------------------------------------- #
# Missing-field / noise robustness
# --------------------------------------------------------------------------- #


def test_empty_input_missing_everything():
    fields, warnings, mrz_summary = extraction.extract_identity(
        "UNIFIED_NATIONAL_CARD", []
    )
    assert fields == {}
    assert mrz_summary["detected"] is False
    assert warnings


def test_legacy_plain_strings_treated_as_front():
    fields, warnings, mrz_summary = extraction.extract_identity(
        "UNIFIED_NATIONAL_CARD",
        [
            "العراق",
            "الاسم اناو SYNTHNAME",
            "الجنس اركمز ذكر",
            "123456789012",
            "H12345678",
        ],
    )
    assert fields["name"]["value"] == "SYNTHNAME"
    assert fields["sex"]["value"] == "MALE"
    assert fields["national_card_number"]["value"] == "123456789012"
    assert fields["unique_card_body_number"]["value"] == "H12345678"
    assert "family_number" not in fields


# --------------------------------------------------------------------------- #
# Region extractor
# --------------------------------------------------------------------------- #


class _FakeEngine:
    def __init__(self, lines=None):
        self._lines = lines or [("ROITEXT", 0.9)]

    def extract_image(self, image):
        return type(
            "R",
            (),
            {
                "lines": tuple(
                    type("L", (), {"text": t, "confidence": c})()
                    for t, c in self._lines
                )
            },
        )()


def test_region_extractor_produces_tagged_lines():
    from PIL import Image

    arabic = _FakeEngine([("عربي", 0.8)])
    latin = _FakeEngine([("LATIN", 0.9)])
    extractor = IraqiNationalCardRegionExtractor(arabic, latin)
    img = Image.new("RGB", (100, 60), "white")
    lines = extractor.run(img, img)
    sides = {line.side for line in lines}
    assert sides == set(REGIONS.keys())
    assert any(line.side == "ROI_MRZ" for line in lines)


def test_region_definitions_are_normalized_fractions():
    for _tag, spec in REGIONS.items():
        assert 0.0 <= spec["x"] < 1.0
        assert 0.0 <= spec["y"] < 1.0
        assert spec["w"] > 0 and spec["h"] > 0
        assert spec["side"] in ("FRONT", "BACK")
        assert spec["engine"] in ("arabic", "latin")
        assert spec["scale"] >= 1


# --------------------------------------------------------------------------- #
# Multi-sample label robustness (second real-card findings, SYNTHETIC values)
# --------------------------------------------------------------------------- #


def _front_only(*lines):
    return [extraction.SideLine("FRONT", text, conf) for text, conf in lines] + [
        extraction.SideLine("FRONT", "الجنس اركمز ذكر", 0.9),
        extraction.SideLine("FRONT", "123456789012", 0.9),
        extraction.SideLine("FRONT", "G12345678", 0.9),
    ]


# --- Name: same-line multilingual / glued connector / split-line ---


@pytest.mark.parametrize(
    "name_line",
    [
        "الاسم / ناو : TESTNAME",
        "الاسم ناو TESTNAME",
        "ناو: TESTNAME",
        "الاسم ناوTESTNAME",  # connector glued to the value
    ],
)
def test_name_label_variants_multilingual(name_line):
    fields, _, _ = _run(_front_only((name_line, 0.9), ("باوك TESTFATHER", 0.9)))
    assert fields["name"]["value"] == "TESTNAME"


def test_name_split_line_label_then_value():
    fields, _, _ = _run(
        _front_only(
            ("الاسم", 0.9),
            ("TESTNAME", 0.9),
            ("باوك TESTFATHER", 0.9),
        )
    )
    assert fields["name"]["value"] == "TESTNAME"


def test_name_label_stuck_to_value():
    # OCR merges label and value with no separator at all.
    fields, _, _ = _run(_front_only(("الاسمTESTNAME", 0.9), ("باوك TESTFATHER", 0.9)))
    assert fields["name"]["value"] == "TESTNAME"


# --- Father: Kurdish label, glued, split-line ---


@pytest.mark.parametrize(
    "father_line",
    [
        "الاب / باوك : TESTFATHER",
        "باوك: TESTFATHER",
        "الاب باوك TESTFATHER",
        "باوكTESTFATHER",  # Kurdish label glued to the value
    ],
)
def test_father_label_variants(father_line):
    fields, _, _ = _run(_front_only(("الاسم اناو TESTNAME", 0.9), (father_line, 0.9)))
    assert fields["father_name"]["value"] == "TESTFATHER"


def test_father_split_line_label_then_value():
    fields, _, _ = _run(
        _front_only(
            ("الاسم اناو TESTNAME", 0.9),
            ("باوك", 0.9),
            ("TESTFATHER", 0.9),
        )
    )
    assert fields["father_name"]["value"] == "TESTFATHER"


def test_father_split_line_must_not_grab_grandfather():
    # Label line then a grandfather-labeled line: the grandfather must win.
    fields, _, _ = _run(
        _front_only(
            ("الاسم اناو TESTNAME", 0.9),
            ("الاب", 0.9),
            ("ابابيرTESTGRAND", 0.9),
        )
    )
    assert fields["grandfather_name"]["value"] == "TESTGRAND"
    assert "father_name" not in fields or fields["father_name"]["value"] != "TESTGRAND"


def test_maternal_grandfather_never_populates_grandfather():
    # Kurdish father chain, mother, then a maternal grandfather row.
    fields, _, _ = _run(
        _front_only(
            ("الاسم ناوTESTNAME", 0.9),
            ("باوكTESTFATHER", 0.9),
            ("ابابيرPATERNALGRAND", 0.9),
            ("ردايك TESTMOTHER", 0.9),
            ("بابير MATERNALGRAND", 0.9),
        )
    )
    assert fields["grandfather_name"]["value"] == "PATERNALGRAND"
    assert fields["grandfather_name"]["value"] != "MATERNALGRAND"
    assert "MATERNALGRAND" not in [f["value"] for f in fields.values()]


# --- Unique card body number: prefix-agnostic + FRONT/MRZ cross-check ---


@pytest.mark.parametrize("prefix", ["A", "G", "H", "Z"])
def test_body_number_any_letter_prefix(prefix):
    body = f"{prefix}12345678"
    lines = _front()
    lines[10] = extraction.SideLine("FRONT", body, 0.9)
    fields, _, _ = _run(_card_lines(lines=lines, mrz=False))
    assert fields["unique_card_body_number"]["value"] == body


def test_body_number_front_only():
    lines = _front()
    lines[10] = extraction.SideLine("FRONT", "G41421961", 0.9)
    fields, _, _ = _run(_card_lines(lines=lines, mrz=False))
    assert fields["unique_card_body_number"]["value"] == "G41421961"
    assert "cross_check" not in fields["unique_card_body_number"]


def test_body_number_mrz_only():
    # Front OCR misses the body number; the MRZ line 1 carries it.
    lines = _front()
    lines[10] = extraction.SideLine("FRONT", "noise here", 0.9)
    for ln in _iraqi_mrz_lines(doc="G41421961"):
        lines.append(extraction.SideLine("ROI_MRZ", ln, 0.9))
    fields, _, _ = _run(lines)
    assert fields["unique_card_body_number"]["value"] == "G41421961"
    assert fields["unique_card_body_number"]["source"] == "MRZ"


def test_body_number_front_mrz_agree():
    lines = _front()
    lines[10] = extraction.SideLine("FRONT", "G41421961", 0.9)
    for ln in _iraqi_mrz_lines(doc="G41421961"):
        lines.append(extraction.SideLine("ROI_MRZ", ln, 0.9))
    fields, _, _ = _run(lines)
    assert fields["unique_card_body_number"]["value"] == "G41421961"
    assert fields["unique_card_body_number"]["cross_check"] == "MRZ_AGREE"
    assert fields["unique_card_body_number"]["confidence"] >= 0.9


def test_body_number_front_mrz_mismatch_warns():
    lines = _front()
    lines[10] = extraction.SideLine("FRONT", "H12345678", 0.9)
    for ln in _iraqi_mrz_lines(doc="G41421961"):
        lines.append(extraction.SideLine("ROI_MRZ", ln, 0.9))
    fields, warnings, _ = _run(lines)
    assert fields["unique_card_body_number"]["value"] == "H12345678"
    assert fields["unique_card_body_number"]["cross_check"] == "MRZ_MISMATCH"
    assert "SOURCE_MISMATCH" in warnings


@pytest.mark.parametrize(
    "bad",
    [
        "123456789",
        "GH1234567",
        "G1234",
        "G1234567890",
        "BODY123456",
    ],
)
def test_invalid_body_numbers_rejected(bad):
    lines = _front()
    lines[10] = extraction.SideLine("FRONT", bad, 0.9)
    fields, _, _ = _run(_card_lines(lines=lines, mrz=False, family=None))
    assert "unique_card_body_number" not in fields
    assert "family_number" not in fields


def test_body_number_distinct_from_card_and_family():
    lines = _front()
    lines[9] = extraction.SideLine("FRONT", "198060266608", 0.9)
    lines[10] = extraction.SideLine("FRONT", "G41421961", 0.9)
    lines.append(extraction.SideLine("ROI_FAMILY", "TESTFAMILY123456", 0.9))
    fields, _, _ = _run(lines)
    assert fields["national_card_number"]["value"] == "198060266608"
    assert fields["unique_card_body_number"]["value"] == "G41421961"
    assert fields["family_number"]["value"] == "TESTFAMILY123456"
    assert fields["family_number"]["value"] != "G41421961"
    assert fields["national_card_number"]["value"] != "G41421961"
