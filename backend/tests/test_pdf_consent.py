# backend/tests/test_pdf_consent.py
# The Carta de Consentimiento page of the note PDF must carry the two
# witness signature blocks NOM-004-SSA3-2012 §10.1.1 requires ("Nombre
# completo y firma de dos testigos"), and the whole Carta must still fit
# on page 1 so the clinical note starts on page 2.
#
# Offline and PHI-free: every name/clinic/value below is made up. Uses a
# fresh PDFGenerator (not app_module.pdf_generator — conftest patches that
# instance's generate_pdf with a fake) and pypdf to read the real output.

import io
import re

from pypdf import PdfReader
from PIL import Image as PILImage

from pdf_generator import PDFGenerator


def _logo_png() -> bytes:
    buf = io.BytesIO()
    PILImage.new('RGB', (200, 200), (30, 120, 200)).save(buf, 'PNG')
    return buf.getvalue()


def _pdf_pages(structured_data: dict, doctor_info: dict) -> list:
    pdf = PDFGenerator().generate_pdf(
        structured_data, session_id='SESSION-20260101-000000-FX', doctor_info=doctor_info,
    )
    # Collapse whitespace: a long line can wrap between words, which would
    # otherwise split a phrase like "representante legal" across a newline.
    return [re.sub(r'\s+', ' ', p.extract_text()) for p in PdfReader(io.BytesIO(pdf)).pages]


def _typical():
    sd = {
        'informacion_paciente': {'nombre_del_paciente': 'Paciente de Prueba', 'curp': 'FAKE000101HDFXXX01'},
        'metadata': {'fecha_hora_consulta': '2026-01-01 10:00'},
        'subjetivo': {'motivo_de_consulta': 'chequeo de rutina (fixture)'},
    }
    di = {'nombre': 'Dr. Prueba', 'cedula': '12345678', 'clinica_nombre': 'Clinica de Prueba',
          'clinica_color': '#0F6E56', 'clinica_direccion': '', 'clinica_telefono': ''}
    return sd, di


def _worst_case():
    """Long wrapping patient/doctor/clinic names, a long address + phone
    line, CURP present and a logo — everything that can eat vertical space
    on the Carta."""
    sd = {
        'informacion_paciente': {
            'nombre_del_paciente': 'Maria Guadalupe Hernandez-Villanueva de la Cruz y Santa Maria Ortega',
            'curp': 'FAKE000101HDFXXX01',
        },
        'metadata': {'fecha_hora_consulta': '2026-01-01 10:00'},
        'subjetivo': {'motivo_de_consulta': 'chequeo de rutina (fixture)'},
    }
    di = {
        'nombre': 'Dr. Alejandro Fernando de la Torre y Villarreal Montemayor',
        'cedula': '12345678',
        'clinica_nombre': 'Centro Medico Integral de Especialidades Clinicas y Diagnostico Avanzado del Valle S.C.',
        'clinica_color': '#0F6E56',
        'clinica_direccion': 'Av. Insurgentes Sur 1234 Int. 56, Col. del Valle Centro, Alcaldia Benito Juarez, CDMX 03100',
        'clinica_telefono': '55 1234 5678',
        'clinica_logo_bytes': _logo_png(),
    }
    return sd, di


def test_consent_page_carries_both_witness_blocks_and_representative_caption():
    pages = _pdf_pages(*_typical())
    page1 = pages[0]

    for expected in (
        'CARTA DE CONSENTIMIENTO INFORMADO',
        'Firma del Paciente',
        'representante legal',
        'Firma y Sello del Médico',
        'Testigo 1',
        'Testigo 2',
        'Nombre completo:',
    ):
        assert expected in page1, f'{expected!r} missing from page 1'

    # Each witness block has its own hand-filled name line.
    assert page1.count('Nombre completo:') == 2


def test_witness_blocks_appear_only_on_the_consent_page():
    pages = _pdf_pages(*_typical())
    assert len(pages) >= 2
    assert 'Testigo' not in ' '.join(pages[1:])


def test_clinical_note_starts_on_page_two():
    pages = _pdf_pages(*_typical())
    assert len(pages) == 2
    assert 'NOTA DE EVOLUCI' in pages[1].upper()
    assert 'NOTA DE EVOLUCI' not in pages[0].upper()


def test_carta_still_fits_on_one_page_in_the_worst_case():
    """Long names + long clinic line + CURP + logo: the Carta (witness
    grid and Beta watermark included) must still end on page 1, with the
    clinical note starting on page 2 and no spill-over or blank page."""
    pages = _pdf_pages(*_worst_case())
    assert len(pages) == 2, f'expected Carta + note = 2 pages, got {len(pages)}'

    page1, page2 = pages
    for expected in ('CARTA DE CONSENTIMIENTO INFORMADO', 'Testigo 1', 'Testigo 2',
                     'representante legal', 'Firma y Sello del Médico', 'SOLO PARA PRUEBAS'):
        assert expected in page1, f'{expected!r} missing from page 1'
    assert 'NOTA DE EVOLUCI' in page2.upper()
    assert 'Testigo' not in page2
