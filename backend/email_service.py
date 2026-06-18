# backend/email_service.py
# Email delivery service for ClinIA using Resend
# Sends PDF of confirmed clinical note to the doctor's registered email

import resend
import base64
from config import Config
from logger import logger


def send_pdf_email(
    doctor_email: str,
    pdf_bytes: bytes,
    patient_name: str,
    consultation_date: str,
    session_id: str
) -> bool:
    """
    Send the confirmed clinical note PDF to the doctor's email.
    Returns True if sent successfully, False otherwise.
    Never raises — email failure must never block the pipeline.
    """
    if not Config.RESEND_API_KEY:
        logger.warning("Email: RESEND_API_KEY not configured — skipping email delivery.")
        return False

    if not doctor_email or '@' not in doctor_email:
        logger.warning("Email: Invalid or missing doctor email — skipping.")
        return False

    try:
        resend.api_key = Config.RESEND_API_KEY

        safe_name = ''.join(
            c for c in patient_name if c.isalnum() or c in (' ', '-')
        ).strip().replace(' ', '_')[:30]
        filename = f"ClinIA_{safe_name}_{consultation_date}.pdf"

        pdf_b64 = base64.b64encode(pdf_bytes).decode('utf-8')

        params = {
            "from": f"Clin.IA <{Config.RESEND_SENDER}>",
            "to": [doctor_email],
            "subject": f"Nota clínica — {patient_name} — {consultation_date}",
            "html": f"""
                <div style="font-family: Arial, sans-serif; max-width: 480px; color: #334155;">
                    <div style="background: #0F6E56; padding: 16px 24px;
                                border-radius: 6px 6px 0 0;">
                        <h2 style="color: white; margin: 0; font-size: 18px;">Clin.IA</h2>
                        <p style="color: #C8EEE4; margin: 4px 0 0 0; font-size: 13px;">
                            Nota clínica generada
                        </p>
                    </div>
                    <div style="background: #f8fafc; padding: 24px;
                                border: 1px solid #e2e8f0;
                                border-top: none; border-radius: 0 0 6px 6px;">
                        <p style="margin: 0 0 12px 0;">
                            Se adjunta la nota clínica de evolución generada para:
                        </p>
                        <p style="margin: 0 0 8px 0;">
                            <strong>Paciente:</strong> {patient_name}
                        </p>
                        <p style="margin: 0 0 24px 0;">
                            <strong>Fecha de consulta:</strong> {consultation_date}
                        </p>
                        <p style="font-size: 12px; color: #94a3b8; margin: 0;">
                            Este mensaje fue generado automáticamente por Clin.IA.
                            El documento adjunto es la nota confirmada e inalterable.
                            Para dudas:
                            <a href="mailto:info@clinianotes.com"
                            style="color: #0F6E56;">info@clinianotes.com</a>
                        </p>
                    </div>
                </div>
            """,
            "attachments": [
                {
                    "filename": filename,
                    "content": pdf_b64,
                }
            ],
        }

        response = resend.Emails.send(params)
        logger.info(f"Email: PDF sent to {doctor_email} — id: {response.get('id', 'unknown')}")
        return True

    except Exception as e:
        logger.error(f"Email: Failed to send to {doctor_email}: {str(e)}")
        return False
