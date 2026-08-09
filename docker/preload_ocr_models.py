import os

from paddleocr import PaddleOCR

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
