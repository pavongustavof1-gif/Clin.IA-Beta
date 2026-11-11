"""Fully offline: every socket connection is refused for the whole suite, so
no test can reach AssemblyAI, the Gateway or anything else even by mistake."""
import json
import socket

import pytest

from bakeoff import paths
from bakeoff.models import load_models
from bakeoff.scenarios import Scenario


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    def refuse(*a, **k):
        raise OSError('network disabled in bakeoff tests')
    monkeypatch.setattr(socket.socket, 'connect', refuse)
    monkeypatch.setattr(socket, 'create_connection', refuse)


@pytest.fixture
def models_cfg():
    return load_models()


UTTERANCES = [
    {'speaker': 'A', 'text': 'Buenos días, ¿qué le trae por aquí?'},
    {'speaker': 'B', 'text': 'Ardor al orinar desde hace dos días, sin fiebre.'},
    {'speaker': 'A', 'text': 'Le indico nitrofurantoína 100 mg cada 12 horas por 5 días y un examen general de orina.'},
]


@pytest.fixture
def text_scenario(tmp_path):
    return Scenario(id='BO-T1', title='Escenario ficticio de prueba', path=tmp_path / 'BO-T1.yaml',
                    transcript='transcripts/BO-T1.txt', speakers_expected=2, consulta='2026-11-05 09:15',
                    checks={'must_contain': [{'text': 'nitrofurantoína'}, {'text': '12 horas'}],
                            'must_not_contain': [{'text': 'ceftriaxona'}]})


def note(**over):
    base = {
        'roles_detectados': {'Hablante A': 'médico', 'Hablante B': 'paciente'},
        'subjetivo': {'motivo_de_consulta': 'Ardor al orinar', 'sintomas': ['disuria']},
        'evaluacion': {'diagnostico': 'Infección de vías urinarias baja no complicada',
                       'termino_busqueda_cie11': 'Infección de vías urinarias'},
        'plan': {'medicamentos': [{'nombre': 'Nitrofurantoína', 'dosis': '100 mg', 'frecuencia': 'cada 12 horas',
                                   'duracion': '5 días'}],
                 'estudios_solicitados': ['Examen general de orina'], 'seguimiento': 'Revaloración en 1 semana'},
    }
    base.update(over)
    return base


def gateway_ok(content, input_tokens=3500, output_tokens=1400, request_id='req-ficticio'):
    return {'request_id': request_id, 'choices': [{'message': {'role': 'assistant', 'content': content},
                                                    'finish_reason': 'stop'}],
            'usage': {'input_tokens': input_tokens, 'output_tokens': output_tokens,
                      'total_tokens': input_tokens + output_tokens}}


class FakeResponse:
    def __init__(self, status=200, body=None, headers=None, text=None):
        self.status_code, self._body, self.headers = status, body, headers or {}
        self.text = text if text is not None else (json.dumps(body) if body is not None else '')

    def json(self):
        if self._body is None:
            raise ValueError('no json')
        return self._body
