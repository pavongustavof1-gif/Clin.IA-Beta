# backend/solicitud_validation.py
# Normalization + validation for the public access-request form
# (POST /api/solicitudes, #53/#54). Pure functions, no I/O: the route in
# app.py owns rate limiting, the honeypot, duplicate checks and the
# insert. Every limit here mirrors a CHECK on public.solicitudes_registro
# so a request that passes this never hits a DB constraint error — the
# DB check stays the real boundary.

import re

ROLES_SOLICITABLES = ('medico', 'admin', 'auxiliar')
# Roles that are doctors and so must give cédula + especialidad
# (same split as auth.is_medico_role; DB CHECK: rol_solicitado =
# 'auxiliar' OR cedula/especialidad not null).
ROLES_MEDICOS = ('medico', 'admin')

# Zero-width / BOM characters a paste from Word, WhatsApp or a PDF
# carries invisibly; they would make two equal-looking emails differ.
_INVISIBLE_RE = re.compile('[​⁠﻿]')
_WHITESPACE_RE = re.compile(r'\s+')

# ASCII-only on purpose: no quotes, '%', '!' or other characters that
# are technically legal in an RFC 5322 local part but never appear in a
# real doctor's address, and some of which are LIKE/URL metacharacters.
_EMAIL_RE = re.compile(
    r'^[a-z0-9](?:[a-z0-9._+-]*[a-z0-9])?'
    r'@(?:[a-z0-9](?:[a-z0-9-]*[a-z0-9])?\.)+[a-z]{2,}$'
)
_CEDULA_RE = re.compile(r'^[0-9]{1,12}$')

# RFC 2606 / 6761 reserved names: never a deliverable mailbox.
_RESERVED_TLDS = ('test', 'invalid', 'localhost', 'example', 'local')
_RESERVED_LABELS = ('example',)  # example.com, example.org, mail.example.mx, ...

# Obvious form-filler placeholders. Kept short on purpose — a false
# positive turns away a real doctor, a false negative only costs the
# superadmin a glance.
_PLACEHOLDER_LOCALS = frozenset({
    'test', 'prueba', 'ejemplo', 'example', 'correo', 'email', 'usuario',
    'nombre', 'asdf', 'qwerty', 'xxx', 'noreply', 'no-reply',
})
_PLACEHOLDER_DOMAINS = frozenset({
    'dominio.com', 'tudominio.com', 'midominio.com', 'ejemplo.com',
    'test.com', 'prueba.com', 'correo.com',
})

# Small, hand-picked list of throwaway-inbox providers. Not exhaustive
# (no list is) and no DNS lookups — this only filters the common ones.
# A subdomain of a listed domain matches too.
DISPOSABLE_DOMAINS = frozenset({
    'mailinator.com', 'guerrillamail.com', 'guerrillamail.net',
    'guerrillamailblock.com', 'sharklasers.com', 'grr.la',
    '10minutemail.com', '10minutemail.net', 'temp-mail.org', 'tempmail.com',
    'tempmail.net', 'tempail.com', 'tmpmail.org', 'yopmail.com', 'yopmail.fr',
    'yopmail.net', 'trashmail.com', 'trashmail.de', 'getnada.com',
    'dispostable.com', 'maildrop.cc', 'throwawaymail.com', 'fakeinbox.com',
    'mintemail.com', 'mohmal.com', 'emailondeck.com', 'mailnesia.com',
    'spamgourmet.com', 'burnermail.io', 'discard.email', 'tempr.email',
    'mail.tm', 'moakt.com', 'inboxkitten.com', 'mytemp.email', '1secmail.com',
    'mailcatch.com', 'spambox.us', 'getairmail.com', 'emailfake.com',
})

MAX_LEN = {
    'nombre': 120, 'email': 254, 'especialidad': 80, 'universidad': 160,
    'razon_social': 160, 'domicilio': 300, 'telefono': 30, 'recomendado_por': 120,
}
MIN_LEN = {'nombre': 3, 'razon_social': 2}
MIN_TELEFONO_DIGITS = 7

_REQUIRED_MSG = {
    'nombre': 'Escribe tu nombre completo.',
    'email': 'Escribe tu correo electrónico.',
    'cedula': 'Escribe tu cédula profesional.',
    'especialidad': 'Escribe tu especialidad.',
    'razon_social': 'Escribe la razón social de la clínica o consultorio.',
    'domicilio': 'Escribe el domicilio de la clínica o consultorio.',
    'telefono': 'Escribe el teléfono de la clínica o consultorio.',
}
_LABEL = {
    'nombre': 'El nombre', 'email': 'El correo', 'especialidad': 'La especialidad',
    'universidad': 'La universidad', 'razon_social': 'La razón social',
    'domicilio': 'El domicilio', 'telefono': 'El teléfono',
    'recomendado_por': 'El campo "Recomendado por"',
}


class _NotText(Exception):
    pass


