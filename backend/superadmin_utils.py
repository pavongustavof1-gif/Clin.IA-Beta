# backend/superadmin_utils.py
# Pure helpers for the superadmin review page (#53, Review C). No I/O:
# the routes in app.py own every DB/Auth call.

import re
import unicodedata

# Same invisible characters solicitud_validation strips from the form.
_INVISIBLE_RE = re.compile('[​⁠﻿]')
_NON_ALNUM_RE = re.compile(r'[^0-9a-z]+')


def normalize_match(value) -> str:
    """
    Fold a clinic name for matching only (never stored): lowercase,
    accents removed (NFKD, combining marks dropped), invisible characters
    and punctuation removed, whitespace collapsed. "Clínica  Sán-Ángel,
    S.C." and "clinica san angel sc" fold to the same string.
    """
    if not isinstance(value, str):
        return ''
    value = _INVISIBLE_RE.sub('', value)
    value = unicodedata.normalize('NFKD', value.lower())
    value = ''.join(c for c in value if not unicodedata.combining(c))
    # Punctuation between letters is removed, not turned into a space:
    # "S.C." must fold to "sc", not "s c".
    value = re.sub(r"[.\-'’,/&]", '', value)
    return _NON_ALNUM_RE.sub(' ', value).strip()


def suggest_clinica(q, clinicas: list) -> str | None:
    """
    id of the clinic whose normalized nombre best matches the requested
    razón social, or None. A match is an exact fold or a substring either
    way; among those, an exact fold wins, then the smallest length
    difference (the closest name), then the first listed. A suggestion
    only — the superadmin still picks the clinic.
    """
    target = normalize_match(q)
    if not target:
        return None
    best = None
    for clinica in clinicas:
        name = normalize_match(clinica.get('nombre'))
        if not name:
            continue
        if name == target:
            return clinica.get('id')
        if name in target or target in name:
            score = abs(len(name) - len(target))
            if best is None or score < best[0]:
                best = (score, clinica.get('id'))
    return best[1] if best else None
