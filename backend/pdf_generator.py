# backend/pdf_generator.py
# PDF generation using ReportLab — pure Python, no system dependencies
# Produces Letter-size (tamaño carta) clinical notes for Mexican medical practice

import html
from io import BytesIO
from datetime import datetime
from typing import Dict

from reportlab.lib import colors
from reportlab.lib.pagesizes import LETTER
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.lib.enums import TA_LEFT, TA_CENTER, TA_RIGHT
from reportlab.platypus import (
    SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle,
    HRFlowable, KeepTogether,
)
from reportlab.pdfgen import canvas
from reportlab.platypus import BaseDocTemplate, Frame, PageTemplate


class PDFGenerator:
    """
    Generates Letter-size clinical notes (Nota de Evolución) as PDF bytes.
    Uses ReportLab — pure Python, no system libraries required.
    """

    # Brand colors
    TEAL        = colors.HexColor('#0F6E56')
    TEAL_LIGHT  = colors.HexColor('#E1F5EE')
    GRAY_LIGHT  = colors.HexColor('#F4F2EB')
    GRAY_MID    = colors.HexColor('#D3D1C7')
    GRAY_TEXT   = colors.HexColor('#5F5E5A')
    DARK        = colors.HexColor('#2C2C2A')
    WHITE       = colors.white

    # SOAP section accent colors
    COLOR_S = colors.HexColor('#0F6E56')   # teal
    COLOR_O = colors.HexColor('#185FA5')   # blue
    COLOR_A = colors.HexColor('#BA7517')   # amber
    COLOR_P = colors.HexColor('#A32D2D')   # red

    def __init__(self):
        self.styles = {
            'normal': ParagraphStyle(
                'normal',
                fontName='Helvetica',
                fontSize=8.5,
                textColor=self.DARK,
                leading=8.5 * 1.4,
                spaceAfter=2 * mm,
            ),
            'bold_label': ParagraphStyle(
                'bold_label',
                fontName='Helvetica-Bold',
                fontSize=8.5,
                textColor=self.DARK,
            ),
            'section_title': ParagraphStyle(
                'section_title',
                fontName='Helvetica-Bold',
                fontSize=9,
                textColor=self.DARK,
                spaceBefore=4 * mm,
                spaceAfter=1 * mm,
            ),
            'soap_letter': ParagraphStyle(
                'soap_letter',
                fontName='Helvetica-Bold',
                fontSize=18,
                textColor=self.TEAL,
                spaceBefore=1 * mm,
            ),
            'patient_label': ParagraphStyle(
                'patient_label',
                fontName='Helvetica-Bold',
                fontSize=7.5,
                textColor=self.GRAY_TEXT,
            ),
            'patient_value': ParagraphStyle(
                'patient_value',
                fontName='Helvetica',
                fontSize=7.5,
                textColor=self.DARK,
            ),
            'footer_text': ParagraphStyle(
                'footer_text',
                fontName='Helvetica',
                fontSize=7,
                textColor=self.GRAY_TEXT,
            ),
            'clinic_name': ParagraphStyle(
                'clinic_name',
                fontName='Helvetica-Bold',
                fontSize=12,
                textColor=colors.white,
            ),
            'clinic_address': ParagraphStyle(
                'clinic_address',
                fontName='Helvetica',
                fontSize=8,
                textColor=colors.HexColor('#CCEDE4'),
            ),
            'note_title': ParagraphStyle(
                'note_title',
                fontName='Helvetica-Bold',
                fontSize=9,
                textColor=colors.white,
                alignment=TA_RIGHT,
            ),
            'bullet_text': ParagraphStyle(
                'bullet_text',
                fontName='Helvetica',
                fontSize=8.5,
                textColor=self.DARK,
                leftIndent=4 * mm,
            ),
        }

    # ──────────────────────────────────────────────────────────────
    # Public entry point
    # ──────────────────────────────────────────────────────────────

    def generate_pdf(self, structured_data: dict) -> bytes:
        buffer = BytesIO()
        doc = SimpleDocTemplate(
            buffer,
            pagesize=LETTER,
            leftMargin=18 * mm,
            rightMargin=18 * mm,
            topMargin=15 * mm,
            bottomMargin=22 * mm,  # room for footer
            title="Nota de Evolución Clínica — Clin.IA",
        )
        story = []
        story.extend(self._build_header(structured_data))
        story.extend(self._build_patient_block(structured_data))
        story.append(HRFlowable(width="100%", thickness=1.5, color=self.TEAL, spaceAfter=4 * mm))
        story.extend(self._build_soap(structured_data))
        story.extend(self._build_signature_block(structured_data))

        doc.build(story, onFirstPage=self._draw_footer, onLaterPages=self._draw_footer)
        buffer.seek(0)
        return buffer.read()

    # ──────────────────────────────────────────────────────────────
    # Header block
    # ──────────────────────────────────────────────────────────────

    def _build_header(self, structured_data: dict) -> list:
        meta  = structured_data.get('metadata') or {}
        fecha = self._safe(
            meta.get('fecha_hora_consulta') or meta.get('fecha_consulta'),
            datetime.now().strftime('%Y-%m-%d %H:%M'),
        )

        page_width = LETTER[0] - 36 * mm

        date_style = ParagraphStyle(
            'header_date',
            fontName='Helvetica',
            fontSize=8,
            textColor=colors.white,
            alignment=TA_RIGHT,
        )

        left_cell  = [
            Paragraph('Consultorio Médico', self.styles['clinic_name']),
            Paragraph('Dirección del consultorio', self.styles['clinic_address']),
        ]
        right_cell = [
            Paragraph('NOTA DE EVOLUCIÓN CLÍNICA', self.styles['note_title']),
            Paragraph(html.escape(fecha), date_style),
        ]

        t = Table(
            [[left_cell, right_cell]],
            colWidths=[page_width * 0.60, page_width * 0.40],
        )
        t.setStyle(TableStyle([
            ('BACKGROUND',    (0, 0), (-1, -1), self.TEAL),
            ('VALIGN',        (0, 0), (-1, -1), 'MIDDLE'),
            ('ALIGN',         (1, 0), (1,  0),  'RIGHT'),
            ('TOPPADDING',    (0, 0), (-1, -1), 4 * mm),
            ('BOTTOMPADDING', (0, 0), (-1, -1), 4 * mm),
            ('LEFTPADDING',   (0, 0), (-1, -1), 4 * mm),
            ('RIGHTPADDING',  (0, 0), (-1, -1), 4 * mm),
        ]))
        return [t, Spacer(1, 3 * mm)]

    # ──────────────────────────────────────────────────────────────
    # Patient identification block
    # ──────────────────────────────────────────────────────────────

    def _build_patient_block(self, structured_data: dict) -> list:
        info = structured_data.get('informacion_paciente') or {}
        meta = structured_data.get('metadata') or {}

        fecha = self._safe(meta.get('fecha_hora_consulta') or meta.get('fecha_consulta'))

        def pid(label: str, value) -> Paragraph:
            val = html.escape(self._safe(value, '___________'))
            lbl = html.escape(label)
            return Paragraph(
                f'<font name="Helvetica-Bold" color="#5F5E5A">{lbl}</font>  '
                f'<font name="Helvetica" color="#2C2C2A">{val}</font>',
                self.styles['patient_value'],
            )

        left_paras = [
            pid('Nombre',        info.get('nombre_del_paciente')),
            pid('F. Nacimiento', info.get('fecha_de_nacimiento')),
            pid('Edad',          info.get('edad')),
            pid('Sexo',          info.get('genero')),
        ]

        right_paras = []
        curp = self._safe(info.get('curp'))
        if curp:
            right_paras.append(pid('CURP', curp))
        exp = self._safe(info.get('numero_expediente') or info.get('expediente'))
        if exp:
            right_paras.append(pid('Expediente', exp))
        right_paras.append(pid('Fecha consulta', fecha or ''))

        page_width = LETTER[0] - 36 * mm
        t = Table(
            [[left_paras, right_paras]],
            colWidths=[page_width * 0.55, page_width * 0.45],
        )
        t.setStyle(TableStyle([
            ('BACKGROUND',    (0, 0), (-1, -1), self.GRAY_LIGHT),
            ('BOX',           (0, 0), (-1, -1), 0.5, self.GRAY_MID),
            ('VALIGN',        (0, 0), (-1, -1), 'TOP'),
            ('TOPPADDING',    (0, 0), (-1, -1), 3 * mm),
            ('BOTTOMPADDING', (0, 0), (-1, -1), 3 * mm),
            ('LEFTPADDING',   (0, 0), (-1, -1), 3 * mm),
            ('RIGHTPADDING',  (0, 0), (-1, -1), 3 * mm),
        ]))
        return [t, Spacer(1, 3 * mm)]

    # ──────────────────────────────────────────────────────────────
    # SOAP dispatcher
    # ──────────────────────────────────────────────────────────────

    def _build_soap(self, structured_data: dict) -> list:
        story = []
        sections = [
            self._build_subjetivo(structured_data.get('subjetivo') or {}),
            self._build_objetivo(structured_data.get('objetivo') or {}),
            self._build_evaluacion(structured_data.get('evaluacion') or {}),
            self._build_plan(structured_data.get('plan') or {}),
        ]
        for items in sections:
            if not items:
                continue
            # Keep section header together with its first content item
            # to prevent orphaned headers at the bottom of a page
            keep_count = min(3, len(items))
            story.append(KeepTogether(items[:keep_count]))
            story.extend(items[keep_count:])
        return story

    # ──────────────────────────────────────────────────────────────
    # Section header helper
    # ──────────────────────────────────────────────────────────────

    def _section_header(self, letter: str, title: str, letter_color) -> Table:
        page_width = LETTER[0] - 36 * mm
        letter_style = ParagraphStyle(
            f'sl_{letter}',
            fontName='Helvetica-Bold',
            fontSize=16,
            textColor=letter_color,
            leading=16,
        )
        title_style = ParagraphStyle(
            f'st_{letter}',
            fontName='Helvetica-Bold',
            fontSize=9,
            textColor=self.DARK,
            leftIndent=2 * mm,
        )
        t = Table(
            [[Paragraph(letter, letter_style), Paragraph(title, title_style)]],
            colWidths=[10 * mm, page_width - 10 * mm],
        )
        t.setStyle(TableStyle([
            ('VALIGN',        (0, 0), (-1, -1), 'BOTTOM'),
            ('LINEBELOW',     (0, 0), (-1,  0), 0.5, self.GRAY_MID),
            ('BOTTOMPADDING', (0, 0), (-1, -1), 2 * mm),
            ('TOPPADDING',    (0, 0), (-1, -1), 1 * mm),
            ('LEFTPADDING',   (0, 0), (-1, -1), 0),
            ('RIGHTPADDING',  (0, 0), (-1, -1), 0),
        ]))
        return t

    # ──────────────────────────────────────────────────────────────
    # Paragraph helpers
    # ──────────────────────────────────────────────────────────────

    def _label_para(self, label: str, value: str) -> Paragraph:
        """Bold label inline with normal-weight value."""
        return Paragraph(
            f'<b>{html.escape(label)}</b> {html.escape(value)}',
            self.styles['normal'],
        )

    def _bullets(self, items: list) -> list:
        return [
            Paragraph(f'• {html.escape(self._safe(item))}', self.styles['bullet_text'])
            for item in items
            if self._safe(item)
        ]

    # ──────────────────────────────────────────────────────────────
    # S — Subjetivo
    # ──────────────────────────────────────────────────────────────

    def _build_subjetivo(self, subj: dict) -> list:
        rows = []

        motivo = self._safe(subj.get('motivo_de_consulta'))
        if motivo:
            rows.append(self._label_para('Motivo de consulta:', motivo))

        sintomas = subj.get('sintomas') or []
        if isinstance(sintomas, list) and sintomas:
            rows.append(Paragraph('<b>Síntomas:</b>', self.styles['bold_label']))
            rows.extend(self._bullets(sintomas))
        elif isinstance(sintomas, str) and sintomas.strip():
            rows.append(self._label_para('Síntomas:', sintomas))

        historia = self._safe(subj.get('historia_de_enfermedad_actual'))
        if historia:
            rows.append(self._label_para('Historia de la enfermedad:', historia))

        duracion = self._safe(subj.get('duracion_sintomas'))
        if duracion:
            rows.append(self._label_para('Duración de síntomas:', duracion))

        if not rows:
            return []
        return [self._section_header('S', 'SUBJETIVO', self.COLOR_S),
                Spacer(1, 2 * mm)] + rows + [Spacer(1, 3 * mm)]

    # ──────────────────────────────────────────────────────────────
    # O — Objetivo
    # ──────────────────────────────────────────────────────────────

    def _build_objetivo(self, obj_: dict) -> list:
        rows = []

        # Compact vitals table — only present fields
        vitales   = obj_.get('signos_vitales') or {}
        vital_map = [
            ('presion_arterial',        'PA'),
            ('frecuencia_cardiaca',     'FC'),
            ('temperatura',             'Temp'),
            ('frecuencia_respiratoria', 'FR'),
            ('saturacion_oxigeno',      'SpO₂'),
        ]
        present = [
            (label, self._safe(vitales.get(key)))
            for key, label in vital_map
            if self._safe(vitales.get(key))
        ]
        if present:
            cell_style = ParagraphStyle(
                'vital_cell', fontName='Helvetica', fontSize=8, textColor=self.DARK,
                alignment=TA_CENTER,
            )
            cells = [
                Paragraph(
                    f'<font color="#0F6E56"><b>{html.escape(lbl)}:</b></font> {html.escape(val)}',
                    cell_style,
                )
                for lbl, val in present
            ]
            page_width = LETTER[0] - 36 * mm
            col_w = page_width / len(cells)
            vt = Table([cells], colWidths=[col_w] * len(cells))
            vt.setStyle(TableStyle([
                ('BACKGROUND',    (0, 0), (-1, -1), self.GRAY_LIGHT),
                ('BOX',           (0, 0), (-1, -1), 0.5, self.GRAY_MID),
                ('INNERGRID',     (0, 0), (-1, -1), 0.3, self.GRAY_MID),
                ('ALIGN',         (0, 0), (-1, -1), 'CENTER'),
                ('TOPPADDING',    (0, 0), (-1, -1), 3),
                ('BOTTOMPADDING', (0, 0), (-1, -1), 3),
            ]))
            rows.append(vt)
            rows.append(Spacer(1, 2 * mm))

        examen = self._safe(obj_.get('examen_fisico'))
        if examen:
            rows.append(self._label_para('Examen físico:', examen))

        hallazgos = obj_.get('hallazgos')
        if isinstance(hallazgos, list) and hallazgos:
            rows.append(Paragraph('<b>Hallazgos:</b>', self.styles['bold_label']))
            rows.extend(self._bullets(hallazgos))
        elif isinstance(hallazgos, str) and hallazgos.strip():
            rows.append(self._label_para('Hallazgos:', hallazgos))

        if not rows:
            return []
        return [self._section_header('O', 'OBJETIVO', self.COLOR_O),
                Spacer(1, 2 * mm)] + rows + [Spacer(1, 3 * mm)]

    # ──────────────────────────────────────────────────────────────
    # A — Evaluación
    # ──────────────────────────────────────────────────────────────

    def _build_evaluacion(self, ev: dict) -> list:
        rows = []

        diag = self._safe(ev.get('diagnostico') or ev.get('diagnostico_principal'))
        if diag:
            # Diagnosis is the most important field — render both label and value bold
            rows.append(Paragraph(
                f'<b>Diagnóstico: {html.escape(diag)}</b>',
                self.styles['normal'],
            ))

        diag_add = ev.get('diagnosticos_adicionales') or []
        if isinstance(diag_add, list) and diag_add:
            rows.append(Paragraph('<b>Diagnósticos adicionales:</b>', self.styles['bold_label']))
            rows.extend(self._bullets(diag_add))

        impresion = self._safe(ev.get('impresion_clinica'))
        if impresion:
            rows.append(self._label_para('Impresión clínica:', impresion))

        pronostico = self._safe(ev.get('pronostico'))
        if pronostico:
            rows.append(self._label_para('Pronóstico:', pronostico))

        if not rows:
            return []
        return [self._section_header('A', 'EVALUACIÓN', self.COLOR_A),
                Spacer(1, 2 * mm)] + rows + [Spacer(1, 3 * mm)]

    # ──────────────────────────────────────────────────────────────
    # P — Plan
    # ──────────────────────────────────────────────────────────────

    def _build_plan(self, plan: dict) -> list:
        rows = []

        tratamiento = self._safe(plan.get('tratamiento'))
        if tratamiento:
            rows.append(self._label_para('Tratamiento:', tratamiento))

        meds = plan.get('medicamentos') or plan.get('medicamentos_prescritos') or []
        if isinstance(meds, list) and meds:
            rows.append(Paragraph('<b>Medicamentos:</b>', self.styles['bold_label']))
            rows.append(Spacer(1, 1 * mm))
            rows.append(self._meds_table(meds))
            rows.append(Spacer(1, 2 * mm))

        recomendaciones = plan.get('recomendaciones') or []
        if isinstance(recomendaciones, list) and recomendaciones:
            rows.append(Paragraph('<b>Recomendaciones:</b>', self.styles['bold_label']))
            rows.extend(self._bullets(recomendaciones))

        estudios = plan.get('estudios_solicitados') or []
        if isinstance(estudios, list) and estudios:
            rows.append(Paragraph('<b>Estudios solicitados:</b>', self.styles['bold_label']))
            rows.extend(self._bullets(estudios))
        elif isinstance(estudios, str) and estudios.strip():
            rows.append(self._label_para('Estudios solicitados:', estudios))

        seguimiento = self._safe(plan.get('seguimiento'))
        if seguimiento:
            rows.append(self._label_para('Seguimiento:', seguimiento))

        if not rows:
            return []
        return [self._section_header('P', 'PLAN', self.COLOR_P),
                Spacer(1, 2 * mm)] + rows + [Spacer(1, 3 * mm)]

    # ──────────────────────────────────────────────────────────────
    # Medications table
    # ──────────────────────────────────────────────────────────────

    def _meds_table(self, meds: list) -> Table:
        header_style = ParagraphStyle(
            'meds_h', fontName='Helvetica-Bold', fontSize=8, textColor=colors.white,
        )
        cell_style = ParagraphStyle(
            'meds_c', fontName='Helvetica', fontSize=8, textColor=self.DARK,
        )

        header_row = [Paragraph(h, header_style) for h in
                      ['Medicamento', 'Dosis', 'Frecuencia', 'Duración']]
        data = [header_row]

        for m in meds:
            if isinstance(m, dict):
                nombre     = self._safe(m.get('nombre'),     '—')
                dosis      = self._safe(m.get('dosis'),      '—')
                frecuencia = self._safe(m.get('frecuencia'), '—')
                duracion   = self._safe(m.get('duracion'),   '—')
            else:
                parts      = [p.strip() for p in str(m).split('-')]
                nombre     = parts[0] if len(parts) > 0 else '—'
                dosis      = parts[1] if len(parts) > 1 else '—'
                frecuencia = parts[2] if len(parts) > 2 else '—'
                duracion   = parts[3] if len(parts) > 3 else '—'
            data.append([
                Paragraph(html.escape(nombre),     cell_style),
                Paragraph(html.escape(dosis),      cell_style),
                Paragraph(html.escape(frecuencia), cell_style),
                Paragraph(html.escape(duracion),   cell_style),
            ])

        page_width = LETTER[0] - 36 * mm
        t = Table(
            data,
            colWidths=[page_width * 0.35, page_width * 0.20,
                       page_width * 0.25, page_width * 0.20],
        )
        style_cmds = [
            ('BACKGROUND',    (0, 0), (-1,  0), self.TEAL),
            ('TEXTCOLOR',     (0, 0), (-1,  0), colors.white),
            ('BOX',           (0, 0), (-1, -1), 0.5, self.GRAY_MID),
            ('INNERGRID',     (0, 0), (-1, -1), 0.3, self.GRAY_MID),
            ('VALIGN',        (0, 0), (-1, -1), 'TOP'),
            ('TOPPADDING',    (0, 0), (-1, -1), 2),
            ('BOTTOMPADDING', (0, 0), (-1, -1), 2),
            ('LEFTPADDING',   (0, 0), (-1, -1), 3),
            ('RIGHTPADDING',  (0, 0), (-1, -1), 3),
        ]
        for i in range(1, len(data)):
            if i % 2 == 0:
                style_cmds.append(('BACKGROUND', (0, i), (-1, i), self.GRAY_LIGHT))
        t.setStyle(TableStyle(style_cmds))
        return t

    # ──────────────────────────────────────────────────────────────
    # Signature / compliance block
    # ──────────────────────────────────────────────────────────────

    def _build_signature_block(self, structured_data: dict) -> list:
        meta   = structured_data.get('metadata') or {}
        medico = self._safe(meta.get('medico'))

        small = ParagraphStyle('sig_small', fontName='Helvetica',         fontSize=7, textColor=self.GRAY_TEXT)
        small_i = ParagraphStyle('sig_i',   fontName='Helvetica-Oblique', fontSize=7, textColor=self.GRAY_TEXT)
        right = ParagraphStyle('sig_r',     fontName='Helvetica',         fontSize=7, textColor=self.GRAY_TEXT, alignment=TA_RIGHT)

        left_content = [
            Paragraph('Generado por Clin.IA — clinianotes.com', small),
            Paragraph('Nota de Evolución conforme a NOM-004-SSA3-2012', small_i),
        ]
        right_content = [
            Paragraph('________________________________', right),
            Paragraph('Firma y sello del médico', right),
        ]
        if medico:
            right_content.append(Paragraph(html.escape(medico), right))

        page_width = LETTER[0] - 36 * mm
        t = Table(
            [[left_content, right_content]],
            colWidths=[page_width * 0.55, page_width * 0.45],
        )
        t.setStyle(TableStyle([
            ('VALIGN',        (0, 0), (-1, -1), 'BOTTOM'),
            ('LEFTPADDING',   (0, 0), (-1, -1), 0),
            ('RIGHTPADDING',  (0, 0), (-1, -1), 0),
            ('TOPPADDING',    (0, 0), (-1, -1), 0),
            ('BOTTOMPADDING', (0, 0), (-1, -1), 0),
        ]))
        return [Spacer(1, 8 * mm), t]

    # ──────────────────────────────────────────────────────────────
    # Per-page footer (drawn on canvas, not in the story)
    # ──────────────────────────────────────────────────────────────

    def _draw_footer(self, canvas, doc):
        canvas.saveState()
        # Teal top border
        canvas.setStrokeColor(self.TEAL)
        canvas.setLineWidth(1)
        canvas.line(18 * mm, 15 * mm, LETTER[0] - 18 * mm, 15 * mm)
        # Branding (left)
        canvas.setFont('Helvetica', 7)
        canvas.setFillColor(self.GRAY_TEXT)
        canvas.drawString(18 * mm, 10 * mm, 'Generado por Clin.IA · clinianotes.com')
        # Page number (right)
        canvas.drawRightString(LETTER[0] - 18 * mm, 10 * mm, f'Página {doc.page}')
        canvas.restoreState()

    # ──────────────────────────────────────────────────────────────
    # Helper
    # ──────────────────────────────────────────────────────────────

    def _safe(self, value, fallback: str = '') -> str:
        """Return string value safely, using fallback if None or empty."""
        if value is None:
            return fallback
        s = str(value).strip()
        return s if s else fallback
