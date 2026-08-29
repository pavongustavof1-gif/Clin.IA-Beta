# backend/transcription.py
# Added custom spelling section line 37
import assemblyai as aai
from config import Config
from logger import logger
from typing import Dict


class TranscriptionService:
    """Handles audio transcription using AssemblyAI"""

    def __init__(self):
        """Initialize AssemblyAI with API key"""
        aai.settings.api_key = Config.ASSEMBLYAI_API_KEY
        self.transcriber = aai.Transcriber()

    def transcribe_audio(self, audio_file_path: str, print_raw: bool = False, speakers_expected: int = 0) -> Dict:
        """
        Transcribe audio file using AssemblyAI

        Args:
            audio_file_path: Path to audio file or URL
            print_raw: Whether to additionally log transcript METADATA
                (duration/confidence/word count) at DEBUG. Never logs
                transcript or utterance CONTENT regardless of this flag or
                the configured log level — that content is PHI and must
                never reach a retained log (Stage H1 fix #11).
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

            # Clinical role naming (Doctor/Paciente/etc.) is no longer decided
            # here — the AssemblyAI speaker labels (A, B, C...) already on
            # each utterance are passed through as-is; Gemini infers the
            # actual clinical role from the dialogue itself (Fix #30). This
            # used to run a word-count heuristic ("most words = doctor"),
            # which was frequently wrong — patients often talk more.
            speaker_count = len({u['speaker'] for u in result["utterances"]})
            logger.info(f"Transcription: {speaker_count} speaker(s) detected")

            # Metadata only — never transcript/utterance CONTENT, at any
            # log level (Stage H1 fix #11). This is the entire scope of
            # what print_raw controls now.
            if print_raw:
                logger.debug(f"Transcription: Duration: {result['audio_duration'] / 1000:.2f} seconds")
                logger.debug(f"Transcription: Confidence: {result['confidence']:.2%}")
                logger.debug(f"Transcription: Word count: {result['words']}")

            # LFPDPPP compliance: delete transcript from AssemblyAI servers immediately.
            # Patient audio data must not be retained on third-party servers beyond
            # what is strictly necessary for processing.
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


# Example usage and testing
if __name__ == "__main__":
    # Test the transcription service
    Config.validate()
    service = TranscriptionService()

    logger.info("TranscriptionService initialized successfully")
    logger.info("Ready to transcribe audio files")

    # Example: Transcribe a sample file
    # result = service.transcribe_audio("path/to/sample_consultation.wav")
