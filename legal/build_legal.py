#!/usr/bin/env python3
"""
legal/build_legal.py — turn the legal documents' markdown into the static
HTML that ships. The .md files in legal/source/ are the only thing to
edit; everything below is generated from them and committed (Render's
static site has no build step, and the app has no markdown dependency):

  landing/legal/index.html                      list of documents
  landing/legal/<slug>/index.html               one page per document
  landing/index.html  (between legal:begin/end markers)
                                                the Términos and Aviso
                                                (usuarios) modals
  backend/templates/legal/aviso_pacientes.html  the app's avisoModal body

Usage (stdlib only, any Python 3.10+):
  python3 legal/build_legal.py           write the files
  python3 legal/build_legal.py --check   exit 1 if any is out of date
backend/tests/test_legal.py runs the --check comparison in CI, so the
pages and the app can't drift from the sources.

The converter handles only the markdown these documents use — headings
(# title, ### section), paragraphs (one per line: the sources put each
data item on its own line), "- "/"* " and "1. " lists, ---, **bold**,
*italic* and [text](mailto:/https:) links — and raises on anything else,
so new syntax fails loudly instead of rendering wrong.
"""

import html
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SOURCE = ROOT / 'legal' / 'source'
LANDING = ROOT / 'landing'
APP_PARTIAL = ROOT / 'backend' / 'templates' / 'legal' / 'aviso_pacientes.html'

VERSION = '1.2 Beta'
FECHA = 'Octubre 2026'
# Every source must state this, so a new .md can't ship under the old
# version label by accident.
VERSION_STAMP = 'Versión 1.2 (Beta), Octubre 2026'

DOCS = [
    {'slug': 'terminos', 'src': 'Terminos_de_Servicio_v1.2.md',
     'titulo': 'Términos de Servicio'},
    {'slug': 'aviso-usuarios', 'src': 'Aviso_de_privacidad_usuarios_v1.2.md',
     'titulo': 'Aviso de Privacidad para Usuarios'},
    {'slug': 'aviso-pacientes', 'src': 'Aviso_de_privacidad_pacientes_v1.2.md',
     'titulo': 'Aviso de Privacidad para Pacientes'},
]

# Landing modal id → document slug.
LANDING_MODALS = {'terminos': 'terminos', 'aviso-usuarios': 'aviso-usuarios'}


# ── markdown subset → HTML ──────────────────────────────────────────────

_LINK = re.compile(r'\[([^\]]+)\]\(((?:mailto:|https://)[^)\s]+)\)')
_BOLD = re.compile(r'\*\*(.+?)\*\*')
_ITALIC = re.compile(r'(?<![*\w])\*(?![\s*])(.+?)(?<![\s*])\*(?![*\w])')
_LEGAL_URL = re.compile(r'(?<![/\w.@])clinianotes\.com/legal(?![/\w])')
# A bare address, not one already inside a mailto: href or link text.
_EMAIL = re.compile(r'(?<![:>\w.])([\w.]+@clinianotes\.com)(?![\w<])')
_UNSUPPORTED = re.compile(r'^(#{2}\s|#{4,}|#[^#\s]|\||>|```|<)')


def _inline(text: str, a_class: str | None) -> str:
    cls = f' class="{a_class}"' if a_class else ''
    out = html.escape(text, quote=True)
    out = _LINK.sub(lambda m: f'<a{cls} href="{m.group(2)}">{m.group(1)}</a>', out)
    out = _LEGAL_URL.sub(f'<a{cls} href="https://clinianotes.com/legal/">clinianotes.com/legal</a>', out)
    out = _EMAIL.sub(lambda m: f'<a{cls} href="mailto:{m.group(1)}">{m.group(1)}</a>', out)
    out = _BOLD.sub(r'<strong>\1</strong>', out)
    out = _ITALIC.sub(r'<em>\1</em>', out)
    if '*' in out or '](' in out:
        raise ValueError(f'unconverted markdown left in: {text!r}')
    return out


