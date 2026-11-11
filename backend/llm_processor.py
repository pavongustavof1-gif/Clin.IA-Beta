# backend/llm_processor.py
from google import genai
from google.genai import types # For configuration
from config import Config
from logger import logger
from icd_service import lookup_cie11, normalize_cie11_code, log_lookup_outcome
# #83: the pure half (prompt, JSON cleaner, validator, CIE-11 term helpers)
# lives in extraction_core, shared with the bake-off harness. Re-exported
# here so existing callers (`from llm_processor import ...`) keep working.
from extraction_core import (  # noqa: F401
    CIE11_TERM_KEY,
    accept_search_term,
    clean_json_response,
    content_words,
    create_extraction_prompt,
    normalize_evaluacion,
    validate_against_schema,
)
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


# 0.8.5 (CIE-11 review #4): a CIE-11 code comes only from the WHO lookup or
# from the doctor — never from Gemini. These keys are removed from Gemini's
# output before the lookup runs, whatever the lookup then does.
CIE11_KEYS = ('codigo_cie11', 'titulo_cie11', 'cie11_fuente')
CIE11_FUENTE_OMS = 'oms'
CIE11_FUENTE_MEDICO = 'medico'


def apply_cie11_lookup(data) -> None:
    """Strip any Gemini-supplied CIE-11 keys and the internal search term,
    then fill code + title from the WHO lookup chain with cie11_fuente='oms'.
    No WHO code → no CIE-11 keys at all (the review screen shows an empty
    field for the doctor to fill)."""
    if not isinstance(data, dict):
        return
    ev = data.get('evaluacion')
    if not isinstance(ev, dict):
        # Still one outcome line per job (0.8.6): nothing to look up.
        log_lookup_outcome('no_diagnosis')
        return
    for key in CIE11_KEYS:
        ev.pop(key, None)
    term = ev.pop(CIE11_TERM_KEY, None)
    diagnostico = ev.get('diagnostico')
    if not isinstance(diagnostico, str) or not diagnostico.strip():
        log_lookup_outcome('no_diagnosis')
        return
    if term is not None and not accept_search_term(term, diagnostico):
        # No text in the log: the term is as much PHI as the diagnosis.
        log_lookup_outcome('term_rejected', stage='term')
        term = None
    try:
        cie_result = lookup_cie11(diagnostico, term)
    except Exception as e:
        # Exception type only — the message could carry the diagnosis.
        logger.warning(f"ICD: Could not inject CIE-11 code (non-fatal): {type(e).__name__}")
        return
    code = normalize_cie11_code((cie_result or {}).get('code'))
    if not code:
        return
    ev['codigo_cie11'] = code
    title = (cie_result or {}).get('title')
    if isinstance(title, str) and title.strip():
        ev['titulo_cie11'] = title.strip()
    ev['cie11_fuente'] = CIE11_FUENTE_OMS
    # No code/title in the log — a patient's diagnosis code is PHI same as
    # the free-text diagnosis it came from (Stage H1 fix #11).
    logger.info("ICD: CIE-11 code injected into structured_data")


class LLMProcessor:
    """Processes transcripts using Google Gemini for structured extraction"""
    
    def __init__(self):
        """Initialize Gemini API"""
        self.client = genai.Client(api_key=Config.GEMINI_API_KEY)
        self.model_id = 'gemini-2.5-flash'

    def create_extraction_prompt(self, transcript: str, utterances: list = None) -> str:
        """The extraction prompt — lives in extraction_core (#83), shared
        verbatim with the bake-off harness."""
        return create_extraction_prompt(transcript, utterances)
    
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

                # 0.8.5: normalise the diagnosis key first (so the lookup
                # sees diagnostico_principal too), then replace whatever
                # CIE-11 keys Gemini returned with the WHO lookup's result.
                normalize_evaluacion(structured_data)
                apply_cie11_lookup(structured_data)

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
        """Clean LLM response to extract pure JSON (extraction_core, #83)."""
        return clean_json_response(response)
    
    def validate_against_schema(self, data: Dict) -> tuple[bool, Optional[str]]:
        """Structural validation + normalisation in place — see
        extraction_core.validate_against_schema (#83) for the rules."""
        return validate_against_schema(data)



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
