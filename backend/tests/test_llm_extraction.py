# backend/tests/test_llm_extraction.py
# Regression coverage for the "extraction crashes when a required field is
# missing from the transcript" bug: a consultation where the patient's
# identity is never spoken aloud is normal input, and must not fail the
# job — llm_processor.validate_against_schema now normalizes every
# non-clinical field that Gemini's own prompt can omit (informacion_paciente,
# metadata) to {} in place instead of failing.
#
# No PHI: every structured_data fixture below is synthetic.

import app as app_module

llm_processor = app_module.llm_processor


def test_missing_informacion_paciente_is_normalized_not_rejected():
    """The exact repro: informacion_paciente entirely absent because the
    patient's name/identity was never mentioned. Must not fail validation,
    and must land as a structurally valid, review-pending placeholder."""
    data = {
        'subjetivo': {'motivo_de_consulta': 'dolor de garganta (fixture)'},
    }

    is_valid, error = llm_processor.validate_against_schema(data)

    assert is_valid is True
    assert error is None
    assert data['informacion_paciente'] == {}


def test_malformed_informacion_paciente_type_is_normalized():
    """Defense in depth: if Gemini returns informacion_paciente as some
    other JSON type instead of an object, normalize rather than crash or
    silently pass through a shape the review screen doesn't expect."""
    data = {
        'informacion_paciente': 'not-an-object',
        'plan': {'tratamiento': 'reposo (fixture)'},
    }

    is_valid, error = llm_processor.validate_against_schema(data)

    assert is_valid is True
    assert error is None
    assert data['informacion_paciente'] == {}


def test_present_informacion_paciente_is_left_untouched():
    """Normalization must only fill in absence — real extracted values are
    never overwritten or dropped."""
    data = {
        'informacion_paciente': {'nombre_del_paciente': 'Paciente de Prueba (fixture)'},
        'subjetivo': {'motivo_de_consulta': 'chequeo (fixture)'},
    }

    is_valid, error = llm_processor.validate_against_schema(data)

    assert is_valid is True
    assert data['informacion_paciente'] == {'nombre_del_paciente': 'Paciente de Prueba (fixture)'}


def test_missing_metadata_is_normalized_not_rejected():
    """The general case: metadata is the other non-clinical field Gemini's
    prompt can omit entirely (e.g. no fecha_consulta/medico/duracion
    mentioned). Must normalize to {} exactly like informacion_paciente,
    not crash — and must still be a plain dict afterward so app.py's
    structured_data['metadata']['fecha_hora_consulta'] = ... assignment
    right after validate_against_schema is safe."""
    data = {
        'informacion_paciente': {'nombre_del_paciente': 'Paciente de Prueba (fixture)'},
        'subjetivo': {'motivo_de_consulta': 'dolor de garganta (fixture)'},
    }

    is_valid, error = llm_processor.validate_against_schema(data)

    assert is_valid is True
    assert error is None
    assert data['metadata'] == {}
    data['metadata']['fecha_hora_consulta'] = '2026-01-01T00:00:00'  # must not raise


def test_malformed_metadata_type_is_normalized():
    data = {
        'informacion_paciente': {},
        'metadata': 'not-an-object',
        'evaluacion': {'diagnostico': 'faringitis (fixture)'},
    }

    is_valid, error = llm_processor.validate_against_schema(data)

    assert is_valid is True
    assert data['metadata'] == {}


def test_both_informacion_paciente_and_metadata_missing_is_normalized():
    """Both non-clinical fields omitted at once — the fully bare case a
    short, low-detail consultation could plausibly produce."""
    data = {'objetivo': {'hallazgos': ['garganta enrojecida (fixture)']}}

    is_valid, error = llm_processor.validate_against_schema(data)

    assert is_valid is True
    assert error is None
    assert data['informacion_paciente'] == {}
    assert data['metadata'] == {}


def test_no_meaningful_content_at_all_still_fails():
    """Unchanged behavior: the "did we extract anything real" check is a
    different guard from patient-identity presence and must still catch a
    genuinely empty extraction (e.g. silent/unusable audio)."""
    data = {'informacion_paciente': {}}

    is_valid, error = llm_processor.validate_against_schema(data)

    assert is_valid is False
    assert error == "No meaningful medical data extracted"
