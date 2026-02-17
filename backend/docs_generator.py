# backend/docs_generator.py
import os
import json
from typing import Dict, List
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

# ID of your template (Must be a native Google Doc)
TEMPLATE_ID = '1XVXnvw6JiAg1If3BUJtaAIrLdcpujAlovHVPyuUny1A'

class GoogleDocsGenerator:
    """Generates Medical Notes with strict bolding control and range validation."""
    
    def __init__(self, owner_email: str = None):
        self.owner_email = owner_email or os.getenv('GOOGLE_DOCS_OWNER_EMAIL')
        SCOPES = ['https://www.googleapis.com/auth/documents', 'https://www.googleapis.com/auth/drive']
        
        creds = None
        if os.path.exists('token.json'):
            creds = Credentials.from_authorized_user_file('token.json', SCOPES)
            
        if not creds or not creds.valid:
            if creds and creds.expired and creds.refresh_token:
                creds.refresh(Request())
            else:
                flow = InstalledAppFlow.from_client_secrets_file('client_secrets.json', SCOPES)
                creds = flow.run_local_server(port=0)
            with open('token.json', 'w') as token:
                token.write(creds.to_json())

        self.docs_service = build('docs', 'v1', credentials=creds)
        self.drive_service = build('drive', 'v3', credentials=creds)
        print("[GoogleDocs] Service initialized.")

    def create_medical_note(self, structured_data: Dict, title: str = None) -> Dict:
        """Copies the template and applies the SOAP summary with selective bolding."""
        if title is None:
            patient_name = structured_data.get('informacion_paciente', {}).get('nombre_del_paciente', 'Paciente')
            fecha = structured_data.get('metadata', {}).get('fecha_consulta', 'Sin fecha')
            title = f"Nota Clínica - {patient_name} - {fecha}"
        
        try:
            # 1. Copy the template
            copy_body = {'name': title}
            copied_file = self.drive_service.files().copy(fileId=TEMPLATE_ID, body=copy_body).execute()
            doc_id = copied_file.get('id')
            
            # 2. Get the index to start writing after the header
            document = self.docs_service.documents().get(documentId=doc_id).execute()
            start_index = document.get('body').get('content')[-1].get('endIndex') - 1
            
            # 3. Build the requests using the helper method
            requests = self._build_document_requests(structured_data, start_index)
            
            if requests:
                self.docs_service.documents().batchUpdate(
                    documentId=doc_id,
                    body={'requests': requests}
                ).execute()
            
            link = f"https://docs.google.com/document/d/{doc_id}/edit"
            print(f"[GoogleDocs] Document created successfully: {link}")
            return {'document_id': doc_id, 'link': link, 'title': title}
            
        except HttpError as error:
            print(f"[GoogleDocs] API Error: {error}")
            raise

    def _build_document_requests(self, data: Dict, start_index: int = 1) -> List[Dict]:
        """Constructs document requests ensuring specific labels are bold and content is normal."""
        requests = []
        index = start_index 
        
        info = data.get('informacion_paciente', {})
        meta = data.get('metadata', {})

        # --- Formatting Helper ---
        def add_text(text: str, bold: bool = False, newline: bool = True):
            nonlocal index, requests
            text_str = str(text) if text is not None else ""
            content = text_str + ('\n' if newline else '')
            
            if not content:
                return

            # Insert the raw text
            requests.append({'insertText': {'location': {'index': index}, 'text': content}})
            
            # CRITICAL: Only apply style if text length > 0 to prevent HttpError 400
            if len(text_str) > 0:
                requests.append({
                    'updateTextStyle': {
                        'range': {'startIndex': index, 'endIndex': index + len(text_str)},
                        'textStyle': {'bold': bold},
                        'fields': 'bold'
                    }
                })
            
            index += len(content)

        # --- 1. PATIENT SUB-HEADER (Normal weight) ---
        add_text("\n", bold=False)
        add_text(f"FECHA: {meta.get('fecha_consulta', '__________')}\t\tEDAD: {info.get('edad', '____')}", bold=False)
        add_text(f"NOMBRE: {info.get('nombre_del_paciente', '____________________')}\t\tFECHA DE NACIMIENTO: {info.get('fecha_de_nacimiento', '__________')}", bold=False)
        add_text(f"ESTADO CIVIL: {info.get('estado_civil', '__________')}\t\tSEXO: {info.get('genero', '__________')}", bold=False)
        add_text(f"DOMICILIO: {info.get('domicilio', '________________________________________')}", bold=False)
        add_text(f"TEL: {info.get('telefono', '__________')}\t\tCELULAR: {info.get('celular', '__________')}", bold=False)

        add_text("-" * 80, bold=False)
        add_text("RESUMEN DE CONSULTA (SOAP)", bold=False)
        add_text("-" * 80 + "\n", bold=False)

        # --- 2. SUBJETIVO (S) ---
        add_text('SUBJETIVO (S)', bold=True)
        subj = data.get('subjetivo', {})
        
        add_text("Motivo de Consulta: ", bold=True, newline=False)
        add_text(subj.get('motivo_de_consulta', ''), bold=False)
        
        add_text("Síntomas: ", bold=True)
        sintomas = subj.get('sintomas', [])
        if isinstance(sintomas, list):
            for s in sintomas: add_text(f" • {s}", bold=False)
        
        add_text("Historia de la Enfermedad: ", bold=True)
        add_text(subj.get('historia_de_enfermedad_actual', ''), bold=False)
        
        if subj.get('duracion'):
            add_text(f"Duración: {subj['duracion']}", bold=False)
        
        add_text("")

        # --- 3. OBJETIVO (O) ---
        add_text('OBJETIVO (O)', bold=True)
        obj = data.get('objetivo', {})
        
        add_text("Signos Vitales: ", bold=True, newline=False)
        v = obj.get('signos_vitales', {})
        vitales_str = ", ".join([f"{k.replace('_',' ').title()}: {val}" for k, val in v.items()])
        add_text(vitales_str if vitales_str else "No registrados", bold=False)

        add_text("Examen Físico: ", bold=True)
        add_text(obj.get('examen_fisico', ''), bold=False)
        
        add_text("Hallazgos: ", bold=True)
        add_text(obj.get('hallazgos', ''), bold=False)
        
        add_text("")

        # --- 4. EVALUACIÓN (A) -- Assessment ---
        add_text('EVALUACIÓN (A) -- Assessment', bold=True)
        ev = data.get('evaluacion', {})
        
        add_text("Diagnóstico: ", bold=True, newline=False)
        add_text(ev.get('diagnostico', ev.get('diagnostico_principal', '')), bold=False)
        
        add_text("Impresión Clínica: ", bold=True, newline=False)
        add_text(ev.get('impresion_clinica', ''), bold=False)
        
        add_text("")

        # --- 5. PLAN (P) ---
        add_text('PLAN (P)', bold=True)
        plan = data.get('plan', {})
        
        add_text("Medicamentos Prescritos:", bold=True)
        meds = plan.get('medicamentos', plan.get('medicamentos_prescritos', []))
        if isinstance(meds, list):
            for m in meds:
                if isinstance(m, dict):
                    content = f" • {m.get('nombre')} - {m.get('dosis')} ({m.get('frecuencia')})"
                    add_text(content, bold=False)
                else:
                    add_text(f" • {m}", bold=False)
        
        add_text("Recomendaciones:", bold=True)
        for r in plan.get('recomendaciones', []):
            add_text(f" • {r}", bold=False)
        
        add_text("Estudios Solicitados:", bold=True)
        estudios = plan.get('estudios_solicitados', [])
        for e in (estudios if isinstance(estudios, list) else [estudios]):
            add_text(f" • {e}", bold=False)
            
        add_text("Seguimiento:", bold=True)
        add_text(plan.get('seguimiento', ''), bold=False)

        return requests
