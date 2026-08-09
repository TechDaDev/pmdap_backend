# M16 OCR/Date Pipeline Benchmark

- Date: 2026-08-09
- Engine: PaddleOCR 3.7.0
- DPI: 300
- Documents: 34

## Primary metrics

- Date Detection Recall: 0.6667
- Suggestion Accuracy: 0.5758
- Suggestion Precision: 1.0000
- Wrong-Suggestion Rate: 0.0000
- No-Suggestion Rate: 0.4242
- DOB False-Selection Rate: 0.0000
- Candidate-Type Accuracy: 0.8636

## By language

| language | n | detection | suggestion accuracy |
|---|---|---|---|
| ar | 12 | 0.0833 | 0.0000 |
| en | 19 | 1.0000 | 0.8947 |
| mixed | 2 | 1.0000 | 1.0000 |

## By format

| format | n | detection | suggestion accuracy |
|---|---|---|---|
| image_jpeg | 1 | 1.0000 | 1.0000 |
| image_pdf | 15 | 0.2667 | 0.2000 |
| image_png | 15 | 1.0000 | 0.8667 |
| mixed_pdf | 1 | 1.0000 | 1.0000 |
| native_pdf | 1 | 1.0000 | 1.0000 |

## By quality

| quality | n | detection | suggestion accuracy |
|---|---|---|---|
| blur | 2 | 0.5000 | 0.5000 |
| clean | 24 | 0.7083 | 0.5833 |
| low_contrast | 2 | 0.5000 | 0.5000 |
| noise | 1 | 1.0000 | 1.0000 |
| rotation | 2 | 0.5000 | 0.5000 |
| small_font | 2 | 0.5000 | 0.5000 |

## Wrong suggestions

None.
