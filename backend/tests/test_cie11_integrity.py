# backend/tests/test_cie11_integrity.py
# 0.8.5 — CIE-11 review, batch 2 (backend half; the frontend half is in
# tests/frontend/review_integrity.html, cases 7-10):
#   #4/#9  the code comes only from the WHO lookup or the doctor; Gemini's
#          own codigo/titulo are removed before the lookup; an empty WHO
#          code is no result; cie11_fuente records the source.
#   #6     the highest-scoring coded entity wins, clusters allowed.
#   #13    codes are normalised (trim, uppercase, no spaces) and checked
#          against the WHO syntax — warn only, never rejected.
#   #8     a code that isn't the WHO value carries no WHO title.
#   #10    diagnostico_principal → diagnostico before the lookup.
#   #11    diagnosticos_adicionales survive as a list.
#   cleanup: a confirm that already saved but whose response was lost is
#          finished on the next confirm (409, no email).
# Invented data only; WHO and Gemini are mocked, no network.

import json
import re
import types
from pathlib import Path

import pytest

import app as app_module
import icd_service
import llm_processor
from llm_processor import LLMProcessor, normalize_evaluacion, apply_cie11_lookup
from pdf_generator import PDFGenerator
from tests import tokens
from tests.conftest import DOCTOR_A1, CLINIC_A, SESSION_PENDING_A, SESSION_CONFIRMED_A
from tests.test_route_authorization import _call
from tests.test_roles_and_cedula import _pdf_text

BACKEND = Path(__file__).resolve().parent.parent


# ── WHO search: which result wins (#6, #9) ──────────────────────────────

class _Ok:
    status_code = 200

    def __init__(self, entities):
        self._body = {'destinationEntities': entities}

    def raise_for_status(self):
        pass

    def json(self):
        return self._body


@pytest.fixture
def who(monkeypatch):
    """Answer the WHO search with the given destinationEntities."""
    monkeypatch.setattr(icd_service, '_get_token', lambda: 'fake-token')
    box = {}
    monkeypatch.setattr(icd_service.requests, 'get', lambda *a, **k: _Ok(box['entities']))

    def answer(entities):
        box['entities'] = entities
        return icd_service.lookup_cie11('diagnóstico ficticio')
    return answer


def test_plain_code(who):
    assert who([{'theCode': '5A11', 'title': 'Título ficticio', 'score': 0.9}]) == \
        {'code': '5A11', 'title': 'Título ficticio'}


def test_cluster_code_is_returned_like_any_code(who):
    assert who([{'theCode': '5A24/5A11', 'title': 'Ficticio combinado', 'score': 0.8}]) == \
        {'code': '5A24/5A11', 'title': 'Ficticio combinado'}


def test_coded_entity_beats_a_higher_scoring_uncoded_one(who):
    assert who([{'title': 'Bloque ficticio', 'score': 0.99},
                {'theCode': '', 'title': 'Agrupación ficticia', 'score': 0.95},
                {'theCode': '5A11', 'title': 'Ficticio simple', 'score': 0.4}]) == \
        {'code': '5A11', 'title': 'Ficticio simple'}


def test_highest_score_wins_when_results_are_unsorted(who):
    assert who([{'theCode': '5A10', 'title': 'Uno', 'score': 0.2},
                {'theCode': '5A24/5A11', 'title': 'Dos', 'score': 0.31},
                {'theCode': '5A11', 'title': 'Tres', 'score': 0.97},
                {'theCode': '5A12', 'title': 'Cuatro', 'score': 0.5}])['code'] == '5A11'


def test_tie_and_missing_scores_keep_api_order(who):
    assert who([{'theCode': '5A10', 'title': 'Uno'}, {'theCode': '5A11', 'title': 'Dos'}])['code'] == '5A10'
    assert who([{'theCode': '5A10', 'score': 'x'}, {'theCode': '5A11', 'score': 0.1}])['code'] == '5A11'


