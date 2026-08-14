"""Adaptive ROI selection tests — synthetic MRZ probes only, no OCR runs."""
import pytest

from identities.tasks import _adaptive_roi_plan


class _Probe:
    def __init__(
        self,
        *,
        detected=True,
        checks_passed=True,
        date_of_birth=None,
        low_confidence_fields=(),
    ):
        self.detected = detected
        self.checks_passed = checks_passed
        self.date_of_birth = date_of_birth
        self.low_confidence_fields = set(low_confidence_fields)


@pytest.mark.parametrize(
    "probe,expect_skip",
    [
        # Clean full-OCR MRZ (detected + checks passed) + validated DOB:
        # both ROI_MRZ and ROI_DOB are redundant.
        (
            _Probe(detected=True, checks_passed=True, date_of_birth="1990-01-15"),
            {"ROI_MRZ": True, "ROI_DOB": True},
        ),
        # Dirty/partial MRZ (checks failed): ROI_MRZ must still run.
        (
            _Probe(detected=True, checks_passed=False, date_of_birth="1990-01-15"),
            {"ROI_MRZ": False, "ROI_DOB": True},
        ),
        # MRZ not detected at all: ROI_MRZ must still run.
        (
            _Probe(detected=False, checks_passed=False, date_of_birth=None),
            {"ROI_MRZ": False, "ROI_DOB": False},
        ),
        # MRZ has DOB but flagged low-confidence: ROI_DOB must still run.
        (
            _Probe(
                detected=True,
                checks_passed=True,
                date_of_birth="1990-01-15",
                low_confidence_fields=("date_of_birth",),
            ),
            {"ROI_MRZ": True, "ROI_DOB": False},
        ),
        # MRZ detected but no DOB field at all: ROI_DOB must still run.
        (
            _Probe(detected=True, checks_passed=True, date_of_birth=None),
            {"ROI_MRZ": True, "ROI_DOB": False},
        ),
    ],
)
def test_adaptive_roi_plan_skip_flags(probe, expect_skip):
    plan = _adaptive_roi_plan(probe)
    for tag, skip in expect_skip.items():
        assert plan[tag] is skip, f"{tag} skip={plan[tag]} expected={skip}"


def test_adaptive_roi_plan_never_skips_required_rois():
    """Blood/dates/family ROIs always run — the extractor depends on them."""
    plan = _adaptive_roi_plan(
        _Probe(detected=True, checks_passed=True, date_of_birth="1990-01-15")
    )
    for tag in ("ROI_BLOOD", "ROI_DATES", "ROI_FAMILY"):
        assert plan[tag] is False
