"""`blind`: one randomly chosen run per model per scenario, as anonymous HTML.

The pick is seeded and the seed is saved with the key. Labels A, B, C, D… are
shuffled per scenario. No model name appears anywhere in bakeoff/blind/: not in
file names, titles, metadata or text — a note that names a model itself has
that word masked as "[modelo]" (and the command says so).
"""
import csv
import html
import json
import random
import re
import string
from datetime import datetime, timezone
from pathlib import Path

from bakeoff import paths

SCORE_COLUMNS = ['note_label', 'completeness', 'accuracy_no_inventions', 'clinical_usefulness',
                 'safety_error', 'comment']

# Words that would reveal a model or vendor, masked if a note contains them.
VENDOR_WORDS = ['gemini', 'claude', 'anthropic', 'google', 'haiku', 'sonnet', 'opus', 'flash-lite', 'flash',
                'vertex', 'bedrock', 'openai', 'gpt', 'assemblyai', 'gateway']

SECTION_LABELS = {
    'informacion_paciente': 'Información del paciente', 'subjetivo': 'Subjetivo (S)',
    'objetivo': 'Objetivo (O)', 'evaluacion': 'Evaluación (A)', 'plan': 'Plan (P)',
    'metadata': 'Metadatos', 'actualizacion_antecedentes': 'Actualización de antecedentes',
    'roles_detectados': 'Roles detectados',
}
FIELD_LABELS = {
    'nombre_del_paciente': 'Nombre', 'fecha_de_nacimiento': 'Fecha de nacimiento', 'edad': 'Edad',
    'genero': 'Género', 'numero_expediente': 'Número de expediente', 'curp': 'CURP',
    'motivo_de_consulta': 'Motivo de consulta', 'sintomas': 'Síntomas',
    'historia_de_enfermedad_actual': 'Historia de la enfermedad actual', 'duracion_sintomas': 'Duración de síntomas',
    'signos_vitales': 'Signos vitales', 'presion_arterial': 'Presión arterial', 'frecuencia_cardiaca': 'Frecuencia cardiaca',
    'temperatura': 'Temperatura', 'frecuencia_respiratoria': 'Frecuencia respiratoria',
    'saturacion_oxigeno': 'Saturación de oxígeno', 'peso': 'Peso', 'talla': 'Talla',
    'habitus_exterior': 'Habitus exterior', 'examen_fisico': 'Examen físico', 'hallazgos': 'Hallazgos',
    'diagnostico': 'Diagnóstico', 'diagnosticos_adicionales': 'Diagnósticos adicionales',
    'impresion_clinica': 'Impresión clínica', 'pronostico': 'Pronóstico',
    'termino_busqueda_cie11': 'Término de búsqueda CIE-11 (interno)',
    'tratamiento': 'Tratamiento', 'medicamentos': 'Medicamentos', 'nombre': 'Nombre', 'dosis': 'Dosis',
    'frecuencia': 'Frecuencia', 'duracion': 'Duración', 'recomendaciones': 'Recomendaciones',
    'estudios_solicitados': 'Estudios solicitados', 'seguimiento': 'Seguimiento',
    'fecha_consulta': 'Fecha de consulta', 'medico': 'Médico', 'duracion_consulta': 'Duración de la consulta',
    'detectado': 'Detectado', 'contenido': 'Contenido',
}
SECTION_ORDER = ['informacion_paciente', 'subjetivo', 'objetivo', 'evaluacion', 'plan',
                 'actualizacion_antecedentes', 'metadata', 'roles_detectados']

