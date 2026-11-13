# backend/tests/test_legal.py
# Legal documents (v1.2 in 0.8.2; patient aviso v1.3 in 0.9.0): legal/source/*.md is the only source;
# legal/build_legal.py generates the landing's /legal pages, the landing
# modals and the app's patient-aviso modal from it. These tests fail when
# a generated file is out of date, so the app and the landing can't drift
# from the .md — re-run `python3 legal/build_legal.py` after editing it.

import importlib.util
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
LEGAL_PAGES = ['index.html', 'terminos/index.html', 'aviso-usuarios/index.html', 'aviso-pacientes/index.html',
               'terminos/v1.2/index.html', 'aviso-pacientes/v1.2/index.html']

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
    assert page.count('Versión 1.2 Beta · Octubre 2026') == 3     # AU, and ToS/AP 1.2 as previous versions
    assert page.count('Versión 1.3 Beta · Noviembre 2026 · Vigente a partir del 28 de noviembre de 2026') == 2
    previous = page[page.index('Versiones anteriores'):]
    assert 'href="/legal/terminos/v1.2/"' in previous and 'href="/legal/aviso-pacientes/v1.2/"' in previous
    assert previous.count('Vigente hasta el 27 de noviembre de 2026') == 2
    assert 'Aún no se han publicado versiones anteriores' not in page


@pytest.mark.parametrize('slug, phrase, meta', [
    ('terminos', 'Convenio de Tratamiento de Datos Personales', 'Versión 1.3 Beta · Noviembre 2026'),
    ('aviso-usuarios', 'El nombre de quien recomienda se usa solo para evaluar la solicitud',
     'Versión 1.2 Beta · Octubre 2026'),
    ('aviso-pacientes', 'Aviso de Privacidad Simplificado', 'Versión 1.3 Beta · Noviembre 2026'),
])
def test_document_pages_carry_the_current_text(slug, phrase, meta):
    page = _read(f'landing/legal/{slug}/index.html')
    assert phrase in page
    assert meta in page
    assert 'BORRADOR' not in page
    assert 'href="/legal/legal.css"' in page


def test_landing_modals_carry_the_current_texts_and_no_borrador():
    page = _read('landing/index.html')
    tos = page[page.index('<!-- legal:begin terminos -->'):page.index('<!-- legal:end terminos -->')]
    aviso = page[page.index('<!-- legal:begin aviso-usuarios -->'):page.index('<!-- legal:end aviso-usuarios -->')]
    assert 'Convenio de Tratamiento de Datos Personales' in tos and 'href="/legal/terminos/"' in tos
    assert 'Versión 1.3 (Beta), Noviembre 2026.' in tos and 'Versión 1.2 (Beta)' not in tos
    assert 'Aviso de Privacidad para Usuarios' in page and 'href="/legal/aviso-usuarios/"' in aviso
    assert 'BORRADOR' not in page
    # The old deep link still opens the Términos modal.
    assert "if (location.hash === '#terminos') openModal('modal-tos');" in page


def test_patient_aviso_v13_with_v12_kept_as_previous_version():
    v13 = _read('landing/legal/aviso-pacientes/index.html')
    assert 'Versión 1.3 (Beta), Noviembre 2026. Vigente a partir del 28 de noviembre de 2026.' in v13
    v12 = _read('landing/legal/aviso-pacientes/v1.2/index.html')
    assert 'Versión 1.2 (Beta), Octubre 2026.' in v12 and 'Versión anterior' in v12
    assert 'href="/legal/aviso-pacientes/"' in v12 and '<meta name="robots" content="noindex">' in v12
    # The old source is never edited: it still states only its own version.
    old = (ROOT / 'legal' / 'source' / 'Aviso_de_privacidad_pacientes_v1.2.md').read_text(encoding='utf-8')
    assert 'Versión 1.3' not in old