def test_only_empty_codes_is_no_result(who):
    assert who([{'theCode': '', 'title': 'Bloque', 'score': 0.9}, {'theCode': '  ', 'title': 'Otro'},
                {'title': 'Sin código'}]) is None


def test_who_code_is_normalised_and_title_tags_stripped(who):
    assert who([{'theCode': ' 5a11 ', 'title': "<em class='found'>Ficticio</em> simple", 'score': 1}]) == \
        {'code': '5A11', 'title': 'Ficticio simple'}


# ── Format (#13) ─────────────────────────────────────────────────────────

@pytest.mark.parametrize('code', [
    '5A11', '1A00', 'BA00', 'CA40', 'NA07.0Z', 'CA23.01', 'ZZ9Z.ZZ', '5A24/5A11', '5A11/5A23',
    'GB51&XK8G&XN6P4', 'BA41.0&XA7RE3&XA7NQ7', 'DD51&XK8G/ME24.2&XT5R', '1A00&XN62R',
])
def test_valid_codes(code):
    assert icd_service.is_valid_cie11_code(code)


@pytest.mark.parametrize('code', [
    '', '5a11', '5A1', '5A111', 'A11', '5I11', '5O11', 'O123', '0A00', 'XK8G', 'XK8G&5A11',
    '5A11/', '/5A11', '5A11&', '5A11&5A12', '5A11.', '5A11.ABC', '5A11 5A12', '5A11,5A12', 'ABC',
])
def test_invalid_codes(code):
    assert not icd_service.is_valid_cie11_code(code)


def test_normalize_trims_uppercases_and_removes_spaces():
    assert icd_service.normalize_cie11_code('  5a24 / 5a11 \t') == '5A24/5A11'
    assert icd_service.normalize_cie11_code(None) == ''
    assert icd_service.normalize_cie11_code(5) == ''


def test_client_pattern_is_identical_to_server_pattern():
    js = (BACKEND / 'static' / 'app.js').read_text(encoding='utf-8')
    m = re.search(r"const CIE11_CODE_PATTERN = '([^']*)';", js)
    assert m, 'CIE11_CODE_PATTERN not found in app.js'
    assert m.group(1).replace('\\\\', '\\') == icd_service.CIE11_CODE_PATTERN


# ── Extraction: source of the code (#4, #9, #10, #11) ───────────────────

def _extract(monkeypatch, gemini_json, lookup):
    calls = []

    def fake_lookup(text, term=None):
        calls.append(text)
        if isinstance(lookup, Exception):
            raise lookup
        return lookup
    monkeypatch.setattr(llm_processor, 'lookup_cie11', fake_lookup)
    p = LLMProcessor.__new__(LLMProcessor)
    p.model_id = 'fake'
    p.client = types.SimpleNamespace(models=types.SimpleNamespace(
        generate_content=lambda **k: types.SimpleNamespace(text=json.dumps(gemini_json))))
    return p.extract_structured_data('transcripción ficticia', utterances=None), calls


GEMINI_WITH_CODE = {'evaluacion': {'diagnostico': 'Dx ficticio', 'codigo_cie11': '9Z99',
                                   'titulo_cie11': 'Inventado', 'cie11_fuente': 'oms'}}


@pytest.mark.parametrize('lookup', [None, RuntimeError('fixture: WHO down'), {'code': '', 'title': 'Bloque'}],
                         ids=['no-result', 'raises', 'empty-code'])
def test_gemini_code_is_removed_when_lookup_gives_nothing(monkeypatch, lookup):
    sd, calls = _extract(monkeypatch, GEMINI_WITH_CODE, lookup)
    assert calls == ['Dx ficticio']
    assert sd['evaluacion'] == {'diagnostico': 'Dx ficticio'}