def normalize_text(value):
    """strip, drop invisible characters, collapse internal whitespace.
    None/missing → ''. A non-string (number, list, object) raises
    _NotText: the form only ever sends strings, so anything else is a
    hand-built request and gets a field error, not a coercion."""
    if value is None:
        return ''
    if not isinstance(value, str):
        raise _NotText()
    value = _INVISIBLE_RE.sub('', value)
    return _WHITESPACE_RE.sub(' ', value).strip()


def normalize_email(value):
    return normalize_text(value).lower()


def normalize_cedula(value):
    """Spaces removed (people type '12 345 678'); nothing else is
    rewritten — validation decides whether what's left is digits."""
    return _WHITESPACE_RE.sub('', normalize_text(value))


def email_problem(email):
    """Spanish error message for an unacceptable email, or None."""
    if not email:
        return _REQUIRED_MSG['email']
    if len(email) > MAX_LEN['email']:
        return f"El correo no puede exceder {MAX_LEN['email']} caracteres."
    if not _EMAIL_RE.match(email) or '..' in email:
        return 'Escribe un correo electrónico válido (por ejemplo, nombre@clinica.mx).'
    local, domain = email.split('@', 1)
    if len(local) > 64:
        return 'Escribe un correo electrónico válido (por ejemplo, nombre@clinica.mx).'
    labels = domain.split('.')
    if labels[-1] in _RESERVED_TLDS or any(l in _RESERVED_LABELS for l in labels):
        return 'Ese dominio de correo no recibe mensajes. Usa tu correo real.'
    if local in _PLACEHOLDER_LOCALS or domain in _PLACEHOLDER_DOMAINS:
        return 'Ese correo parece de ejemplo. Usa tu correo real.'
    if any(domain == d or domain.endswith('.' + d) for d in DISPOSABLE_DOMAINS):
        return 'No aceptamos correos temporales. Usa tu correo profesional o personal.'
    return None


def validate_solicitud(data):
    """
    Returns (clean, errores). clean holds the normalized values ready for
    insert (column names of solicitudes_registro, optional empties as
    None); errores maps field → Spanish message. errores non-empty means
    reject with 400 and clean must not be used.

    For an auxiliar, cédula / especialidad / universidad are IGNORED, not
    rejected: the form hides them, and a value left in a hidden input (or
    sent by an older page) must never reach the row.
    """
    errores = {}
    raw = {}
    for field in ('nombre', 'email', 'rol', 'cedula', 'especialidad', 'universidad',
                  'razon_social', 'domicilio', 'telefono', 'recomendado_por'):
        try:
            if field == 'email':
                raw[field] = normalize_email(data.get(field))
            elif field == 'cedula':
                raw[field] = normalize_cedula(data.get(field))
            elif field == 'rol':
                raw[field] = normalize_text(data.get(field)).lower()
            else:
                raw[field] = normalize_text(data.get(field))
        except _NotText:
            raw[field] = ''
            errores[field] = 'Valor inválido.'

    rol = raw['rol']
    if 'rol' not in errores and rol not in ROLES_SOLICITABLES:
        errores['rol'] = 'Elige un rol.'
    es_medico = rol in ROLES_MEDICOS

    required = ['nombre', 'razon_social', 'domicilio', 'telefono']
    if es_medico:
        required += ['cedula', 'especialidad']
    for field in required:
        if field not in errores and not raw[field]:
            errores[field] = _REQUIRED_MSG[field]

    for field, n in MIN_LEN.items():
        if field not in errores and raw[field] and len(raw[field]) < n:
            errores[field] = f'{_LABEL[field]} debe tener al menos {n} caracteres.'
    for field, n in MAX_LEN.items():
        if field == 'email' or field in errores:
            continue  # email has its own checks below
        if not es_medico and field in ('especialidad', 'universidad'):
            continue  # ignored for an auxiliar, so never judged
        if len(raw[field]) > n:
            errores[field] = f'{_LABEL[field]} no puede exceder {n} caracteres.'

    if 'email' not in errores:
        problem = email_problem(raw['email'])
        if problem:
            errores['email'] = problem

    if es_medico and 'cedula' not in errores and not _CEDULA_RE.match(raw['cedula']):
        errores['cedula'] = 'La cédula profesional solo lleva números (máximo 12).'

    if 'telefono' not in errores and raw['telefono']:
        if sum(c.isdigit() for c in raw['telefono']) < MIN_TELEFONO_DIGITS:
            errores['telefono'] = 'Escribe un teléfono válido, con lada.'

    if data.get('acepta_terminos') is not True:
        errores['acepta_terminos'] = 'Debes aceptar los Términos de Servicio y el Aviso de Privacidad.'

    clean = {
        'nombre':          raw['nombre'],
        'email':           raw['email'],
        'rol_solicitado':  rol,
        'cedula':          raw['cedula'] if es_medico else None,
        'especialidad':    raw['especialidad'] if es_medico else None,
        'universidad':     (raw['universidad'] or None) if es_medico else None,
        'razon_social':    raw['razon_social'],
        'domicilio':       raw['domicilio'],
        'telefono':        raw['telefono'],
        'recomendado_por': raw['recomendado_por'] or None,
    }
    return clean, errores
