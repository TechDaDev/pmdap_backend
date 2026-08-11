"""Deterministic ICAO MRZ parser tests — synthetic strings only."""
import pytest

from identities import mrz


def _td3_line1(
    doc_number="AB123456",
    issuing="UTO",
    nationality="UTO",
    dob="900101",
    sex="M",
    expiry="301231",
):
    def cd(s):
        return str(mrz.check_digit(s))

    doc = doc_number.ljust(9, "<")
    return (
        "P<"
        + issuing
        + doc
        + cd(doc)
        + nationality
        + "<"
        + dob
        + cd(dob)
        + sex
        + "<<"
        + expiry
        + cd(expiry)
        + "00000000"
    )


def _td3_line2(surname="DOE", given="JOHN"):
    return (surname + "<<" + given).ljust(44, "<")


def test_valid_td3_passport():
    l1 = _td3_line1()
    l2 = _td3_line2()
    result = mrz.parse_mrz([l1, l2])
    assert result.detected is True
    assert result.valid is True
    assert result.checks_passed is True
    assert result.document_type == "P"
    assert result.issuing_country == "UTO"
    assert result.document_number == "AB123456"
    assert result.nationality == "UTO"
    assert result.date_of_birth.year == 1990
    assert result.sex == "M"
    assert result.expiry_date.year == 2030
    assert "DOE" in (result.name or "")


def test_invalid_document_number_check_digit():
    l1 = list(_td3_line1())
    # Corrupt the document number field in place (9 chars) so its check digit fails.
    l1[5:14] = list("AB123457<")
    l1 = "".join(l1)
    result = mrz.parse_mrz([l1, _td3_line2()])
    assert result.valid is False
    assert "MRZ_CHECK_FAILED" in result.warnings
    assert "document_number" in result.low_confidence_fields


def test_ocr_substitution_not_silently_normalized():
    # 'O' instead of '0' — parser must NOT silently rewrite the identifier.
    l1 = _td3_line1(doc_number="AB12345O")
    result = mrz.parse_mrz([l1, _td3_line2()])
    # Check digit happens to pass; the key guarantee is the value is kept as-is.
    assert result.document_number == "AB12345O"


def test_invalid_line_length():
    result = mrz.parse_mrz(["SHORT", "LINES"])
    assert result.detected is False
    assert "MRZ_NOT_DETECTED" in result.warnings


def test_td1_id_card():
    def cd(s):
        return str(mrz.check_digit(s))

    doc = "X1234567".ljust(9, "<")
    l1 = ("I<UTO" + doc + cd(doc) + "UTO").ljust(30, "<")
    l2 = ("SURNAME<<GIVEN".ljust(21, "<") + "301231" + cd("301231") + "UT").ljust(
        30, "<"
    )
    l3 = ("900101" + cd("900101") + "M").ljust(30, "<")
    result = mrz.parse_mrz([l1, l2, l3])
    assert result.detected is True
    assert result.valid is True
    assert result.document_number == "X1234567"
    assert result.expiry_date.year == 2030
    assert result.date_of_birth.year == 1990
    assert result.sex == "M"
