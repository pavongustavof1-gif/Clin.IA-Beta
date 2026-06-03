# backend/pdf_generator.py
# Generates clinical note PDFs from structured SOAP data.
# Uses WeasyPrint (server-side HTML→PDF) — no external API, no device software needed.
# Paper size: Letter (8.5 × 11 in) — standard in Mexico.

import html
from typing import Dict


class PDFGenerator:
    """Converts structured SOAP data into a downloadable Letter-size PDF."""

    def generate_pdf(self, structured_data: Dict) -> bytes:
        """Return the clinical note as PDF bytes."""
        html_content = self._build_html(structured_data)
        from weasyprint import HTML
        return HTML(string=html_content).write_pdf()

    # ──────────────────────────────────────────────────────────────
    # HTML builder
    # ──────────────────────────────────────────────────────────────

    def _build_html(self, data: Dict) -> str:
        info    = data.get('informacion_paciente') or {}
        meta    = data.get('metadata')             or {}
        subj    = data.get('subjetivo')            or {}
        obj_    = data.get('objetivo')             or {}
        ev      = data.get('evaluacion')           or {}
        plan    = data.get('plan')                 or {}

        e = html.escape  # shorthand

        # ── Header values ────────────────────────────────────────
        fecha_consulta = e(str(meta.get('fecha_hora_consulta') or
                               meta.get('fecha_consulta') or ''))
        medico         = e(str(meta.get('medico') or ''))

        # ── Patient block ────────────────────────────────────────
        nombre   = e(str(info.get('nombre_del_paciente') or ''))
        fnac     = e(str(info.get('fecha_de_nacimiento') or ''))
        edad     = e(str(info.get('edad') or ''))
        genero   = e(str(info.get('genero') or ''))
        curp     = e(str(info.get('curp') or ''))
        expediente = e(str(info.get('numero_expediente') or
                         info.get('expediente') or ''))

        # ── SOAP sections ────────────────────────────────────────
        soap_html = ''
        soap_html += self._section_subjetivo(subj)
        soap_html += self._section_objetivo(obj_)
        soap_html += self._section_evaluacion(ev)
        soap_html += self._section_plan(plan)

        # ── Footer signature block ───────────────────────────────
        if medico:
            footer_center = f'<span style="font-weight:600">{medico}</span>'
        else:
            footer_center = (
                '<span style="letter-spacing:0.12em">_________________________</span><br>'
                '<span style="font-size:6.5pt;color:#888">Firma y sello del médico</span>'
            )

        # ── Optional patient ID fields ───────────────────────────
        curp_row       = f'<tr><td class="pid-label">CURP</td><td class="pid-val">{curp}</td></tr>' if curp else ''
        exp_row        = f'<tr><td class="pid-label">Expediente</td><td class="pid-val">{expediente}</td></tr>' if expediente else ''

        return f"""<!DOCTYPE html>
<html lang="es">
<head>
<meta charset="UTF-8">
<style>
/* ── Page setup ── */
@page {{
    size: 8.5in 11in;          /* Tamaño carta — estándar en México */
    margin: 15mm 18mm 20mm 18mm;
}}

/* ── Reset ── */
* {{ box-sizing: border-box; margin: 0; padding: 0; }}

body {{
    font-family: Arial, Helvetica, sans-serif;
    font-size: 8.5pt;
    color: #1a1a1a;
    line-height: 1.4;
}}

/* ── Clinic header ── */
.header {{
    background: #0F6E56;
    color: #fff;
    padding: 8pt 12pt 7pt;
    display: flex;
    justify-content: space-between;
    align-items: flex-start;
    margin-bottom: 6pt;
}}
.header-left {{ flex: 1; }}
.header-right {{ text-align: right; }}
.clinic-name {{ font-family: Georgia, 'Times New Roman', serif; font-size: 13pt; font-weight: bold; }}
.clinic-divider {{ border: none; border-top: 1px solid rgba(255,255,255,0.4); margin: 3pt 0; }}
.clinic-address {{ font-size: 8pt; opacity: 0.85; }}
.doc-type {{ font-size: 10pt; font-weight: bold; text-transform: uppercase; letter-spacing: 0.06em; }}
.doc-date {{ font-size: 8pt; margin-top: 3pt; opacity: 0.9; }}

/* ── Patient identification block ── */
.patient-block {{
    background: #F4F2EB;
    border: 1px solid #ddd8cc;
    border-radius: 3pt;
    padding: 7pt 10pt;
    margin-bottom: 6pt;
    display: flex;
    gap: 12pt;
}}
.pid-col {{ flex: 1; }}
.pid-table {{ width: 100%; border-collapse: collapse; }}
.pid-label {{ font-weight: bold; font-size: 7.5pt; color: #555; white-space: nowrap; padding-right: 5pt; padding-bottom: 2pt; }}
.pid-val {{ font-size: 8pt; color: #1a1a1a; padding-bottom: 2pt; }}

/* ── Section divider ── */
.teal-rule {{ border: none; border-top: 1.5pt solid #0F6E56; margin: 0 0 8pt; }}

/* ── SOAP section ── */
.soap-section {{ margin-bottom: 10pt; page-break-inside: avoid; }}
.section-header {{ display: flex; align-items: baseline; gap: 5pt; margin-bottom: 4pt; border-bottom: 1px solid #e8e8e8; padding-bottom: 3pt; }}
.section-letter {{ font-family: Georgia, 'Times New Roman', serif; font-size: 20pt; font-weight: bold; color: #0F6E56; line-height: 1; }}
.section-title {{ font-size: 9pt; font-weight: bold; text-transform: uppercase; letter-spacing: 0.05em; color: #333; }}

/* ── Field rows ── */
.field-row {{ margin-bottom: 4pt; }}
.field-label {{ font-weight: bold; font-size: 8pt; color: #444; }}
.field-value {{ font-size: 8.5pt; color: #1a1a1a; }}

/* ── Bullets ── */
.bullet-list {{ margin: 2pt 0 4pt 10pt; }}
.bullet-item {{ margin-bottom: 2pt; }}
.bullet-item::before {{ content: "\\2022  "; color: #0F6E56; font-weight: bold; }}

/* ── Vitals grid ── */
.vitals-grid {{ display: flex; flex-wrap: wrap; gap: 8pt; margin-bottom: 3pt; }}
.vital-item {{ font-size: 8pt; }}
.vital-label {{ font-weight: bold; color: #444; }}

/* ── Medications table ── */
.meds-table {{ width: 100%; border-collapse: collapse; margin-top: 3pt; margin-bottom: 4pt; font-size: 8pt; }}
.meds-table th {{ background: #e8e8e8; font-weight: bold; padding: 3pt 5pt; text-align: left; border: 1px solid #ccc; }}
.meds-table td {{ padding: 2.5pt 5pt; border: 1px solid #ddd; vertical-align: top; }}
.meds-table tr:nth-child(even) td {{ background: #f9f9f9; }}

/* ── Fixed footer (every page) ── */
.page-footer {{
    position: fixed;
    bottom: 0;
    left: 0;
    right: 0;
    border-top: 1pt solid #0F6E56;
    padding: 4pt 18mm 0;
    display: flex;
    justify-content: space-between;
    align-items: flex-start;
    font-size: 7pt;
    color: #888;
}}
.footer-center {{ text-align: center; flex: 1; font-size: 8pt; color: #444; }}
</style>
</head>
<body>

<!-- Header -->
<div class="header">
  <div class="header-left">
    <div class="clinic-name">Consultorio Médico</div>
    <hr class="clinic-divider">
    <div class="clinic-address">Dirección del consultorio</div>
  </div>
  <div class="header-right">
    <div class="doc-type">Nota de Evolución Clínica</div>
    <div class="doc-date">{fecha_consulta}</div>
  </div>
</div>

<!-- Patient identification -->
<div class="patient-block">
  <div class="pid-col">
    <table class="pid-table">
      <tr><td class="pid-label">Nombre</td><td class="pid-val">{nombre}</td></tr>
      <tr><td class="pid-label">F. Nacimiento</td><td class="pid-val">{fnac}</td></tr>
      <tr><td class="pid-label">Edad</td><td class="pid-val">{edad}</td></tr>
      <tr><td class="pid-label">Sexo</td><td class="pid-val">{genero}</td></tr>
    </table>
  </div>
  <div class="pid-col">
    <table class="pid-table">
      {curp_row}
      {exp_row}
      <tr><td class="pid-label">Fecha consulta</td><td class="pid-val">{fecha_consulta}</td></tr>
    </table>
  </div>
</div>

<hr class="teal-rule">

<!-- SOAP body -->
{soap_html}

<!-- Footer (fixed, appears on every page) -->
<div class="page-footer">
  <span>Generado por Clin.IA · clinianotes.com</span>
  <span class="footer-center">{footer_center}</span>
  <span>Página 1</span>
</div>

</body>
</html>"""

    # ──────────────────────────────────────────────────────────────
    # SOAP section builders
    # ──────────────────────────────────────────────────────────────

    def _section_subjetivo(self, subj: Dict) -> str:
        e = html.escape
        rows = []

        motivo = subj.get('motivo_de_consulta')
        if motivo:
            rows.append(f'<div class="field-row"><span class="field-label">Motivo de consulta: </span>'
                        f'<span class="field-value">{e(str(motivo))}</span></div>')

        sintomas = subj.get('sintomas') or []
        if isinstance(sintomas, list) and sintomas:
            items = ''.join(f'<li class="bullet-item">{e(str(s))}</li>' for s in sintomas)
            rows.append(f'<div class="field-row"><span class="field-label">Síntomas:</span>'
                        f'<ul class="bullet-list">{items}</ul></div>')
        elif isinstance(sintomas, str) and sintomas.strip():
            rows.append(f'<div class="field-row"><span class="field-label">Síntomas: </span>'
                        f'<span class="field-value">{e(sintomas)}</span></div>')

        historia = subj.get('historia_de_enfermedad_actual')
        if historia:
            rows.append(f'<div class="field-row"><span class="field-label">Historia de la enfermedad: </span>'
                        f'<span class="field-value">{e(str(historia))}</span></div>')

        duracion = subj.get('duracion_sintomas')
        if duracion:
            rows.append(f'<div class="field-row"><span class="field-label">Duración de síntomas: </span>'
                        f'<span class="field-value">{e(str(duracion))}</span></div>')

        if not rows:
            return ''
        return self._wrap_section('S', 'Subjetivo', rows)

    def _section_objetivo(self, obj_: Dict) -> str:
        e = html.escape
        rows = []

        # Signos vitales — compact inline grid, show only present fields
        vitales = obj_.get('signos_vitales') or {}
        vital_map = [
            ('presion_arterial',       'PA'),
            ('frecuencia_cardiaca',    'FC'),
            ('temperatura',            'Temp'),
            ('frecuencia_respiratoria','FR'),
            ('saturacion_oxigeno',     'SpO₂'),
        ]
        vital_items = []
        for key, label in vital_map:
            val = vitales.get(key)
            if val:
                vital_items.append(
                    f'<span class="vital-item"><span class="vital-label">{label}:</span> {e(str(val))}</span>'
                )
        if vital_items:
            rows.append(f'<div class="field-row"><span class="field-label">Signos vitales: </span>'
                        f'<div class="vitals-grid">{"".join(vital_items)}</div></div>')

        examen = obj_.get('examen_fisico')
        if examen:
            rows.append(f'<div class="field-row"><span class="field-label">Examen físico: </span>'
                        f'<span class="field-value">{e(str(examen))}</span></div>')

        hallazgos = obj_.get('hallazgos')
        if isinstance(hallazgos, list) and hallazgos:
            items = ''.join(f'<li class="bullet-item">{e(str(h))}</li>' for h in hallazgos)
            rows.append(f'<div class="field-row"><span class="field-label">Hallazgos:</span>'
                        f'<ul class="bullet-list">{items}</ul></div>')
        elif isinstance(hallazgos, str) and hallazgos.strip():
            rows.append(f'<div class="field-row"><span class="field-label">Hallazgos: </span>'
                        f'<span class="field-value">{e(hallazgos)}</span></div>')

        if not rows:
            return ''
        return self._wrap_section('O', 'Objetivo', rows)

    def _section_evaluacion(self, ev: Dict) -> str:
        e = html.escape
        rows = []

        diag = ev.get('diagnostico') or ev.get('diagnostico_principal')
        if diag:
            rows.append(f'<div class="field-row"><span class="field-label">Diagnóstico: </span>'
                        f'<span class="field-value" style="font-weight:600">{e(str(diag))}</span></div>')

        diag_add = ev.get('diagnosticos_adicionales') or []
        if isinstance(diag_add, list) and diag_add:
            items = ''.join(f'<li class="bullet-item">{e(str(d))}</li>' for d in diag_add)
            rows.append(f'<div class="field-row"><span class="field-label">Diagnósticos adicionales:</span>'
                        f'<ul class="bullet-list">{items}</ul></div>')

        impresion = ev.get('impresion_clinica')
        if impresion:
            rows.append(f'<div class="field-row"><span class="field-label">Impresión clínica: </span>'
                        f'<span class="field-value">{e(str(impresion))}</span></div>')

        pronostico = ev.get('pronostico')
        if pronostico:
            rows.append(f'<div class="field-row"><span class="field-label">Pronóstico: </span>'
                        f'<span class="field-value">{e(str(pronostico))}</span></div>')

        if not rows:
            return ''
        return self._wrap_section('A', 'Evaluación', rows)

    def _section_plan(self, plan: Dict) -> str:
        e = html.escape
        rows = []

        tratamiento = plan.get('tratamiento')
        if tratamiento:
            rows.append(f'<div class="field-row"><span class="field-label">Tratamiento: </span>'
                        f'<span class="field-value">{e(str(tratamiento))}</span></div>')

        # Medications table
        meds = plan.get('medicamentos') or plan.get('medicamentos_prescritos') or []
        if isinstance(meds, list) and meds:
            rows.append(self._meds_table(meds))

        recomendaciones = plan.get('recomendaciones') or []
        if isinstance(recomendaciones, list) and recomendaciones:
            items = ''.join(f'<li class="bullet-item">{e(str(r))}</li>' for r in recomendaciones)
            rows.append(f'<div class="field-row"><span class="field-label">Recomendaciones:</span>'
                        f'<ul class="bullet-list">{items}</ul></div>')

        estudios = plan.get('estudios_solicitados') or []
        if isinstance(estudios, list) and estudios:
            items = ''.join(f'<li class="bullet-item">{e(str(s))}</li>' for s in estudios)
            rows.append(f'<div class="field-row"><span class="field-label">Estudios solicitados:</span>'
                        f'<ul class="bullet-list">{items}</ul></div>')
        elif isinstance(estudios, str) and estudios.strip():
            rows.append(f'<div class="field-row"><span class="field-label">Estudios solicitados: </span>'
                        f'<span class="field-value">{e(estudios)}</span></div>')

        seguimiento = plan.get('seguimiento')
        if seguimiento:
            rows.append(f'<div class="field-row"><span class="field-label">Seguimiento: </span>'
                        f'<span class="field-value">{e(str(seguimiento))}</span></div>')

        if not rows:
            return ''
        return self._wrap_section('P', 'Plan', rows)

    def _meds_table(self, meds: list) -> str:
        e = html.escape
        rows_html = ''
        for m in meds:
            if isinstance(m, dict):
                nombre    = e(str(m.get('nombre')    or ''))
                dosis     = e(str(m.get('dosis')     or ''))
                frecuencia= e(str(m.get('frecuencia') or ''))
                duracion  = e(str(m.get('duracion')  or ''))
            else:
                parts = [p.strip() for p in str(m).split('-')]
                nombre, dosis, frecuencia, duracion = (
                    e(parts[0]) if len(parts) > 0 else '',
                    e(parts[1]) if len(parts) > 1 else '',
                    e(parts[2]) if len(parts) > 2 else '',
                    e(parts[3]) if len(parts) > 3 else '',
                )
            rows_html += (f'<tr><td>{nombre}</td><td>{dosis}</td>'
                          f'<td>{frecuencia}</td><td>{duracion}</td></tr>')

        return (
            '<div class="field-row"><span class="field-label">Medicamentos:</span>'
            '<table class="meds-table">'
            '<thead><tr><th>Medicamento</th><th>Dosis</th><th>Frecuencia</th><th>Duración</th></tr></thead>'
            f'<tbody>{rows_html}</tbody>'
            '</table></div>'
        )

    def _wrap_section(self, letter: str, title: str, rows: list) -> str:
        content = '\n'.join(rows)
        return (
            f'<div class="soap-section">'
            f'  <div class="section-header">'
            f'    <span class="section-letter">{letter}</span>'
            f'    <span class="section-title">{title}</span>'
            f'  </div>'
            f'  {content}'
            f'</div>'
        )
