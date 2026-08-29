# backend/llm_processor.py
from google import genai
from google.genai import types # For configuration
from config import Config
from logger import logger
from icd_service import lookup_cie11
import json
import time
from typing import Dict, Optional


class LLMProcessingError(Exception):
    """Raised when Gemini extraction exhausts all retries without producing
    a usable result. Callers must treat this as a hard failure and must
    never persist whatever partial/error state exists (Stage M4 fix #24) —
    previously extract_structured_data returned an error-shaped dict here
    instead of raising, so it silently flowed through as if it were a real
    structured_data result."""
    pass


class LLMProcessor:
    """Processes transcripts using Google Gemini for structured extraction"""
    
    def __init__(self):
        """Initialize Gemini API"""
        self.client = genai.Client(api_key=Config.GEMINI_API_KEY)
        self.model_id = 'gemini-2.5-flash'

    def create_extraction_prompt(self, transcript: str, utterances: list = None) -> str:
        """
        Create detailed prompt for Gemini to extract medical information

        Args:
            transcript: Raw Spanish transcript from AssemblyAI
            utterances: Optional speaker-labeled utterances from AssemblyAI

        Returns:
            Formatted prompt for LLM
        """
        if utterances:
            # Neutral, anonymous speaker labels only (Hablante A/B/...) —
            # the raw AssemblyAI diarization label, no clinical-role guess.
            # Gemini determines the actual role itself; see speaker_instruction
            # below (Fix #30 — replaces the old word-count heuristic, which
            # assumed "most words = doctor" and was frequently backwards,
            # since patients often talk more than the doctor).
            transcript_content = "\n".join(
                f"[Hablante {u['speaker']}]: {u['text']}" for u in utterances
            )
            speaker_instruction = (
                "\n6. La transcripción está etiquetada por interlocutor anónimo "
                "(Hablante A, Hablante B, …), sin asignar rol clínico alguno. "
                "PRIMERO determina el rol clínico de cada interlocutor a partir "
                "del diálogo: el médico hace las preguntas clínicas, conduce la "
                "exploración, y da el diagnóstico, el pronóstico y el plan/"
                "indicaciones; el paciente describe síntomas, antecedentes y "
                "responde a las preguntas; puede haber un familiar o enfermera. "
                "No asumas que quien más habla es el médico — el paciente con "
                "frecuencia habla más. DESPUÉS extrae el SOAP según esa "
                "determinación: Subjetivo principalmente del paciente (y "
                "familiar, si está presente); Objetivo de los hallazgos de "
                "exploración del médico; Evaluación y Plan del médico. "
                "Reporta tu determinación en el campo \"roles_detectados\" del "
                "JSON de salida (ver esquema abajo) — una entrada por cada "
                "\"Hablante X\" que aparezca en la transcripción."
            )
            # Only meaningful when there are speaker-labeled utterances —
            # omitted from the schema entirely in the flat-transcript
            # fallback below, where there's no per-speaker role to report.
            roles_detectados_field = (
                '  "roles_detectados": {\n'
                '    "Hablante A": "médico|paciente|familiar|enfermera",\n'
                '    "Hablante B": "médico|paciente|familiar|enfermera"\n'
                '  },\n'
            )
        else:
            transcript_content = transcript
            speaker_instruction = ""
            roles_detectados_field = ""

        prompt = f"""Eres un asistente médico especializado en crear notas clínicas siguiendo el formato SOAP (Subjetivo, Objetivo, Evaluación, Plan).

Tu tarea es analizar la siguiente transcripción de una consulta médica en español y extraer toda la información relevante en un formato JSON estructurado.

INSTRUCCIONES CRÍTICAS:
1. Debes extraer ÚNICAMENTE información que esté explícitamente mencionada en la transcripción
2. Si cierta información no está presente, omite ese campo (no inventes datos)
3. En las secciones Subjetivo y Objetivo, mantén los términos como aparecen en la transcripción (incluidas las palabras del paciente). En las secciones Evaluación y Plan, aplica el lenguaje técnico-médico según la sección «LENGUAJE TÉCNICO-MÉDICO» de abajo. En todos los casos, asegura la congruencia de género entre los artículos (definidos e indefinidos) y el sustantivo que les sigue
4. Organiza la información según el formato SOAP
5. Identifica y separa la información del paciente, síntomas, hallazgos, diagnóstico y plan de tratamiento
6. Para peso y talla, escucha frases como 'el paciente pesa', 'la talla es', 'pesa X kilos', 'mide X'. Para habitus exterior, escucha frases como 'paciente consciente y orientado', 'bien orientado en tiempo y espacio', 'estado nutricional adecuado', 'paciente masculino/femenino, adulto'
7. curp: NUNCA autogenerar ni inferir. Solo incluir si el médico lo menciona explícitamente durante la consulta.{speaker_instruction}

LENGUAJE TÉCNICO-MÉDICO (NOM-004-SSA3-2012, §5.11) — aplica ÚNICAMENTE a «evaluacion» y «plan»:
- Redacta los campos de «evaluacion» (diagnostico, diagnosticos_adicionales, impresion_clinica,
  pronostico) y de «plan» (tratamiento, medicamentos, recomendaciones, estudios_solicitados,
  seguimiento) en lenguaje técnico-médico formal.
- Sustituye términos coloquiales por su equivalente técnico. Sustituciones canónicas (lista ampliable):
    • panza / barriga / tripa → abdomen (o «cavidad abdominal» según contexto)
    • vahído → síncope

* dolor de cabeza → cefalea
* fiebre → pirexia/hipertermia

- Sin abreviaturas: desarróllalas a su forma completa (p. ej. «c/8h» → «cada 8 horas»;
  «VO» → «vía oral»). Conserva las unidades estándar (mg, mL, mmHg, °C) y el código CIE-11.

REGLA DE SEGURIDAD (obligatoria): esta normalización cambia ÚNICAMENTE el registro lingüístico,
NUNCA el contenido clínico. Está PROHIBIDO añadir diagnósticos, hallazgos, localización anatómica,
lateralidad, severidad, temporalidad o cualquier detalle no explícito en la transcripción, o inferir
más de lo que dijo el médico. Si no existe un equivalente técnico claro y unívoco, conserva el término
original. Preserva el significado clínico exactamente. Ante la duda, no cambies nada.

Las secciones «subjetivo» y «objetivo» NO se modifican.

TRANSCRIPCIÓN:
{transcript_content}

FORMATO DE SALIDA:
Debes responder ÚNICAMENTE con un objeto JSON válido que siga este esquema:

{{
{roles_detectados_field}  "informacion_paciente": {{
    "nombre_del_paciente": "string (si se menciona)",
    "fecha_de_nacimiento": "string en formato DD/MM/YYYY (si se menciona)",
    "edad": "string (si se menciona)",
    "genero": "string (si se menciona)",
    "numero_expediente": "string - número de expediente si se menciona explícitamente; omitir si no se menciona",
    "curp": "string - CURP del paciente si se menciona explícitamente en la consulta; omitir si no se menciona. Nunca autogenerar."
  }},
  "subjetivo": {{
    "motivo_de_consulta": "string - razón principal de la visita",
    "sintomas": ["lista de síntomas mencionados por el paciente"],
    "historia_de_enfermedad_actual": "string - descripción de cómo empezó y evolucionó",
    "duracion_sintomas": "string - hace cuánto empezaron los síntomas"
  }},
  "objetivo": {{
    "signos_vitales": {{
      "presion_arterial": "string (si se menciona)",
      "frecuencia_cardiaca": "string (si se menciona)",
      "temperatura": "string (si se menciona)",
      "frecuencia_respiratoria": "string (si se menciona)",
      "saturacion_oxigeno": "string (si se menciona)",
      "peso": "string en kg (si se menciona)",
      "talla": "string en cm o metros (si se menciona)"
    }},
    "habitus_exterior": "string - descripción general del paciente: edad aparente, estado de consciencia, orientación, estado nutricional (si se menciona)",
    "examen_fisico": "string - hallazgos del examen físico",
    "hallazgos": ["lista de hallazgos objetivos"]
  }},
  "evaluacion": {{
    "diagnostico": "string - diagnóstico principal",
    "diagnosticos_adicionales": ["otros diagnósticos o diagnósticos diferenciales"],
    "impresion_clinica": "string - impresión general del médico",
    "pronostico": "string - pronóstico esperado por el médico (favorable, reservado, o descripción)"
  }},
  "plan": {{
    "tratamiento": "string - plan de tratamiento general",
    "medicamentos": [
      {{
        "nombre": "nombre del medicamento",
        "dosis": "dosis prescrita",
        "frecuencia": "con qué frecuencia tomar",
        "duracion": "por cuánto tiempo"
      }}
    ],
    "recomendaciones": ["lista de recomendaciones e instrucciones"],
    "estudios_solicitados": ["laboratorios, imágenes u otros estudios solicitados"],
    "seguimiento": "string - instrucciones de seguimiento"
  }},
  "metadata": {{
    "fecha_consulta": "string (si se menciona)",
    "medico": "string (si se menciona)",
    "duracion_consulta": "string (si se puede determinar)"
  }},
  "actualizacion_antecedentes": {{
    "detectado": "true o false - detecta si el paciente menciona antecedentes hereditarios, familiares o personales nuevos no capturados previamente (por ejemplo, un diagnóstico reciente de un familiar)",
    "contenido": "string - descripción del antecedente nuevo mencionado (omitir si detectado es false)"
  }}
}}

REGLAS IMPORTANTES:
- IMPORTANTE: Si necesitas citar al paciente o usar comillas dentro de un texto, usa SOLO comillas simples ('ejemplo'). NUNCA uses comillas dobles dentro de un valor de texto, ya que esto destruye el formato JSON.
- Responde SOLO con el JSON, sin texto adicional antes o después
- No incluyas ```json ni ningún otro formato de código
- Si un campo no tiene información, omítelo del JSON
- Asegúrate de que el JSON sea válido y pueda ser parseado
- Usa comillas dobles estrictamente para las llaves y estructura general del JSON.
- Mantén los acentos y caracteres especiales del español

Ahora extrae la información de la transcripción y genera el JSON:"""
        
        return prompt
    
    def extract_structured_data(self, transcript: str, utterances: list = None, max_retries: int = 3) -> Dict:
        """
        Extract structured medical data from transcript using Gemini
        """
        logger.info(f"LLM: Processing with {self.model_id}...")
        if utterances:
            logger.info(f"LLM: Using {len(utterances)} speaker-labeled utterances")
        prompt = self.create_extraction_prompt(transcript, utterances)
        
        last_error = "Unknown error"
        
        for attempt in range(max_retries):
            try:
                response = self.client.models.generate_content(
                    model=self.model_id,
                    contents=prompt,
                    config=types.GenerateContentConfig(
                        temperature=0.1,
                        response_mime_type='application/json',
                    )
                )
                
                if not response.text:
                    raise ValueError("Empty response from Google API")
                    
                logger.debug(f"LLM: Raw response received. Length: {len(response.text)}")

                # Clean the text JUST IN CASE Gemini added markdown backticks
                cleaned_text = self._clean_json_response(response.text)

                structured_data = json.loads(cleaned_text)
                logger.info("LLM: Extraction successful.")

                # CIE-11 lookup — inject code from WHO API based on Gemini's extracted diagnosis
                try:
                    diagnostico = structured_data.get('evaluacion', {}).get('diagnostico', '')
                    if diagnostico:
                        cie_result = lookup_cie11(diagnostico)
                        if cie_result:
                            structured_data.setdefault('evaluacion', {})
                            structured_data['evaluacion']['codigo_cie11'] = cie_result['code']
                            structured_data['evaluacion']['titulo_cie11'] = cie_result['title']
                            # No code/title in the log — a patient's diagnosis
                            # code is PHI same as the free-text diagnosis it
                            # came from (Stage H1 fix #11, caught via a live
                            # log review that still had one in it).
                            logger.info("ICD: CIE-11 code injected into structured_data")
                except Exception as e:
                    logger.warning(f"ICD: Could not inject CIE-11 code (non-fatal): {e}")

                return structured_data

            except Exception as e:
                last_error = str(e)
                error_msg = str(e).lower()
                logger.error(f"LLM: Attempt {attempt + 1} failed: {last_error}")

                # Catch rate limits and server timeouts
                if any(x in error_msg for x in ["429", "504", "deadline", "cancelled", "quota", "503"]):
                    if attempt < max_retries - 1:
                        wait_time = 5 * (attempt + 1)
                        logger.warning(f"LLM: Server busy. Retrying in {wait_time}s...")
                        time.sleep(wait_time)
                        continue

                # Catch JSON formatting errors and tell the prompt to be more careful
                if "json" in error_msg or "expecting value" in error_msg:
                    if attempt < max_retries - 1:
                         prompt += "\n\nNOTA: Asegúrate de devolver SOLO JSON válido, sin texto adicional."
                         logger.warning("LLM: JSON format error, retrying...")
                         time.sleep(2)
                         continue

                # If it's a completely different error (like a 404), break immediately
                break

        # All retries exhausted — fail loudly (Stage M4 fix #24). This used
        # to return an error-shaped dict, which the caller had no contract
        # to check for, so it silently became "structured_data" for a
        # pending_review session. Raising forces every caller to handle
        # failure explicitly instead of trusting whatever came back.
        logger.error(f"LLM: All attempts failed. Final error: {last_error}")
        raise LLMProcessingError(
            f"Extraction failed after {max_retries} attempts: {last_error}"
        )

    def _clean_json_response(self, response: str) -> str:
        """
        Clean LLM response to extract pure JSON
        """
        # Remove any leading/trailing whitespace
        response = response.strip()
        
        # Remove any text before first { or after last }
        first_brace = response.find('{')
        last_brace = response.rfind('}')
        
        if first_brace != -1 and last_brace != -1:
            response = response[first_brace:last_brace + 1]
        
        return response
    
    def validate_against_schema(self, data: Dict) -> tuple[bool, Optional[str]]:
        """
        Validate extracted data against JSON schema
        
        Args:
            data: Extracted data dictionary
        
        Returns:
            Tuple of (is_valid, error_message)
        """
        # Basic validation - check required fields
        if "informacion_paciente" not in data:
            return False, "Missing required field: informacion_paciente"
        
        # Check that at least some meaningful data was extracted
        has_content = False
        for section in ["subjetivo", "objetivo", "evaluacion", "plan"]:
            if section in data and data[section]:
                has_content = True
                break
        
        if not has_content:
            return False, "No meaningful medical data extracted"
        
        return True, None



