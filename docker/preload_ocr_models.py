import os

from paddleocr import PaddleOCR

PaddleOCR(
    lang=os.getenv("OCR_LANGUAGE", "ar"),
    ocr_version=os.getenv("OCR_MODEL_VERSION", "PP-OCRv5"),
    device="cpu",
    use_doc_orientation_classify=False,
    use_doc_unwarping=False,
    use_textline_orientation=False,
)
