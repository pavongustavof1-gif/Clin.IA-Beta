# backend/transcription.py
import assemblyai as aai
from config import Config
import time
from typing import Optional, Dict
import json

class TranscriptionService:
    """Handles audio transcription using AssemblyAI"""
    
    def __init__(self):
        """Initialize AssemblyAI with API key"""
        aai.settings.api_key = Config.ASSEMBLYAI_API_KEY
        self.transcriber = aai.Transcriber()
    
    def transcribe_audio(self, audio_file_path: str, print_raw: bool = True) -> Dict:
        """
        Transcribe audio file using AssemblyAI
        
        Args:
            audio_file_path: Path to audio file or URL
            print_raw: Whether to print raw transcript (for Alpha verification)
        
        Returns:
            Dictionary containing transcript and metadata
        """
        print(f"[AssemblyAI] Starting transcription for: {audio_file_path}")
        
        # Configure transcription settings for Spanish medical context
        config = aai.TranscriptionConfig(
            language_code="es",  # Spanish
            punctuate=True,
            format_text=True,
            speaker_labels=True  # Identify doctor vs patient (helpful for SOAP)
        )
        
        try:
            # Submit transcription
            transcript = self.transcriber.transcribe(
                audio_file_path,
                config=config
            )
            
            # Wait for completion
            print("[AssemblyAI] Transcription submitted, waiting for completion...")
            
            # Check status
            if transcript.status == aai.TranscriptStatus.error:
                raise Exception(f"Transcription failed: {transcript.error}")
            
            # Prepare result
            result = {
                "text": transcript.text,
                "confidence": transcript.confidence,
                "audio_duration": transcript.audio_duration,
                "words": len(transcript.words) if transcript.words else 0,
                "utterances": []
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
            
            # Alpha verification: Print raw transcript
            if print_raw:
                print("\n" + "="*80)
                print("RAW TRANSCRIPT (Alpha Verification)")
                print("="*80)
                print(f"Duration: {result['audio_duration'] / 1000:.2f} seconds")
                print(f"Confidence: {result['confidence']:.2%}")
                print(f"Word count: {result['words']}")
                print("\nFull Transcript:")
                print("-"*80)
                print(result["text"])
                print("-"*80)
                
                if result["utterances"]:
                    print("\nSpeaker-Separated Transcript:")
                    print("-"*80)
                    for utt in result["utterances"]:
                        print(f"[Speaker {utt['speaker']}]: {utt['text']}")
                    print("-"*80)
                print("="*80 + "\n")
            
            return result
            
        except Exception as e:
            print(f"[AssemblyAI] Error during transcription: {str(e)}")
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
    
    print("TranscriptionService initialized successfully")
    print("Ready to transcribe audio files")
    
    # Example: Transcribe a sample file
    # result = service.transcribe_audio("path/to/sample_consultation.wav")
    # print(json.dumps(result, indent=2, ensure_ascii=False))
