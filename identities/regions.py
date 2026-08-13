"""Targeted ROI extraction regions for the Iraqi National Card.

Centralizes normalized (relative) coordinates so region crops survive image
resolution changes. Only in-memory working copies are produced; original
evidence images are never modified, stored or committed.

Each region declares which engine reads it:

* ``arabic`` — the worker's main engine (settings Arabic recognizer). Good for
  the family number row where the Arabic model reads the full alphanumeric
  value cleanly.
* ``latin`` — the secondary Latin/multilingual engine (PP-OCRv6_medium_rec).
  Needed for the blood group, printed dates (the Arabic model truncates the
  trailing digit) and the MRZ (Latin model reads MRZ line 3 cleanly).

Engines are injected per-process singletons from ``processing.ocr_provider``
so this class stays deterministic and testable.
"""
from __future__ import annotations

from PIL import Image

from identities.extraction import SideLine

# region tag -> {side, x, y, w, h (fractions), scale (upscale factor), engine}
REGIONS = {
    "ROI_BLOOD": {
        "side": "FRONT",
        "x": 0.30,
        "y": 0.80,
        "w": 0.40,
        "h": 0.20,
        "scale": 3,
        "engine": "latin",
    },
    "ROI_DATES": {
        "side": "BACK",
        "x": 0.03,
        "y": 0.06,
        "w": 0.52,
        "h": 0.24,
        "scale": 2,
        "engine": "latin",
    },
    "ROI_DOB": {
        "side": "BACK",
        "x": 0.03,
        "y": 0.24,
        "w": 0.52,
        "h": 0.20,
        "scale": 2,
        "engine": "latin",
    },
    "ROI_FAMILY": {
        "side": "BACK",
        "x": 0.03,
        "y": 0.36,
        "w": 0.59,
        "h": 0.20,
        "scale": 2,
        "engine": "arabic",
    },
    "ROI_MRZ": {
        "side": "BACK",
        "x": 0.0,
        "y": 0.66,
        "w": 1.0,
        "h": 0.34,
        "scale": 2,
        "engine": "latin",
    },
}


class IraqiNationalCardRegionExtractor:
    """Runs targeted OCR passes over defined card regions."""

    def __init__(self, arabic_engine, latin_engine):
        self._arabic = arabic_engine
        self._latin = latin_engine

    def _engine_for(self, engine_kind):
        return self._latin if engine_kind == "latin" else self._arabic

    @staticmethod
    def crop(image, spec):
        width, height = image.size
        box = (
            int(spec["x"] * width),
            int(spec["y"] * height),
            int((spec["x"] + spec["w"]) * width),
            int((spec["y"] + spec["h"]) * height),
        )
        crop = image.crop(box)
        scale = spec.get("scale", 1)
        if scale > 1:
            crop = crop.resize(
                (crop.width * scale, crop.height * scale), Image.LANCZOS
            )
        return crop

    def extract_region(self, image, tag, spec):
        crop = self.crop(image, spec)
        result = self._engine_for(spec["engine"]).extract_image(crop)
        return [
            SideLine(tag, line.text, line.confidence) for line in result.lines
        ]

    def run(self, front_image, back_image):
        images = {"FRONT": front_image, "BACK": back_image}
        lines = []
        for tag, spec in REGIONS.items():
            image = images[spec["side"]]
            if image is None:
                continue
            lines.extend(self.extract_region(image, tag, spec))
        return lines

    @staticmethod
    def region_tags():
        return list(REGIONS)