def render(md: str, h_tag: str = 'h2', h_class: str | None = None,
           a_class: str | None = None) -> tuple[str, str]:
    """(title, body_html) for one document. The '# ' title is returned
    apart from the body, so each target can place it (or not)."""
    title = None
    parts = []
    list_tag = None
    # A blank line inside a list only ends it if the next line isn't
    # another item of the same list ("loose" list, e.g. 1. … blank … 2.).
    after_blank = False
    hcls = f' class="{h_class}"' if h_class else ''

    def close_list():
        nonlocal list_tag
        if list_tag:
            parts.append(f'</{list_tag}>')
            list_tag = None

    for n, raw in enumerate(md.splitlines(), 1):
        line = raw.rstrip()
        if not line:
            after_blank = True
            continue
        m = re.match(r'^([-*]|(\d+)\.) (.*)$', line)
        if after_blank and not (m and list_tag == ('ol' if m.group(2) else 'ul')):
            close_list()
        after_blank = False
        if line.startswith('# '):
            if title is not None or parts:
                raise ValueError(f'line {n}: "# " title must be the first block')
            title = line[2:].strip()
            continue
        if line.startswith('### '):
            close_list()
            parts.append(f'<{h_tag}{hcls}>{_inline(line[4:].strip(), a_class)}</{h_tag}>')
            continue
        if line == '---':
            close_list()
            parts.append('<hr>')
            continue
        if m:
            tag = 'ol' if m.group(2) else 'ul'
            if list_tag != tag:
                close_list()
                start = int(m.group(2)) if m.group(2) else 1
                parts.append(f'<{tag} start="{start}">' if start != 1 else f'<{tag}>')
                list_tag = tag
            parts.append(f'<li>{_inline(m.group(3).strip(), a_class)}</li>')
            continue
        if _UNSUPPORTED.match(line):
            raise ValueError(f'line {n}: unsupported markdown: {line!r}')
        close_list()
        parts.append(f'<p>{_inline(line.strip(), a_class)}</p>')
    close_list()
    if title is None:
        raise ValueError('document has no "# " title')
    return html.escape(title), '\n'.join(parts)


def _load(doc: dict) -> str:
    md = (SOURCE / doc['src']).read_text(encoding='utf-8')
    if VERSION_STAMP not in md:
        raise ValueError(f'{doc["src"]} does not state "{VERSION_STAMP}"')
    return md


def _generated_note(src: str) -> str:
    return (f'Generated by legal/build_legal.py from legal/source/{src}. '
            f'Do not edit here: edit the .md and re-run the script.')


# ── page templates ──────────────────────────────────────────────────────

_HEAD = '''<!DOCTYPE html>
<html lang="es">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <!-- Access is invitation-only during the Beta. -->
    <meta name="robots" content="noindex">
    <title>{title} — Clin.IA™</title>
    <link rel="icon" type="image/x-icon" href="/favicon/favicon.ico">
    <link rel="preconnect" href="https://fonts.googleapis.com">
    <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
    <link href="https://fonts.googleapis.com/css2?family=DM+Serif+Display:ital@0;1&family=Outfit:wght@300;400;500;600;700&display=swap" rel="stylesheet">
    <link rel="stylesheet" href="/legal/legal.css">
</head>
<body>
    <header class="legal-nav">
        <div class="legal-nav-inner">
            <a href="/" class="legal-logo">Clin<span>.</span>IA</a>
            <a href="/legal/" class="legal-nav-link">Documentos legales</a>
        </div>
    </header>
'''

_FOOT = '''
    <footer class="legal-footer">
        <p>© 2026 Clin.IA™ · <a href="mailto:info@clinianotes.com">info@clinianotes.com</a></p>
    </footer>
</body>
</html>
'''


def _doc_page(doc: dict) -> str:
    title, body = render(_load(doc), h_tag='h2')
    return (
        f'<!-- {_generated_note(doc["src"])} -->\n'
        + _HEAD.format(title=html.escape(doc['titulo']))
        + f'''
    <main class="legal-main">
        <p class="legal-crumb"><a href="/legal/">Documentos legales</a></p>
        <h1>{title}</h1>
        <p class="legal-meta">Versión {VERSION} · {FECHA}
            <button type="button" class="legal-print" onclick="window.print()">Imprimir</button></p>
        <article class="legal-doc">
{body}
        </article>
    </main>
''' + _FOOT)


