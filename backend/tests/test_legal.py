# backend/tests/test_legal.py
# Legal documents v1.2 (0.8.2): legal/source/*.md is the only source;
# legal/build_legal.py generates the landing's /legal pages, the landing
# modals and the app's patient-aviso modal from it. These tests fail when
# a generated file is out of date, so the app and the landing can't drift
# from the .md — re-run `python3 legal/build_legal.py` after editing it.

import importlib.util
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
LEGAL_PAGES = ['index.html', 'terminos/index.html', 'aviso-usuarios/index.html', 'aviso-pacientes/index.html']

_spec = importlib.util.spec_from_file_location('build_legal', ROOT / 'legal' / 'build_legal.py')
build_legal = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(build_legal)


def _read(rel):
    return (ROOT / rel).read_text(encoding='utf-8')


def test_generated_files_match_the_markdown_sources():
    assert build_legal.stale() == [], 'run: python3 legal/build_legal.py'


@pytest.mark.parametrize('page', LEGAL_PAGES)
def test_every_legal_page_is_noindex(page):
    assert '<meta name="robots" content="noindex">' in _read(f'landing/legal/{page}')


def test_index_lists_the_three_documents_with_version_and_date():
    page = _read('landing/legal/index.html')
    assert 'Clin.IA™ se encuentra en fase Beta. Estos documentos pueden actualizarse; ' \
           'cada versión se publica aquí con su número y fecha.' in page
    for slug in ('terminos', 'aviso-usuarios', 'aviso-pacientes'):
        assert f'href="/legal/{slug}/"' in page
    assert page.count('Versión 1.2 Beta · Octubre 2026') == 3
    assert 'Versiones anteriores' in page


@pytest.mark.parametrize('slug, phrase', [
    ('terminos', 'Convenio de Tratamiento de Datos Personales'),
    ('aviso-usuarios', 'El nombre de quien recomienda se usa solo para evaluar la solicitud'),
    ('aviso-pacientes', 'Aviso de Privacidad Simplificado'),
])
def test_document_pages_carry_the_v12_text(slug, phrase):
    page = _read(f'landing/legal/{slug}/index.html')
    assert phrase in page
    assert 'Versión 1.2 Beta · Octubre 2026' in page
    assert 'BORRADOR' not in page
    assert 'href="/legal/legal.css"' in page


def test_landing_modals_carry_v12_and_no_borrador():
    page = _read('landing/index.html')
    tos = page[page.index('<!-- legal:begin terminos -->'):page.index('<!-- legal:end terminos -->')]
    aviso = page[page.index('<!-- legal:begin aviso-usuarios -->'):page.index('<!-- legal:end aviso-usuarios -->')]
    assert 'Convenio de Tratamiento de Datos Personales' in tos and 'href="/legal/terminos/"' in tos
    assert 'Aviso de Privacidad para Usuarios' in page and 'href="/legal/aviso-usuarios/"' in aviso
    assert 'BORRADOR' not in page
    # The old deep link still opens the Términos modal.
    assert "if (location.hash === '#terminos') openModal('modal-tos');" in page


def test_app_aviso_modal_shows_v12_patient_aviso(client):
    html = client.get('/').get_data(as_text=True)
    modal = html[html.index('id="avisoModal"'):html.index('<!-- /avisoModal -->')]
    assert 'Aviso de Privacidad para Pacientes' in modal
    assert 'Versión 1.2 Beta · Octubre 2026' in modal
    # v1.2-only wording (the old draft said the OMS lookup was a transfer).
    assert 'esta consulta no constituye una transferencia de datos personales' in modal
    assert 'Los datos personales del paciente no se tratan para finalidades secundarias.' in modal
    assert 'Aviso de Privacidad Simplificado' in modal
    assert 'BORRADOR' not in modal and 'aviso-draft-badge' not in modal
    # The Jinja comments in the template/partial never reach the browser.
    assert 'build_legal' not in modal


# ── the converter itself ────────────────────────────────────────────────

def test_converter_keeps_a_loose_ordered_list_together():
    _, body = build_legal.render('# T\n\n1. uno\n\n2. dos\n\n3. tres\n\nfin')
    assert body.count('<ol>') == 1 and body.count('<li>') == 3
    assert body.endswith('</ol>\n<p>fin</p>')


def test_converter_one_paragraph_per_line_and_inline_marks():
    _, body = build_legal.render(
        '# T\nUno **negrita**\n*(cursiva)*\nver clinianotes.com/legal y escribir a admin@clinianotes.com\n'
        '[info@clinianotes.com](mailto:info@clinianotes.com)')
    assert body.split('\n') == [
        '<p>Uno <strong>negrita</strong></p>',
        '<p><em>(cursiva)</em></p>',
        '<p>ver <a href="https://clinianotes.com/legal/">clinianotes.com/legal</a> y escribir a '
        '<a href="mailto:admin@clinianotes.com">admin@clinianotes.com</a></p>',
        '<p><a href="mailto:info@clinianotes.com">info@clinianotes.com</a></p>',
    ]


def test_converter_escapes_html():
    _, body = build_legal.render('# T\nUn texto con <script>alert(1)</script> & "comillas"')
    assert '<script>' not in body and '&lt;script&gt;' in body and '&amp;' in body


@pytest.mark.parametrize('md', [
    '# T\n## h2 no usado', '# T\n| tabla |', '# T\n> cita', '# T\n[x](javascript:alert(1))',
    '# T\nun * suelto', 'sin título',
])
def test_converter_rejects_unsupported_markdown(md):
    with pytest.raises(ValueError):
        build_legal.render(md)


def test_every_source_states_the_published_version():
    for doc in build_legal.DOCS:
        assert build_legal.VERSION_STAMP in (ROOT / 'legal' / 'source' / doc['src']).read_text(encoding='utf-8')
    assert re.fullmatch(r'\d+\.\d+ Beta', build_legal.VERSION)
