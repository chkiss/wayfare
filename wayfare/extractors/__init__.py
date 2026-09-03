"""Extractors, in descending order of trustworthiness.

``barcode``     — the IATA BCBP string encoded in a boarding pass barcode.
                  Machine-written, fixed-width, unambiguous. Ground truth.
``kitinerary``  — KDE's extraction engine, if installed. Hand-written parsers
                  for real airline, rail and hotel documents.
``llm``         — a free language model reading OCR output, constrained to a
                  strict JSON schema. Used last, and never trusted alone.

The pipeline runs all three and merges, letting the more trustworthy source
win field by field.
"""

from . import barcode, kitinerary, llm

__all__ = ["barcode", "kitinerary", "llm"]
