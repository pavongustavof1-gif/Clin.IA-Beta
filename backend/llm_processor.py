# backend/llm_processor.py
# added logic to prompt, line 41
# import google.generativeai as genai
from google import genai
from google.genai import types # For configuration
from config import Config
import json
import os
import time
from typing import Dict, Optional

class LLMProcessor:
    """Processes transcripts using Google Gemini for structured extraction"""
    
    def __init__(self):
        """Initialize Gemini API"""
        genai.configure(api_key=Config.GEMINI_API_KEY)
        
        # Use Gemini flash-latest for fast, cost-effective processing
        # self.model = genai.GenerativeModel('gemini-3-flash')

        self.client = genai.Client(api_key=Config.GEMINI_API_KEY)
        self.model_id = 'gemini-2.0-flash'
        
        # Load JSON schema
        # Look for the file in the current folder, wherever that may be
        import os
        base_dir = os.path.dirname(__file__)
        cred_path = os.path.join(base_dir, 'credentials.json')

        with open(cred_path, 'r', encoding='utf-8') as f:
            self.schema = json.load(f)
    
    def create_extraction_prompt(self, transcript: str) -> str:
        """
        Create detailed prompt for Gemini to extract medical information
        
        Args:
            transcript: Raw Spanish transcript from AssemblyAI
        
        Returns:
            Formatted prompt for LLM
        """
        prompt = f"""Eres un asistente médico especializado en crear notas clínicas siguiendo el formato SOAP (Subjetivo, Objetivo, Evaluación, Plan).

Tu tarea es analizar la siguiente transcripción de una consulta médica en español y extraer toda la información relevante en un formato JSON estructurado.

INSTRUCCIONES CRÍTICAS:
1. Debes extraer ÚNICAMENTE información que esté explícitamente mencionada en la transcripción
2. Si cierta información no está presente, omite ese campo (no inventes datos)
3. Mantén los términos médicos exactamente como aparecen en la transcripción, asegurando la congruencia de genero entre artículos y artículos indefinidos con el sustantivo que le sigue
4. Organiza la información según el formato SOAP
5. Identifica y separa la información del paciente, síntomas, hallazgos, diagnóstico y plan de tratamiento

TRANSCRIPCIÓN:
{transcript}

FORMATO DE SALIDA:
Debes responder ÚNICAMENTE con un objeto JSON válido que siga este esquema:

{{
  "informacion_paciente": {{
    "nombre_del_paciente": "string (si se menciona)",
    "fecha_de_nacimiento": "string en formato DD/MM/YYYY (si se menciona)",
    "edad": "string (si se menciona)",
    "genero": "string (si se menciona)"
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
      "saturacion_oxigeno": "string (si se menciona)"
    }},
    "examen_fisico": "string - hallazgos del examen físico",
    "hallazgos": ["lista de hallazgos objetivos"]
  }},
  "evaluacion": {{
    "diagnostico": "string - diagnóstico principal",
    "diagnosticos_adicionales": ["otros diagnósticos o diagnósticos diferenciales"],
    "impresion_clinica": "string - impresión general del médico"
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
  }}
}}

REGLAS IMPORTANTES:
- IMPORTANTE: Si necesitas citar al paciente o usar comillas dentro de un texto, usa SOLO comillas simples ('ejemplo'). NUNCA uses comillas dobles dentro de un valor de texto, ya que esto destruye el formato JSON.
- Responde SOLO con el JSON, sin texto adicional antes o después
- No incluyas ```json ni ningún otro formato de código
- Si un campo no tiene información, omítelo del JSON
- Asegúrate de que el JSON sea válido y pueda ser parseado
# - Usa comillas dobles para strings, no comillas simples
- Usa comillas dobles estrictamente para las llaves y estructura general del JSON.
- Mantén los acentos y caracteres especiales del español

Ahora extrae la información de la transcripción y genera el JSON:"""
        
        return prompt
    
    def extract_structured_data(self, transcript: str, max_retries: int = 3) -> Dict:
        """
        Extract structured medical data from transcript using Gemini
        """
        print(f"[LLM] Processing with {self.model_id}...")
        prompt = self.create_extraction_prompt(transcript)
        
        for attempt in range(max_retries):
            try:
                # NEW: The modern, paid-tier way to call Gemini
                response = self.client.models.generate_content(
                    model=self.model_id,
                    contents=prompt,
                    config=types.GenerateContentConfig(
                        temperature=0.1,
                        response_mime_type='application/json', # FORCES valid JSON
                    )
                )
                
                # The response is now guaranteed to be JSON
                structured_data = json.loads(response.text)
                print("[LLM] Success!")
                return structured_data
                
            except Exception as e:
                error_msg = str(e).lower()
                print(f"[LLM] Attempt {attempt + 1} failed: {error_msg}")
                
                # Catch specific retryable errors
                if any(x in error_msg for x in ["429", "504", "deadline", "cancelled"]):
                    if attempt < max_retries - 1:
                        wait_time = 5 * (attempt + 1)
                        print(f"[LLM] Retrying in {wait_time}s...")
                        time.sleep(wait_time)
                        continue
                break
        
        # Fallback if everything fails
        return {"error": "Processing failed", "raw_transcript_length": len(transcript)}

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
    result = processor.extract_structured_data(sample_transcript)
    
    print("\n" + "="*80)
    print("EXTRACTED STRUCTURED DATA")
    print("="*80)
    print(json.dumps(result, indent=2, ensure_ascii=False))
    print("="*80)