def test_gemini_code_is_replaced_by_who_code(monkeypatch):
    sd, _ = _extract(monkeypatch, GEMINI_WITH_CODE, {'code': '5a11', 'title': 'Título OMS ficticio'})
    assert sd['evaluacion'] == {'diagnostico': 'Dx ficticio', 'codigo_cie11': '5A11',
                                'titulo_cie11': 'Título OMS ficticio', 'cie11_fuente': 'oms'}


def test_who_cluster_is_kept(monkeypatch):
    sd, _ = _extract(monkeypatch, {'evaluacion': {'diagnostico': 'Dx ficticio'}},
                     {'code': '5A24/5A11', 'title': 'Ficticio combinado'})
    assert sd['evaluacion']['codigo_cie11'] == '5A24/5A11'
    assert sd['evaluacion']['cie11_fuente'] == 'oms'


def test_gemini_code_removed_even_without_a_diagnosis(monkeypatch):
    sd, calls = _extract(monkeypatch, {'evaluacion': {'codigo_cie11': '9Z99', 'impresion_clinica': 'x'}},
                         {'code': '5A11', 'title': 'T'})
    assert calls == []
    assert sd['evaluacion'] == {'impresion_clinica': 'x'}


def test_diagnostico_principal_is_looked_up(monkeypatch):
    sd, calls = _extract(monkeypatch, {'evaluacion': {'diagnostico_principal': 'Dx principal ficticio'}},
                         {'code': '5A11', 'title': 'T'})
    assert calls == ['Dx principal ficticio']
    assert sd['evaluacion'] == {'diagnostico': 'Dx principal ficticio', 'codigo_cie11': '5A11',
                                'titulo_cie11': 'T', 'cie11_fuente': 'oms'}


def test_diagnostico_wins_over_diagnostico_principal(monkeypatch):
    sd, calls = _extract(monkeypatch, {'evaluacion': {'diagnostico': 'Dx A', 'diagnostico_principal': 'Dx B'}},
                         None)
    assert calls == ['Dx A']
    assert sd['evaluacion'] == {'diagnostico': 'Dx A', 'diagnostico_principal': 'Dx B'}


def test_blank_diagnostico_is_replaced_by_principal():
    data = {'evaluacion': {'diagnostico': '  ', 'diagnostico_principal': 'Dx B'}}
    normalize_evaluacion(data)
    assert data['evaluacion'] == {'diagnostico': 'Dx B'}


def test_validate_normalises_diagnostico_principal():
    data = {'evaluacion': {'diagnostico_principal': 'Dx ficticio'}}
    assert LLMProcessor.validate_against_schema(None, data) == (True, None)
    assert data['evaluacion'] == {'diagnostico': 'Dx ficticio'}


def test_diagnosticos_adicionales_string_becomes_list():
    data = {'evaluacion': {'diagnosticos_adicionales': 'Dx uno\n\n  Dx dos  \n'}}
    normalize_evaluacion(data)
    assert data['evaluacion']['diagnosticos_adicionales'] == ['Dx uno', 'Dx dos']
    data = {'evaluacion': {'diagnosticos_adicionales': '   '}}
    normalize_evaluacion(data)
    assert data['evaluacion'] == {}


@pytest.mark.parametrize('data', [None, [], {'evaluacion': None}, {'evaluacion': ['x']}, {'evaluacion': 'x'}])
def test_normalisers_tolerate_malformed_shapes(data, monkeypatch):
    monkeypatch.setattr(llm_processor, 'lookup_cie11', lambda t: pytest.fail('no lookup expected'))
    normalize_evaluacion(data)
    apply_cie11_lookup(data)


def test_prompt_no_longer_mentions_keeping_a_code():
    prompt = LLMProcessor.create_extraction_prompt(LLMProcessor.__new__(LLMProcessor), 'texto ficticio')
    assert 'y el código CIE-11' not in prompt
    assert 'No incluyas códigos CIE-11' in prompt
    assert '"codigo_cie11"' not in prompt and '"titulo_cie11"' not in prompt


# ── Confirm: server-side normalisation and source (#4, #8, #13) ─────────