STYLE = """
body{font-family:system-ui,-apple-system,"Segoe UI",sans-serif;margin:0;color:#1f2933;background:#f7f8fa}
header{background:#0f6e56;color:#fff;padding:14px 24px}header h1{margin:0;font-size:1.2rem}
header p{margin:4px 0 0;opacity:.9;font-size:.9rem}
main{display:grid;grid-template-columns:minmax(0,1fr) minmax(0,1.3fr);gap:20px;padding:20px 24px}
@media(max-width:900px){main{grid-template-columns:1fr}}
section.panel{background:#fff;border:1px solid #d9dee5;border-radius:8px;padding:16px 20px}
h2{font-size:1.05rem;margin:0 0 12px}h3{font-size:.95rem;margin:16px 0 6px;color:#0f6e56}
.utt{margin:0 0 8px;line-height:1.45}.spk{font-weight:600;color:#52606d}
dl{margin:0}dt{font-weight:600;margin-top:8px;font-size:.88rem;color:#3e4c59}dd{margin:2px 0 0 0;white-space:pre-wrap}
ul{margin:4px 0 0 18px;padding:0}.med{margin:4px 0}.empty{color:#9aa5b1;font-style:italic}
.fail{background:#fff4e5;border:1px solid #f0b46a;padding:12px;border-radius:6px}
nav a{margin-right:12px}
"""


class BlindError(RuntimeError):
    pass


def _label(key: str) -> str:
    return FIELD_LABELS.get(key) or SECTION_LABELS.get(key) or key.replace('_', ' ').capitalize()


def _value_html(value) -> str:
    if value is None or value == '' or value == [] or value == {}:
        return '<span class="empty">(vacío)</span>'
    if isinstance(value, bool):
        return 'Sí' if value else 'No'
    if isinstance(value, dict):
        return '<dl>' + ''.join(f'<dt>{html.escape(_label(k))}</dt><dd>{_value_html(v)}</dd>'
                                for k, v in value.items()) + '</dl>'
    if isinstance(value, list):
        if all(isinstance(v, dict) for v in value):
            return ''.join(f'<div class="med">{_value_html(v)}</div>' for v in value)
        return '<ul>' + ''.join(f'<li>{_value_html(v)}</li>' for v in value) + '</ul>'
    return html.escape(str(value))


def note_html(record: dict) -> str:
    if record.get('http_status') != 200 or record.get('raw_response_text') is None:
        return '<div class="fail">La llamada falló: no hay nota para esta ejecución.</div>'
    if not record.get('parse_ok'):
        return '<div class="fail">La respuesta no se pudo leer como JSON (falla de formato).</div>'
    note = record.get('note')
    if not isinstance(note, dict):
        return '<div class="fail">La respuesta es JSON, pero no es un objeto de nota.</div>'
    keys = [k for k in SECTION_ORDER if k in note] + [k for k in note if k not in SECTION_ORDER]
    return ''.join(f'<h3>{html.escape(_label(k))}</h3>{_value_html(note[k])}' for k in keys)


def transcript_html(utterances) -> str:
    return ''.join(f'<p class="utt"><span class="spk">Hablante {html.escape(str(u["speaker"]))}:</span> '
                   f'{html.escape(u["text"])}</p>' for u in utterances)


def page(title: str, subtitle: str, body: str) -> str:
    # Deliberately no <meta name="generator">, no comments, nothing identifying.
    return (f'<!doctype html><html lang="es"><head><meta charset="utf-8">'
            f'<meta name="viewport" content="width=device-width,initial-scale=1">'
            f'<title>{html.escape(title)}</title><style>{STYLE}</style></head><body>'
            f'<header><h1>{html.escape(title)}</h1><p>{html.escape(subtitle)}</p></header>{body}</body></html>')


def mask_model_names(text: str, names) -> tuple[str, int]:
    words = sorted({w.lower() for w in list(names) + VENDOR_WORDS if w}, key=len, reverse=True)
    count = 0
    for w in words:
        pattern = re.compile(re.escape(w), re.IGNORECASE)
        text, n = pattern.subn('[modelo]', text)
        count += n
    return text, count


