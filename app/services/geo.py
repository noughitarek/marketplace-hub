import unicodedata

WILAYAS = [
    "Adrar", "Chlef", "Laghouat", "Oum El Bouaghi", "Batna", "Béjaïa", "Biskra", "Béchar", "Blida",
    "Bouira", "Tamanrasset", "Tébessa", "Tlemcen", "Tiaret", "Tizi Ouzou", "Alger", "Djelfa", "Jijel",
    "Sétif", "Saïda", "Skikda", "Sidi Bel Abbès", "Annaba", "Guelma", "Constantine", "Médéa",
    "Mostaganem", "M'Sila", "Mascara", "Ouargla", "Oran", "El Bayadh", "Illizi", "Bordj Bou Arréridj",
    "Boumerdès", "El Tarf", "Tindouf", "Tissemsilt", "El Oued", "Khenchela", "Souk Ahras", "Tipaza",
    "Mila", "Aïn Defla", "Naâma", "Aïn Témouchent", "Ghardaïa", "Relizane", "El M'Ghair", "El Menia",
    "Ouled Djellal", "Bordj Badji Mokhtar", "Béni Abbès", "Timimoun", "Touggourt", "Djanet",
    "In Salah", "In Guezzam",
]
WILAYA_BY_CODE = {i + 1: n for i, n in enumerate(WILAYAS)}


def _norm(s):
    s = unicodedata.normalize("NFKD", str(s or "")).encode("ascii", "ignore").decode().lower()
    return "".join(c for c in s if c.isalnum())


_BY_NORM = {_norm(n): c for c, n in WILAYA_BY_CODE.items()}
_BY_NORM.update({"algiers": 16, "algier": 16, "alger": 16})


def wilaya_code_from(value):
    """Accepts a code ('16'), a name ('Alger', 'alger centre'...) and returns the wilaya code or None."""
    if value in (None, ""):
        return None
    s = str(value).strip()
    if s.isdigit() and 1 <= int(s) <= 58:
        return int(s)
    n = _norm(s)
    if n in _BY_NORM:
        return _BY_NORM[n]
    for key, code in sorted(_BY_NORM.items(), key=lambda kv: -len(kv[0])):
        if len(key) > 3 and key in n:
            return code
    return None
