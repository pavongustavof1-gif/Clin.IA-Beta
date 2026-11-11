import pytest

from bakeoff import checks
from bakeoff.tests.conftest import note


@pytest.mark.parametrize('a,b', [('Nitrofurantoína', 'nitrofurantoina'), ('EXAMEN  general\nde orina', 'examen general de orina'),
                                 ('Mañana', 'manana'), ('Higiénico-Dietético', 'higienico-dietetico')])
def test_normalize(a, b):
    assert checks.normalize(a) == checks.normalize(b) == b


def test_must_contain_ignores_case_accents_and_field_names():
    n = note()
    res = checks.run_checks(n, {'must_contain': [{'text': 'NITROFURANTOINA'}, {'text': '12 horas'},
                                                 {'text': 'dosis'}]})       # 'dosis' is only a key
    assert [r['passed'] for r in res] == [True, True, False]


def test_path_scoped_must_not_contain():
    prescribed = note(plan={'medicamentos': [{'nombre': 'Ibuprofeno', 'dosis': '400 mg'}]})
    mentioned = note(subjetivo={'motivo_de_consulta': 'Tomó ibuprofeno sin mejoría'})
    rule = {'must_not_contain': [{'path': 'plan.medicamentos', 'text': 'ibuprofeno'}]}
    assert checks.run_checks(prescribed, rule)[0]['passed'] is False
    assert checks.run_checks(mentioned, rule)[0]['passed'] is True


def test_must_have_fields_and_list_index():
    n = note()
    res = checks.run_checks(n, {'must_have_fields': ['evaluacion.diagnostico', 'plan.medicamentos.0.dosis',
                                                     'objetivo.signos_vitales', 'plan.medicamentos.5']})
    assert [r['passed'] for r in res] == [True, True, False, False]


def test_partial_words_match():
    assert checks.run_checks(note(plan={'seguimiento': 'Tratamiento indefinido'}),
                             {'must_not_contain': [{'text': 'indefinid'}]})[0]['passed'] is False


def test_everything_fails_without_a_note():
    rules = {'must_contain': [{'text': 'x'}], 'must_not_contain': [{'text': 'y'}], 'must_have_fields': ['a']}
    assert all(not r['passed'] for r in checks.run_checks(None, rules, parse_ok=False))


def test_check_rows_and_by_model(text_scenario):
    recs = [{'scenario': 'BO-T1', 'model': 'm1', 'run': 1, 'http_status': 200, 'parse_ok': True, 'valid': True,
             'note': note()},
            {'scenario': 'BO-T1', 'model': 'm1', 'run': 2, 'http_status': 200, 'parse_ok': False, 'valid': False,
             'note': None}]
    rows = checks.check_records(recs, {'BO-T1': text_scenario})
    assert [r['checks_passed'] for r in rows] == [3, 0] and rows[1]['failed_checks'].startswith('+nitrofurantoína')
    summary = checks.by_model(rows)[0]
    assert summary['checks_pass_rate'] == 0.5 and summary['parse_ok_rate'] == 0.5 and summary['runs_all_checks_passed'] == 1


def test_consistency():
    a = note()
    b = note(plan={**note()['plan'], 'seguimiento': 'Cita en 2 semanas'})
    recs = [{'scenario': 'S', 'model': 'm', 'run': i, 'parse_ok': True, 'note': n} for i, n in enumerate([a, a, b], 1)]
    row = checks.consistency(recs)[0]
    assert row['diagnosis_agreement'] == 1.0 and row['medications_agreement'] == 1.0
    assert row['followup_agreement'] == pytest.approx(0.667, abs=1e-3)
    recs[2] = {'scenario': 'S', 'model': 'm', 'run': 3, 'parse_ok': False, 'note': None}
    assert checks.consistency(recs)[0]['diagnosis_agreement'] == pytest.approx(0.667, abs=1e-3)


def test_any_of_must_contain_passes_on_any_alternative():
    rule = {'must_contain': [{'any_of': ['3 días', 'tres días']}]}
    assert checks.run_checks(note(plan={'seguimiento': 'Revaloración en TRES DIAS'}), rule)[0]['passed'] is True
    assert checks.run_checks(note(plan={'seguimiento': 'Revaloración en 3 dias'}), rule)[0]['passed'] is True
    res = checks.run_checks(note(plan={'seguimiento': 'Revaloración en una semana'}), rule)[0]
    assert res['passed'] is False and res['text'] == '3 días | tres días'
    assert checks.describe(res) == '+3 días | tres días'


def test_any_of_respects_path_and_must_not_contain():
    rule = {'must_not_contain': [{'path': 'objetivo', 'any_of': ['muslo', 'implícit']}]}
    in_objetivo = note(objetivo={'examen_fisico': 'Dolor irradiado a muslo'})
    elsewhere = note(subjetivo={'motivo_de_consulta': 'Refiere dolor en el muslo'})
    assert checks.run_checks(in_objetivo, rule)[0]['passed'] is False
    assert checks.run_checks(elsewhere, rule)[0]['passed'] is True
    assert checks.run_checks(note(objetivo={'hallazgos': 'Consentimiento implícito'}), rule)[0]['passed'] is False


def test_any_of_fails_without_a_note():
    rules = {'must_contain': [{'any_of': ['a', 'b']}], 'must_not_contain': [{'any_of': ['c']}]}
    assert [r['passed'] for r in checks.run_checks(None, rules, parse_ok=False)] == [False, False]
