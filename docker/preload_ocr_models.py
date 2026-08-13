import os

from paddleocr import PaddleOCR

# Primary Arabic OCR pipeline (full-card reads: labels + Arabic values).
PaddleOCR(
    text_detection_model_name=os.getenv(
        "OCR_TEXT_DETECTION_MODEL_NAME", "PP-OCRv5_mobile_det"
    ),
    text_recognition_model_name=os.getenv(
        "OCR_TEXT_RECOGNITION_MODEL_NAME", "arabic_PP-OCRv5_mobile_rec"
    ),
    device="cpu",
    enable_mkldnn=False,
    use_doc_orientation_classify=False,
    use_doc_unwarping=False,
    use_textline_orientation=False,
)

# Secondary Latin/multilingual pipeline for targeted ROI reads (blood group,
# printed dates, family number, MRZ). Kept in the worker image so the OCR
# worker never needs a runtime model download.
PaddleOCR(
    text_detection_model_name=os.getenv(
        "OCR_LATIN_DETECTION_MODEL_NAME", "PP-OCRv6_medium_det"
    ),
    text_recognition_model_name=os.getenv(
        "OCR_LATIN_RECOGNITION_MODEL_NAME", "PP-OCRv6_medium_rec"
    ),
    device="cpu",
    enable_mkldnn=False,
    use_doc_orientation_classify=False,
    use_doc_unwarping=False,
    use_textline_orientation=False,
)
