"""Extract Algerian phone numbers from free text (Latin / Arabic-Indic digits, +213, spaces, dots)."""
import re

_DIGITS = str.maketrans("٠١٢٣٤٥٦٧٨٩۰۱۲۳۴۵۶۷۸۹", "01234567890123456789")
_CANDIDATE = re.compile(r"(?<![\d+])(\+?\d(?:[ .\-]?\d){7,13})(?!\d)")
_MOBILE = re.compile(r"0[567]\d{8}")
_LANDLINE = re.compile(r"0[234]\d{7}")


def normalize(raw: str):
    d = re.sub(r"\D", "", raw.translate(_DIGITS))
    if d.startswith("00213"):
        d = "0" + d[5:]
    elif d.startswith("213") and len(d) >= 11:
        d = "0" + d[3:]
    elif len(d) == 9 and d[0] in "567":
        d = "0" + d
    if _MOBILE.fullmatch(d) or _LANDLINE.fullmatch(d):
        return d
    return None


def extract_phones(text: str):
    if not text:
        return []
    found = []
    for m in _CANDIDATE.finditer(text.translate(_DIGITS)):
        p = normalize(m.group(1))
        if p and p not in found:
            found.append(p)
    return found