# Testing
if __name__ == "__main__":
    Config.validate()
    
    # Test with sample transcript
    sample_transcript = """
    Doctor: Buenos días, ¿cómo está usted?
    Paciente: Buenos días doctor. Me llamo María González.
    Doctor: ¿Cuál es su fecha de nacimiento?
    Paciente: 15 de marzo de 1985.
    Doctor: ¿Y qué la trae hoy por aquí?
    Paciente: Doctor, llevo tres días con mucho dolor de garganta y fiebre.
    Doctor: ¿Qué temperatura ha tenido?
    Paciente: Hasta 38.5 grados.
    Doctor: Déjeme examinarla. Su presión arterial es 120 sobre 80. Veo inflamación en la garganta y las amígdalas están rojas.
    Paciente: ¿Es grave doctor?
    Doctor: Parece una faringitis bacteriana. Le voy a recetar amoxicilina 500 miligramos cada 8 horas por 7 días. También tome paracetamol para la fiebre.
    Paciente: ¿Algo más doctor?
    Doctor: Sí, descanse, tome muchos líquidos y regrese en una semana si no mejora.
    """
    
    processor = LLMProcessor()
    try:
        result = processor.extract_structured_data(sample_transcript)
        logger.info("EXTRACTED STRUCTURED DATA:\n" + json.dumps(result, indent=2, ensure_ascii=False))
    except LLMProcessingError as e:
        logger.error(f"Extraction failed: {e}")