def _set_pending_ev(db, ev):
    row = next(r for r in db.tables['sesiones'] if r['session_id'] == SESSION_PENDING_A)
    sd = {'informacion_paciente': {'nombre_del_paciente': 'Paciente de Prueba'}, 'evaluacion': ev}
    row['structured_data'] = sd
    row['full_response'] = {**row['full_response'], 'structured_data': sd}


def _confirm_ev(client, ev, create_pdf=False):
    return _call(client, 'POST', '/api/confirm-and-generate', token=tokens.mint(DOCTOR_A1), json_body={
        'session_id': SESSION_PENDING_A,
        'structured_data': {'informacion_paciente': {'nombre_del_paciente': 'Paciente de Prueba'},
                            'evaluacion': ev},
        'create_pdf': create_pdf, 'send_email': False,
    })


def _saved_ev(db):
    row = next(r for r in db.tables['sesiones'] if r['session_id'] == SESSION_PENDING_A)
    assert row['status'] == 'confirmed'
    return row['structured_data']['evaluacion']


WHO_PENDING = {'diagnostico': 'Dx ficticio', 'codigo_cie11': '5A24/5A11',
               'titulo_cie11': 'Título OMS ficticio', 'cie11_fuente': 'oms'}


def test_confirm_keeps_who_code_and_title(client, db):
    _set_pending_ev(db, WHO_PENDING)
    assert _confirm_ev(client, {'diagnostico': 'Dx ficticio', 'codigo_cie11': ' 5a24 / 5a11 ',
                                'titulo_cie11': 'Título OMS ficticio', 'cie11_fuente': 'oms'}).status_code == 200
    assert _saved_ev(db) == {'diagnostico': 'Dx ficticio', 'codigo_cie11': '5A24/5A11',
                             'titulo_cie11': 'Título OMS ficticio', 'cie11_fuente': 'oms'}


def test_confirm_without_fuente_from_an_old_client_still_gets_who_title(client, db):
    _set_pending_ev(db, WHO_PENDING)
    assert _confirm_ev(client, {'codigo_cie11': '5A24/5A11'}).status_code == 200
    assert _saved_ev(db) == {'codigo_cie11': '5A24/5A11', 'titulo_cie11': 'Título OMS ficticio',
                             'cie11_fuente': 'oms'}


def test_confirm_changed_code_is_the_doctors_without_title(client, db):
    _set_pending_ev(db, WHO_PENDING)
    # Even if a client claims the WHO source and sends the stale title.
    assert _confirm_ev(client, {'codigo_cie11': '5A11', 'titulo_cie11': 'Título OMS ficticio',
                                'cie11_fuente': 'oms'}).status_code == 200
    assert _saved_ev(db) == {'codigo_cie11': '5A11', 'cie11_fuente': 'medico'}


def test_confirm_cleared_code_drops_title_and_fuente(client, db):
    _set_pending_ev(db, WHO_PENDING)
    assert _confirm_ev(client, {'diagnostico': 'Dx', 'codigo_cie11': '  ', 'titulo_cie11': 'x',
                                'cie11_fuente': 'oms'}).status_code == 200
    assert _saved_ev(db) == {'diagnostico': 'Dx'}


def test_confirm_malformed_code_is_stored_normalised_not_rejected(client, db):
    _set_pending_ev(db, {'diagnostico': 'Dx ficticio'})
    assert _confirm_ev(client, {'codigo_cie11': ' abc 1 '}).status_code == 200
    assert _saved_ev(db) == {'codigo_cie11': 'ABC1', 'cie11_fuente': 'medico'}


def test_confirm_typed_code_when_who_found_nothing(client, db):
    _set_pending_ev(db, {'diagnostico': 'Dx ficticio'})
    assert _confirm_ev(client, {'codigo_cie11': '5a11', 'cie11_fuente': 'oms', 'titulo_cie11': 'x'}).status_code == 200
    assert _saved_ev(db) == {'codigo_cie11': '5A11', 'cie11_fuente': 'medico'}