def _index_page() -> str:
    items = '\n'.join(
        f'''            <li>
                <a href="/legal/{d['slug']}/">{html.escape(d['titulo'])}</a>
                <span class="legal-item-meta">Versión {VERSION} · {FECHA}</span>
            </li>''' for d in DOCS)
    return (
        '<!-- Generated by legal/build_legal.py. Do not edit here: edit the script and re-run it. -->\n'
        + _HEAD.format(title='Documentos legales')
        + f'''
    <main class="legal-main">
        <h1>Documentos legales</h1>
        <p class="legal-lead">Clin.IA™ se encuentra en fase Beta. Estos documentos pueden actualizarse; cada versión se publica aquí con su número y fecha.</p>
        <ul class="legal-list">
{items}
        </ul>
        <h2>Versiones anteriores</h2>
        <p>Aún no se han publicado versiones anteriores.</p>
    </main>
''' + _FOOT)


def _modal_body(doc: dict) -> str:
    _, body = render(_load(doc), h_tag='h3')
    return (
        f'<!-- {_generated_note(doc["src"])} -->\n'
        + body
        + f'\n<p class="modal-legal-link">Versión {VERSION} · {FECHA} · '
          f'<a href="/legal/{doc["slug"]}/">Ver en Documentos legales (versión para imprimir)</a></p>'
    )


# App copy only: the patient aviso's bracketed Responsable placeholders
# get a data-responsable key, and static/app.js replaces the bracket with
# the signed-in doctor's value from /api/aviso-responsable. Placeholder →
# (key, how many times the source has it). "[correo]" is deliberately
# not here: it stays bracketed. The landing /legal page keeps every
# bracket (it's the generic template).
APP_RESPONSABLE_FIELDS = {
    '[Nombre completo del médico]':          ('nombre', 2),
    '[número]':                              ('cedula', 1),
    '[domicilio del consultorio o clínica]': ('domicilio', 2),
}


def _mark_responsable(body: str) -> str:
    for placeholder, (key, expected) in APP_RESPONSABLE_FIELDS.items():
        old = f'<strong>{html.escape(placeholder)}</strong>'
        found = body.count(old)
        if found != expected:
            raise ValueError(f'aviso-pacientes: expected {placeholder!r} {expected}x, found {found}x')
        body = body.replace(old, f'<strong data-responsable="{key}">{html.escape(placeholder)}</strong>')
    return body


def _app_partial() -> str:
    doc = next(d for d in DOCS if d['slug'] == 'aviso-pacientes')
    _, body = render(_load(doc), h_tag='h3', h_class='aviso-section-title', a_class='aviso-email-link')
    return f'{{# {_generated_note(doc["src"])} #}}\n{_mark_responsable(body)}\n'


def _landing_with_modals(current: str) -> str:
    out = current
    for marker, slug in LANDING_MODALS.items():
        doc = next(d for d in DOCS if d['slug'] == slug)
        begin, end = f'<!-- legal:begin {marker} -->', f'<!-- legal:end {marker} -->'
        if out.count(begin) != 1 or out.count(end) != 1:
            raise ValueError(f'landing/index.html needs exactly one {begin} … {end} pair')
        head, rest = out.split(begin, 1)
        _, tail = rest.split(end, 1)
        out = f'{head}{begin}\n{_modal_body(doc)}\n{end}{tail}'
    return out


def outputs() -> dict[Path, str]:
    """Every generated file and its expected content."""
    files = {LANDING / 'legal' / 'index.html': _index_page()}
    for d in DOCS:
        files[LANDING / 'legal' / d['slug'] / 'index.html'] = _doc_page(d)
    landing_index = LANDING / 'index.html'
    files[landing_index] = _landing_with_modals(landing_index.read_text(encoding='utf-8'))
    files[APP_PARTIAL] = _app_partial()
    return files


def stale() -> list[str]:
    """Generated files whose content on disk differs from the sources."""
    return [str(p.relative_to(ROOT)) for p, text in outputs().items()
            if not p.exists() or p.read_text(encoding='utf-8') != text]


def main(argv: list[str]) -> int:
    if '--check' in argv:
        bad = stale()
        for path in bad:
            print(f'out of date: {path}')
        return 1 if bad else 0
    for path, text in outputs().items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding='utf-8')
        print(f'wrote {path.relative_to(ROOT)}')
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