def test_patient_aviso_v13_section_4_names_assemblyai_for_both_and_not_google():
    src = (ROOT / 'legal' / 'source' / 'Aviso_de_privacidad_pacientes_v1.3.md').read_text(encoding='utf-8')
    s4 = src[src.index('### 4.'):src.index('### 5.')]
    assert ('(i) transcripción de audio a texto y (ii) procesamiento de lenguaje natural para la extracción '
            'y estructuración de la información clínica a partir de la transcripción, mediante un modelo de '
            'lenguaje que AssemblyAI opera a través de su proveedor de infraestructura Amazon Web Services, '
            'Inc. (Estados Unidos).') in s4
    assert 'no son conservados por AssemblyAI ni por su proveedor (retención cero)' in s4
    assert 'Google' not in src and 'Gemini' not in src
    page = _read('landing/legal/aviso-pacientes/index.html')
    assert 'Amazon Web Services, Inc.' in page and 'Gemini' not in page


def test_terminos_v13_clause_12_with_v12_kept_as_previous_version():
    src = (ROOT / 'legal' / 'source' / 'Terminos_de_Servicio_v1.3.md').read_text(encoding='utf-8')
    c12 = src[src.index('### 12.'):src.index('### 13.')]
    assert c12.count('\n\n') >= 3                                   # three paragraphs, first one unchanged
    assert ('Clin.IA™ podrá actualizar estos Términos. Cada versión se publica, con su número y fecha, en '
            'clinianotes.com/legal, donde permanecen disponibles las versiones anteriores.') in c12
    assert ('Las modificaciones sustanciales, en particular las relativas al tratamiento de los datos de los '
            'pacientes, a los subencargados o a las obligaciones del médico usuario, se notificarán al correo '
            'electrónico registrado con al menos 15 días naturales de anticipación. Si el médico usuario '
            'continúa utilizando el Servicio a partir de la fecha de entrada en vigor, se entenderá que acepta '
            'las modificaciones. Si no las acepta, podrá terminar el Servicio conforme a la cláusula 11 antes '
            'de esa fecha, sin penalización alguna.') in c12
    assert 'aceptación expresa' not in c12
    assert 'Versión 1.3 (Beta), Noviembre 2026. Vigente a partir del 28 de noviembre de 2026.' in src
    v12 = _read('landing/legal/terminos/v1.2/index.html')
    assert 'Versión 1.2 (Beta), Octubre 2026.' in v12 and 'Versión anterior' in v12
    assert 'aceptación expresa del médico usuario' in v12
    old = (ROOT / 'legal' / 'source' / 'Terminos_de_Servicio_v1.2.md').read_text(encoding='utf-8')
    assert 'Versión 1.3' not in old


def test_terminos_version_names_the_published_documents():
    import config
    assert build_legal.version_code() == 'ToS-1.3-Beta+AU-1.2-Beta+AP-1.3-Beta'
    assert config.Config.TERMINOS_VERSION == build_legal.version_code()


def test_app_aviso_modal_shows_current_patient_aviso(client):
    html = client.get('/').get_data(as_text=True)
    modal = html[html.index('id="avisoModal"'):html.index('<!-- /avisoModal -->')]
    assert 'Aviso de Privacidad para Pacientes' in modal
    ap = next(d for d in build_legal.DOCS if d['slug'] == 'aviso-pacientes')
    assert f"Versión {ap['version']} · {ap['fecha']}" in modal
    assert 'Vigente a partir del 28 de noviembre de 2026' in modal
    # v1.2+ wording (the old draft said the OMS lookup was a transfer).
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


def test_every_source_states_its_published_version():
    for doc in build_legal.DOCS + build_legal.PREVIOUS:
        assert doc['stamp'] in (ROOT / 'legal' / 'source' / doc['src']).read_text(encoding='utf-8')
        assert re.fullmatch(r'\d+\.\d+ Beta', doc['version'])
        assert doc['stamp'] == f"Versión {doc['version'].replace(' Beta', '')} (Beta), {doc['fecha']}"


def test_a_source_without_its_stamp_is_refused():
    doc = {**build_legal.DOCS[2], 'stamp': 'Versión 9.9 (Beta), Nunca'}
    with pytest.raises(ValueError, match='does not state'):
        build_legal._load(doc)
