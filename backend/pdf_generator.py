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
    HRFlowable, KeepTogether, PageBreak,
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

    def generate_pdf(self, structured_data: dict, session_id: str = '') -> bytes:
        self._session_id = session_id
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
        story.extend(self._build_consent_page(structured_data))
        story.append(PageBreak())
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
        exp = self._safe(info.get('numero_expediente') or info.get('expediente'))
        if exp:
            right_paras.append(pid('Expediente', exp))
        curp = self._safe(info.get('curp'))
        if curp:
            right_paras.append(pid('CURP', curp))
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
            ('saturacion_oxigeno',      'SpO2'),
            ('peso',                    'Peso'),
            ('talla',                   'Talla'),
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

        habitus = self._safe(obj_.get('habitus_exterior'))
        if habitus:
            rows.append(self._label_para('Habitus exterior:', habitus))
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

        cie11_code  = self._safe(ev.get('codigo_cie11'))
        cie11_title = self._safe(ev.get('titulo_cie11'))
        if cie11_code:
            cie11_value = cie11_code
            if cie11_title:
                cie11_value += f'  —  {cie11_title}'
            rows.append(self._label_para('Código CIE-11:', cie11_value))

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

    def _build_consent_page(self, structured_data: dict) -> list:
        """
        Page 1: Carta de Consentimiento Informado.
        Satisfies NOM-004 section 10.1 and LFPDPPP informed consent requirement.
        """
        info   = structured_data.get('informacion_paciente') or {}
        meta   = structured_data.get('metadata') or {}
        fecha  = self._safe(
            meta.get('fecha_hora_consulta') or meta.get('fecha_consulta'),
            datetime.now().strftime('%Y-%m-%d %H:%M')
        )
        paciente      = self._safe(info.get('nombre_del_paciente'), '___________________________')
        session_label = getattr(self, '_session_id', '')
        page_width    = LETTER[0] - 36 * mm

        title_style = ParagraphStyle('consent_title', fontName='Helvetica-Bold', fontSize=14,
                                      textColor=self.TEAL, alignment=TA_CENTER, spaceAfter=6 * mm)
        subtitle_style = ParagraphStyle('consent_subtitle', fontName='Helvetica-Bold', fontSize=10,
                                         textColor=colors.black, spaceAfter=3 * mm, spaceBefore=5 * mm)
        body_style = ParagraphStyle('consent_body', fontName='Helvetica', fontSize=9,
                                     textColor=colors.black, leading=14, spaceAfter=3 * mm)
        small_style = ParagraphStyle('consent_small', fontName='Helvetica-Oblique', fontSize=7,
                                      textColor=self.GRAY_TEXT, alignment=TA_CENTER, spaceBefore=4 * mm)
        sig_label = ParagraphStyle('csl', fontName='Helvetica',      fontSize=8,
                                    textColor=colors.black, alignment=TA_CENTER)
        sig_name  = ParagraphStyle('csn', fontName='Helvetica-Bold', fontSize=8,
                                    textColor=colors.black, alignment=TA_CENTER)
        sig_sub   = ParagraphStyle('css', fontName='Helvetica',      fontSize=7,
                                    textColor=self.GRAY_TEXT, alignment=TA_CENTER)

        elems = []

        # Header banner
        date_style_w = ParagraphStyle('cdate', fontName='Helvetica', fontSize=8,
                                       textColor=colors.white, alignment=TA_RIGHT)
        left_cell  = [Paragraph('Consultorio Médico', self.styles['clinic_name']),
                      Paragraph('Dirección del consultorio', self.styles['clinic_address'])]
        right_cell = [Paragraph('CARTA DE CONSENTIMIENTO INFORMADO', self.styles['note_title']),
                      Paragraph(html.escape(fecha), date_style_w)]
        banner = Table([[left_cell, right_cell]],
                       colWidths=[page_width * 0.60, page_width * 0.40])
        banner.setStyle(TableStyle([
            ('BACKGROUND',    (0, 0), (-1, -1), self.TEAL),
            ('VALIGN',        (0, 0), (-1, -1), 'MIDDLE'),
            ('ALIGN',         (1, 0), (1,  0),  'RIGHT'),
            ('TOPPADDING',    (0, 0), (-1, -1), 4 * mm),
            ('BOTTOMPADDING', (0, 0), (-1, -1), 4 * mm),
            ('LEFTPADDING',   (0, 0), (-1, -1), 4 * mm),
            ('RIGHTPADDING',  (0, 0), (-1, -1), 4 * mm),
        ]))
        elems.append(banner)
        elems.append(Spacer(1, 6 * mm))

        # Patient identification
        elems.append(Paragraph('Datos del Paciente', subtitle_style))
        elems.append(Paragraph(
            f'Nombre: <b>{html.escape(paciente)}</b> &nbsp;&nbsp;&nbsp; Fecha: <b>{html.escape(fecha)}</b>',
            body_style
        ))
        curp = self._safe(info.get('curp'))
        if curp:
            elems.append(Paragraph(f'CURP: <b>{html.escape(curp)}</b>', body_style))
        elems.append(Spacer(1, 4 * mm))

        # Purpose
        elems.append(Paragraph('Propósito de la Consulta', subtitle_style))
        elems.append(Paragraph(
            'El paciente acude a consulta médica para evaluación, diagnóstico y/o tratamiento por parte del '
            'médico tratante. La presente carta documenta el consentimiento informado del paciente conforme a '
            'lo establecido en la NOM-004-SSA3-2012 (sección 10.1) y la Ley Federal de Protección de Datos '
            'Personales en Posesión de los Particulares (LFPDPPP).', body_style
        ))

        # AI and data use
        elems.append(Paragraph('Uso de Inteligencia Artificial y Datos Personales', subtitle_style))
        elems.append(Paragraph(
            'En esta consulta se utiliza <b>Clin.IA</b>, un sistema de transcripción e inteligencia artificial '
            'que genera automáticamente la nota clínica a partir del audio de la consulta. El paciente ha sido '
            'informado de lo anterior y ha expresado su consentimiento. El audio de la consulta es '
            '<b>eliminado automáticamente</b> tras la transcripción y no es almacenado. La transcripción de '
            'texto es eliminada tras la confirmación de la nota clínica. Los datos clínicos estructurados y el '
            'PDF de la nota son conservados durante el período mínimo de <b>5 años</b> conforme a '
            'NOM-004-SSA3-2012 (10 años para pacientes menores de edad hasta 18+5 años).', body_style
        ))

        # ARCO rights
        elems.append(Paragraph('Derechos ARCO', subtitle_style))
        elems.append(Paragraph(
            'El paciente tiene derecho a <b>Acceder</b> a sus datos personales, solicitar su '
            '<b>Rectificación</b> mediante adéndum a la nota clínica, solicitar su <b>Cancelación</b> '
            '(bloqueo conforme al período de retención legal), u <b>Oponerse</b> al uso secundario de sus '
            f'datos. Para ejercer estos derechos, contactar a: <b>admin@clinianotes.com</b> indicando nombre '
            f'completo, fecha de consulta e ID de nota: <b>{html.escape(session_label)}</b>. '
            'El plazo de respuesta es de 20 días hábiles conforme a la LFPDPPP.', body_style
        ))

        # Consent declaration
        elems.append(Paragraph('Declaración de Consentimiento', subtitle_style))
        elems.append(Paragraph(
            'El paciente declara haber leído y comprendido la presente carta, haber recibido explicación '
            'verbal por parte del médico tratante, y otorgar su consentimiento libre, voluntario e informado '
            'para la consulta médica y el procesamiento de sus datos personales con los fines descritos.',
            body_style
        ))
        elems.append(Spacer(1, 10 * mm))

        # Signature block — Patient + Doctor
        medico   = self._safe(meta.get('medico'))
        sig_line = '________________________________'
        left_sig = [
            Paragraph(sig_line, sig_label),
            Paragraph('Firma del Paciente', sig_name),
            Paragraph(html.escape(paciente), sig_sub),
            Paragraph('Huella digital (si aplica)', sig_sub),
        ]
        right_sig = [
            Paragraph(sig_line, sig_label),
            Paragraph('Firma y Sello del Médico', sig_name),
            Paragraph(html.escape(medico) if medico else 'Médico Tratante', sig_sub),
        ]
        sig_table = Table([[left_sig, right_sig]],
                          colWidths=[page_width * 0.50, page_width * 0.50])
        sig_table.setStyle(TableStyle([
            ('VALIGN',        (0, 0), (-1, -1), 'TOP'),
            ('ALIGN',         (0, 0), (-1, -1), 'CENTER'),
            ('LEFTPADDING',   (0, 0), (-1, -1), 8 * mm),
            ('RIGHTPADDING',  (0, 0), (-1, -1), 8 * mm),
            ('TOPPADDING',    (0, 0), (-1, -1), 0),
            ('BOTTOMPADDING', (0, 0), (-1, -1), 0),
        ]))
        elems.append(sig_table)
        elems.append(Spacer(1, 6 * mm))

        # Beta watermark
        elems.append(Paragraph(
            'SOLO PARA PRUEBAS — Este documento es generado por una versión Beta de Clin.IA y no constituye '
            'un expediente clínico oficial hasta su validación por el médico tratante.', small_style
        ))
        if session_label:
            elems.append(Paragraph(f'ID de nota: {session_label}', small_style))

        return elems

    def _build_signature_block(self, structured_data: dict) -> list:
        meta     = structured_data.get('metadata') or {}
        info     = structured_data.get('informacion_paciente') or {}
        medico   = self._safe(meta.get('medico'))
        paciente = self._safe(info.get('nombre_del_paciente'))

        small   = ParagraphStyle('sb_s',  fontName='Helvetica',         fontSize=7,
                                  textColor=self.GRAY_TEXT, alignment=TA_CENTER)
        small_b = ParagraphStyle('sb_b',  fontName='Helvetica-Bold',    fontSize=7,
                                  textColor=colors.black, alignment=TA_CENTER)
        small_i = ParagraphStyle('sb_i',  fontName='Helvetica-Oblique', fontSize=7,
                                  textColor=self.GRAY_TEXT)

        sig_line = '________________________________'

        left_content = [
            Paragraph('Generado por Clin.IA — clinianotes.com', small_i),
            Paragraph('Nota de Evolución conforme a NOM-004-SSA3-2012', small_i),
        ]
        mid_content = [
            Paragraph(sig_line, small),
            Paragraph('Firma del Paciente', small_b),
            Paragraph(html.escape(paciente) if paciente else '', small),
        ]
        right_content = [
            Paragraph(sig_line, small),
            Paragraph('Firma y Sello del Médico', small_b),
            Paragraph(html.escape(medico) if medico else 'Médico Tratante', small),
        ]

        page_width = LETTER[0] - 36 * mm
        t = Table(
            [[left_content, mid_content, right_content]],
            colWidths=[page_width * 0.35, page_width * 0.32, page_width * 0.33]
        )
        t.setStyle(TableStyle([
            ('VALIGN',        (0, 0), (-1, -1), 'BOTTOM'),
            ('ALIGN',         (0, 0), (-1, -1), 'CENTER'),
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
        id_label = f'ID de nota: {self._session_id}  ·  ' if getattr(self, '_session_id', '') else ''
        canvas.drawString(18 * mm, 10 * mm, f'{id_label}Generado por Clin.IA · clinianotes.com')
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
