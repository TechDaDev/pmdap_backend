import unicodedata


def normalize_reference_name(value):
    return " ".join(unicodedata.normalize("NFKC", value).split()).casefold()
