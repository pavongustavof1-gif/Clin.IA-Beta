# ClinIA - AI Medical Note Taker (Alpha Version)

An intelligent Spanish-language medical note-taking system that transcribes patient consultations and generates structured SOAP-compliant clinical notes.

## 🚀 Features

- **Audio Recording**: Record consultations directly from browser (desktop/mobile)
- **File Upload**: Support for WAV, MP3, WEBM, OGG, M4A formats
- **Spanish Transcription**: High-accuracy transcription using AssemblyAI
- **SOAP Format**: Automatic extraction of Subjective, Objective, Assessment, Plan sections
- **Google Docs Integration**: Automated, formatted document generation
- **Structured Data Export**: Download extracted data as JSON

## 📋 Prerequisites

- Python 3.9+
- Google Cloud Account (for Docs API)
- AssemblyAI API Key
- Google Gemini API Key (or OpenAI API Key)

## 🛠️ Installation

### Step 1: Clone and Setup
```bash
# Navigate to project directory
cd clinia-alpha

# Create virtual environment
python -m venv venv

# Activate virtual environment
# macOS/Linux:
source venv/bin/activate
# Windows:
venv\Scripts\activate

# Install dependencies
pip install -r backend/requirements.txt
```
