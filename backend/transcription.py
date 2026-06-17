# backend/transcription.py
# Added custom spelling section line 37
import assemblyai as aai
from config import Config
from logger import logger
from typing import Dict


def assign_speaker_roles(utterances: list, speakers_expected: int) -> dict:
    """
    Maps AssemblyAI speaker labels (A, B, C...) to clinical role names.
    Assignment is based on word count — the speaker with the most words
    is assumed to be the doctor (longest speaking time, medical terminology).

    Returns a dict like: {'A': 'Doctor', 'B': 'Paciente', 'C': 'Familiar'}
    """
    if not utterances:
        return {}

    word_counts = {}
    for utt in utterances:
        speaker = utt.get('speaker', 'A')
        words = len(utt.get('text', '').split())
        word_counts[speaker] = word_counts.get(speaker, 0) + words

    ranked = sorted(word_counts.keys(), key=lambda s: word_counts[s], reverse=True)

    role_names = ['Doctor', 'Paciente', 'Familiar', 'Enfermera']

    role_map = {}
    for i, speaker in enumerate(ranked):
        if i < len(role_names):
            role_map[speaker] = role_names[i]
        else:
            role_map[speaker] = f'Hablante {i + 1}'

    return role_map

class TranscriptionService:
    """Handles audio transcription using AssemblyAI"""

    def __init__(self):
        """Initialize AssemblyAI with API key"""
        aai.settings.api_key = Config.ASSEMBLYAI_API_KEY
        self.transcriber = aai.Transcriber()

    def transcribe_audio(self, audio_file_path: str, print_raw: bool = True, speakers_expected: int = 0) -> Dict:
        """
        Transcribe audio file using AssemblyAI

        Args:
            audio_file_path: Path to audio file or URL
            print_raw: Whether to print raw transcript (for Alpha verification)
            speakers_expected: Hint to AssemblyAI for number of speakers (0 = not set)

        Returns:
            Dictionary containing transcript and metadata
        """
        logger.info(f"AssemblyAI: Starting transcription for: {audio_file_path}")

        # Configure transcription settings for Spanish medical context
        config = aai.TranscriptionConfig(
            language_code="es",  # Spanish
            punctuate=True,
            format_text=True,
            speaker_labels=True,  # Identify doctor vs patient (helpful for SOAP)
            domain="medical-v1",  # Medical Mode: improved accuracy for medications, dosages, diagnoses
            **({'speakers_expected': speakers_expected} if speakers_expected > 0 else {})
        )

        # Configure custom spelling
        config.set_custom_spelling(
          {
            "esguince": ["esquinza"],
          }
        )

        try:
            # Submit transcription
            transcript = self.transcriber.transcribe(
                audio_file_path,
                config=config
            )

            logger.info("AssemblyAI: Transcription submitted, waiting for completion...")

            # Check status
            if transcript.status == aai.TranscriptStatus.error:
                raise Exception(f"Transcription failed: {transcript.error}")

            # Prepare result
            result = {
                "text": transcript.text,
                "confidence": transcript.confidence,
                "audio_duration": transcript.audio_duration,
                "words": len(transcript.words) if transcript.words else 0,
                "utterances": [],
                "transcript_id": transcript.id
            }

            # Add speaker-separated utterances if available
            if transcript.utterances:
                result["utterances"] = [
                    {
                        "speaker": utt.speaker,
                        "text": utt.text,
                        "confidence": utt.confidence,
                        "start": utt.start,
                        "end": utt.end
                    }
                    for utt in transcript.utterances
                ]

            # Assign clinical role names to speaker labels
            role_map = assign_speaker_roles(result["utterances"], speakers_expected)
            result["speaker_role_map"] = role_map
            logger.info(f"Transcription: Speaker role mapping: {role_map}")

            # Verification: log raw transcript details
            if print_raw:
                logger.debug(f"Transcription: Duration: {result['audio_duration'] / 1000:.2f} seconds")
                logger.debug(f"Transcription: Confidence: {result['confidence']:.2%}")
                logger.debug(f"Transcription: Word count: {result['words']}")
                logger.debug(f"Transcription: Full text:\n{result['text']}")

                if result["utterances"]:
                    lines = []
                    for utt in result["utterances"]:
                        role = role_map.get(utt['speaker'], 'Hablante ' + utt['speaker'])
                        lines.append(f"[{role}]: {utt['text']}")
                    logger.debug("Transcription: Speaker-separated transcript:\n" + "\n".join(lines))

            # LFPDPPP compliance: delete transcript from AssemblyAI servers immediately.
            # Patient audio data must not be retained on third-party servers beyond
            # what is strictly necessary for processing.
            # Note: transcribe_from_bytes() calls this method, so deletion is
            # covered for all callers — no separate handling needed there.
            try:
                aai.Transcript.delete_by_id(transcript.id)
                logger.info(f"AssemblyAI: Transcript {transcript.id} deleted from servers.")
            except Exception as del_err:
                logger.warning(f"AssemblyAI: Could not delete transcript {transcript.id}: {str(del_err)}")
                # Do NOT raise — deletion failure must never block the pipeline

            return result

        except Exception as e:
            logger.error(f"AssemblyAI: Error during transcription: {str(e)}")
            raise

    def transcribe_from_bytes(self, audio_data: bytes, print_raw: bool = True) -> Dict:
        """
        Transcribe audio from bytes (for real-time recording)

        Args:
            audio_data: Raw audio bytes
            print_raw: Whether to print raw transcript

        Returns:
            Dictionary containing transcript and metadata
        """
        # Save bytes to temporary file
        import tempfile
        import os

        with tempfile.NamedTemporaryFile(delete=False, suffix='.wav') as tmp_file:
            tmp_file.write(audio_data)
            tmp_path = tmp_file.name

        try:
            result = self.transcribe_audio(tmp_path, print_raw)
            return result
        finally:
            # Clean up temporary file
            if os.path.exists(tmp_path):
                os.remove(tmp_path)

    def estimate_cost(self, audio_duration_seconds: float) -> float:
        """
        Estimate transcription cost (AssemblyAI pricing as of 2024)

        Args:
            audio_duration_seconds: Duration in seconds

        Returns:
            Estimated cost in USD
        """
        # AssemblyAI charges per audio hour
        # Approximate pricing: $0.00025 per second
        return audio_duration_seconds * 0.00025


# Example usage and testing
if __name__ == "__main__":
    # Test the transcription service
    Config.validate()
    service = TranscriptionService()

    logger.info("TranscriptionService initialized successfully")
    logger.info("Ready to transcribe audio files")

    # Example: Transcribe a sample file
    # result = service.transcribe_audio("path/to/sample_consultation.wav")