def make_blind(scenarios, records, utterances_for, model_names, seed=None, blind_dir=None, key_dir=None,
               scores_dir=None, force=False, log=print) -> dict:
    blind_dir, key_dir, scores_dir = blind_dir or paths.BLIND, key_dir or paths.KEY, scores_dir or paths.SCORES
    key_file, scores_file = key_dir / 'key.json', scores_dir / 'blind_scores.csv'
    if not force and (key_file.exists() or scores_file.exists()):
        raise BlindError(f'{key_file} or {scores_file} already exists — your key/scores would be replaced. '
                         'Move them aside or pass --force.')
    seed = seed if seed is not None else random.SystemRandom().randrange(2 ** 32)
    rng = random.Random(seed)
    key = {'seed': seed, 'created_at': datetime.now(timezone.utc).isoformat(timespec='seconds'), 'scenarios': {}}
    score_rows, masked_total = [], 0
    # The HTML is derived output: always rebuilt, so no page from an earlier
    # pick (with different labels) can survive next to the new key.
    if blind_dir.exists():
        for f in sorted(blind_dir.rglob('*.html')):
            f.unlink()
    index_links = []
    for scenario in sorted(scenarios, key=lambda s: s.id):
        by_model = {}
        for rec in records:
            if rec['scenario'] == scenario.id:
                by_model.setdefault(rec['model'], []).append(rec)
        if not by_model:
            log(f'{scenario.id}: no runs — skipped')
            continue
        chosen = {}
        for model in sorted(by_model):
            runs = sorted(by_model[model], key=lambda r: r['run'])
            chosen[model] = rng.choice(runs)
        models = sorted(chosen)
        rng.shuffle(models)
        labels = list(string.ascii_uppercase[:len(models)])
        key['scenarios'][scenario.id] = {lab: {'model': m, 'run': chosen[m]['run'], 'run_file': chosen[m]['_file']}
                                         for lab, m in zip(labels, models)}
        utterances = utterances_for(scenario)
        sdir = blind_dir / scenario.id
        sdir.mkdir(parents=True, exist_ok=True)
        subtitle = f'{scenario.title} · consulta {scenario.consulta} · datos inventados'
        nav = '<nav>' + ''.join(f'<a href="nota_{lab}.html">Nota {lab}</a>' for lab in labels) + '</nav>'
        for lab, m in zip(labels, models):
            body = (f'<main><section class="panel"><h2>Transcripción</h2>{transcript_html(utterances)}</section>'
                    f'<section class="panel"><h2>Nota {lab}</h2>{nav}{note_html(chosen[m])}</section></main>')
            text, masked = mask_model_names(page(f'{scenario.id} — Nota {lab}', subtitle, body), model_names)
            if masked:
                log(f'{scenario.id} nota {lab}: WARNING {masked} model/vendor word(s) masked as [modelo]')
                masked_total += masked
            (sdir / f'nota_{lab}.html').write_text(text, encoding='utf-8')
            score_rows.append({'note_label': f'{scenario.id}/{lab}', 'completeness': '', 'accuracy_no_inventions': '',
                               'clinical_usefulness': '', 'safety_error': '', 'comment': ''})
        index_links.append(f'<li><a href="{scenario.id}/nota_A.html">{html.escape(scenario.id)} — '
                           f'{html.escape(scenario.title)}</a> ({len(labels)} notas)</li>')
        log(f'{scenario.id}: {len(labels)} notes → {sdir}')
    (blind_dir).mkdir(parents=True, exist_ok=True)
    instructions = ('<p>Califique cada nota del 1 al 5 en completitud, exactitud (sin inventar datos) y '
                    'utilidad clínica, e indique si contiene un error de seguridad (y/n). Anote las '
                    'calificaciones en scores/blind_scores.csv.</p>')
    index = page('Evaluación ciega', 'Datos inventados — nunca pacientes reales',
                 f'<main><section class="panel">{instructions}<ul>{"".join(index_links)}</ul></section></main>')
    (blind_dir / 'index.html').write_text(mask_model_names(index, model_names)[0], encoding='utf-8')
    key_dir.mkdir(parents=True, exist_ok=True)
    key_file.write_text(json.dumps(key, ensure_ascii=False, indent=2), encoding='utf-8')
    scores_dir.mkdir(parents=True, exist_ok=True)
    with scores_file.open('w', newline='', encoding='utf-8') as f:
        w = csv.DictWriter(f, fieldnames=SCORE_COLUMNS)
        w.writeheader()
        w.writerows(score_rows)
    log(f'key (seed {seed}) → {key_file}; fill in {scores_file}')
    return key
