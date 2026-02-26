# backend/llm_processor.py
# added logic to prompt, line 41
import google.generativeai as genai
from config import Config
import json
from typing import Dict, Optional

class LLMProcessor:
    """Processes transcripts using Google Gemini for structured extraction"""
    
    def __init__(self):
        """Initialize Gemini API"""
        genai.configure(api_key=Config.GEMINI_API_KEY)
        
        # Use Gemini flash-latest for fast, cost-effective processing
        self.model = genai.GenerativeModel('gemini-flash-latest')
        
        # Load JSON schema
        # Look for the file in the current folder, wherever that may be
        with open('credentials.json', 'r', encoding='utf-8') as f:
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
- Responde SOLO con el JSON, sin texto adicional antes o después
- No incluyas ```json ni ningún otro formato de código
- Si un campo no tiene información, omítelo del JSON
- Asegúrate de que el JSON sea válido y pueda ser parseado
- Usa comillas dobles para strings, no comillas simples
- Mantén los acentos y caracteres especiales del español

Ahora extrae la información de la transcripción y genera el JSON:"""
        
        return prompt
    
    def extract_structured_data(self, transcript: str, max_retries: int = 3) -> Dict:
        """
        Extract structured medical data from transcript using Gemini
        
        Args:
            transcript: Raw transcript text
            max_retries: Number of retry attempts if JSON parsing fails
        
        Returns:
            Structured dictionary matching schema
        """
        print("[LLM] Processing transcript with Gemini Flash...")
        
        prompt = self.create_extraction_prompt(transcript)
        
        for attempt in range(max_retries):
            try:
                # Generate response
                response = self.model.generate_content(
                    prompt,
                    generation_config=genai.GenerationConfig(
                        temperature=0.1,  # Low temperature for consistency
                        top_p=0.95,
                        top_k=40,
                        max_output_tokens=2048,
                    )
                )
                
                # Extract text response
                response_text = response.text.strip()
                
                print(f"\n[LLM] Raw Response (Attempt {attempt + 1}):")
                print("-" * 80)
                print(response_text[:500] + "..." if len(response_text) > 500 else response_text)
                print("-" * 80)
                
                # Clean response (remove markdown code blocks if present)
                cleaned_response = self._clean_json_response(response_text)
                
                # Parse JSON
                structured_data = json.loads(cleaned_response)
                
                print("[LLM] Successfully extracted and parsed structured data")
                return structured_data
                
            except json.JSONDecodeError as e:
                print(f"[LLM] JSON parsing error on attempt {attempt + 1}: {str(e)}")
                if attempt == max_retries - 1:
                    # Last attempt failed, try to salvage what we can
                    print("[LLM] All parsing attempts failed, returning error structure")
                    return {
                        "error": "Failed to parse LLM response",
                        "raw_response": response_text,
                        "informacion_paciente": {}
                    }
                # Add instruction to be more careful with JSON format
                prompt += "\n\nNOTA: El JSON anterior no era válido. Asegúrate de generar JSON perfectamente válido esta vez."
                
            except Exception as e:
                print(f"[LLM] Unexpected error: {str(e)}")
                raise
    
    def _clean_json_response(self, response: str) -> str:
        """
        Clean LLM response to extract pure JSON
        
        Args:
            response: Raw LLM response
        
        Returns:
            Cleaned JSON string
        """
        # Remove markdown code blocks
        if "```json" in response:
            response = response.split("```json")[1].split("```")[0]
        elif "```" in response:
            response = response.split("```")[1].split("```")[0]
        
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