def test_confirm_pre_085_pending_code_counts_as_doctors(client, db):
    _set_pending_ev(db, {'codigo_cie11': '5A11', 'titulo_cie11': 'Origen desconocido'})
    assert _confirm_ev(client, {'codigo_cie11': '5A11', 'titulo_cie11': 'Origen desconocido'}).status_code == 200
    assert _saved_ev(db) == {'codigo_cie11': '5A11', 'cie11_fuente': 'medico'}


def test_confirm_pdf_prints_doctor_code_without_title_and_never_the_source(client, db, monkeypatch):
    monkeypatch.setattr(app_module, 'pdf_generator', PDFGenerator())
    sent = []
    monkeypatch.setattr(app_module, 'send_pdf_email', lambda **k: sent.append(k['pdf_bytes']) or True)
    _set_pending_ev(db, WHO_PENDING)
    resp = _call(client, 'POST', '/api/confirm-and-generate', token=tokens.mint(DOCTOR_A1), json_body={
        'session_id': SESSION_PENDING_A,
        'structured_data': {'informacion_paciente': {'nombre_del_paciente': 'Paciente de Prueba'},
                            'evaluacion': {'diagnostico': 'Dx ficticio', 'codigo_cie11': '5A11',
                                           'titulo_cie11': 'Título OMS ficticio'}},
        'create_pdf': True, 'send_email': True,
    })
    assert resp.status_code == 200
    text = _pdf_text(sent[0])
    assert 'Código CIE-11: 5A11' in text
    assert 'Título OMS ficticio' not in text
    assert not re.search(r'\bmedico\b', text) and 'fuente' not in text.lower()


def test_pdf_prints_who_code_with_title_but_not_the_source():
    pdf = PDFGenerator().generate_pdf({
        'informacion_paciente': {'nombre_del_paciente': 'Paciente de Prueba'},
        'evaluacion': {'diagnostico': 'Dx ficticio', 'codigo_cie11': '5A24/5A11',
                       'titulo_cie11': 'Título OMS ficticio', 'cie11_fuente': 'oms',
                       'diagnosticos_adicionales': ['Adicional ficticio uno', 'Adicional ficticio dos']},
    }, session_id='SESSION-FIXTURE')
    text = _pdf_text(pdf)
    assert '5A24/5A11 — Título OMS ficticio' in text
    assert 'Adicional ficticio uno' in text and 'Adicional ficticio dos' in text
    assert not re.search(r'\boms\b', text) and 'fuente' not in text.lower()  # the value 'oms'; the title itself says OMS


def test_confirm_carries_diagnosticos_adicionales(client, db):
    _set_pending_ev(db, {'diagnostico': 'Dx ficticio'})
    assert _confirm_ev(client, {'diagnostico': 'Dx ficticio',
                                'diagnosticos_adicionales': ['Adicional uno', 'Adicional dos']}).status_code == 200
    assert _saved_ev(db)['diagnosticos_adicionales'] == ['Adicional uno', 'Adicional dos']


# ── Confirm-timeout cleanup (follow-up to #3) ───────────────────────────

@pytest.fixture
def recorded(db, monkeypatch):
    rec = {'emails': [], 'patches': [], 'deletes': []}
    monkeypatch.setattr(app_module, 'send_pdf_email', lambda **k: rec['emails'].append(k) or True)
    monkeypatch.setattr(app_module, '_sb_patch', lambda p, b: rec['patches'].append(p) or db.patch(p, b))
    monkeypatch.setattr(app_module, '_sb_delete', lambda p: rec['deletes'].append(p) or db.delete(p))
    return rec


def _confirm_confirmed(client):
    return _call(client, 'POST', '/api/confirm-and-generate', token=tokens.mint(DOCTOR_A1), json_body={
        'session_id': SESSION_CONFIRMED_A, 'structured_data': {'evaluacion': {'diagnostico': 'Dx'}},
        'create_pdf': True, 'send_email': True,
    })


def _confirmed_row(db):
    return next(r for r in db.tables['sesiones'] if r['session_id'] == SESSION_CONFIRMED_A)


def test_already_confirmed_with_leftovers_finishes_cleanup_and_409(client, db, recorded):
    _confirmed_row(db)['transcript_text'] = 'transcripción ficticia'
    db.add('trabajos', {'job_id': 'job-fixture-late', 'usuario_id': DOCTOR_A1, 'clinica_id': CLINIC_A,
                        'status': 'done', 'session_id': SESSION_CONFIRMED_A})
    resp = _confirm_confirmed(client)
    assert resp.status_code == 409
    assert resp.get_json()['error_code'] == 'SESSION_LOCKED'
    assert _confirmed_row(db)['transcript_text'] is None
    assert not any(t.get('session_id') == SESSION_CONFIRMED_A for t in db.tables['trabajos'])
    assert recorded['emails'] == []
    # Nothing else about the signed note changed.
    assert _confirmed_row(db)['status'] == 'confirmed'


def test_already_confirmed_only_transcript_left(client, db, recorded):
    db.tables['trabajos'] = [t for t in db.tables['trabajos'] if t.get('session_id') != SESSION_CONFIRMED_A]
    _confirmed_row(db)['transcript_text'] = 'transcripción ficticia'
    assert _confirm_confirmed(client).status_code == 409
    assert _confirmed_row(db)['transcript_text'] is None
    assert recorded['deletes'] == [] and recorded['emails'] == []


def test_already_confirmed_clean_note_writes_nothing(client, db, recorded):
    db.tables['trabajos'] = [t for t in db.tables['trabajos'] if t.get('session_id') != SESSION_CONFIRMED_A]
    _confirmed_row(db)['transcript_text'] = None
    resp = _confirm_confirmed(client)
    assert resp.status_code == 409 and resp.get_json()['error_code'] == 'SESSION_LOCKED'
    assert recorded == {'emails': [], 'patches': [], 'deletes': []}


def test_already_confirmed_cleanup_is_owner_only(client, db, recorded):
    from tests.conftest import DOCTOR_A2
    _confirmed_row(db)['transcript_text'] = 'transcripción ficticia'
    resp = _call(client, 'POST', '/api/confirm-and-generate', token=tokens.mint(DOCTOR_A2), json_body={
        'session_id': SESSION_CONFIRMED_A, 'structured_data': {}, 'create_pdf': False})
    assert resp.status_code == 404
    assert _confirmed_row(db)['transcript_text'] == 'transcripción ficticia'
    assert recorded == {'emails': [], 'patches': [], 'deletes': []}


# ── Review-screen markup (the harness checks visibility, this the wording) ─

def test_review_template_has_cie11_notes_and_adicionales_field():
    html = (BACKEND / 'templates' / 'index.html').read_text(encoding='utf-8')
    for note_id, text in [
        ('review_cie11_vacio', 'No se obtuvo código CIE-11; escríbalo.'),
        ('review_cie11_formato', 'Revise el formato del código CIE-11.'),
        ('review_cie11_dx_cambio', 'El diagnóstico cambió; verifique el código CIE-11.'),
    ]:
        m = re.search(rf'<p class="([^"]*)" id="{note_id}" hidden>([^<]*)</p>', html)
        assert m, note_id
        assert m.group(2) == text
        assert 'review-field-note' in m.group(1)
    assert re.search(r'<textarea id="review_diagnosticos_adicionales" class="review-textarea"', html)
    assert 'sugerido por IA' not in html
    # The textarea sits under Diagnóstico, before the CIE-11 block.
    assert html.index('id="review_diagnostico"') < html.index('id="review_diagnosticos_adicionales"') \
        < html.index('id="cie11-field-block"')
