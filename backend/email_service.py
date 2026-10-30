# backend/email_service.py
# Email delivery service for ClinIA using Resend
# Sends PDF of confirmed clinical note to the doctor's registered email

import resend
import base64
import html
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
                            <strong>Paciente:</strong> {html.escape(patient_name)}
                        </p>
                        <p style="margin: 0 0 24px 0;">
                            <strong>Fecha de consulta:</strong> {html.escape(consultation_date)}
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


def send_invite_email(
    doctor_email: str,
    doctor_nombre: str,
    clinica_nombre: str,
    action_link: str
) -> bool:
    """
    Send a clinic invite email containing Supabase's action_link, so the
    doctor can set their own password. Sent via Resend rather than
    Supabase's own mailer — avoids a second SMTP configuration.
    Never raises — caller decides how to handle a False return.
    """
    if not Config.RESEND_API_KEY:
        logger.warning("Email: RESEND_API_KEY not configured — skipping invite email.")
        return False

    if not doctor_email or '@' not in doctor_email:
        logger.warning("Email: Invalid or missing doctor email — skipping invite.")
        return False

    try:
        resend.api_key = Config.RESEND_API_KEY

        params = {
            "from": f"Clin.IA <{Config.RESEND_SENDER}>",
            "to": [doctor_email],
            "subject": f"Has sido invitado a Clin.IA — {clinica_nombre}",
            "html": f"""
                <div style="font-family: Arial, sans-serif; max-width: 480px; color: #334155;">
                    <div style="background: #0F6E56; padding: 16px 24px;
                                border-radius: 6px 6px 0 0;">
                        <h2 style="color: white; margin: 0; font-size: 18px;">Clin.IA</h2>
                        <p style="color: #C8EEE4; margin: 4px 0 0 0; font-size: 13px;">
                            Invitación a la clínica
                        </p>
                    </div>
                    <div style="background: #f8fafc; padding: 24px;
                                border: 1px solid #e2e8f0;
                                border-top: none; border-radius: 0 0 6px 6px;">
                        <p style="margin: 0 0 12px 0;">
                            Hola {html.escape(doctor_nombre)},
                        </p>
                        <p style="margin: 0 0 16px 0;">
                            Se te ha invitado a unirte a <strong>{html.escape(clinica_nombre)}</strong> en Clin.IA.
                            Haz clic en el siguiente enlace para configurar tu contraseña y
                            acceder a tu cuenta:
                        </p>
                        <p style="margin: 0 0 24px 0;">
                            <a href="{action_link}"
                               style="display: inline-block; background: #0F6E56; color: white;
                                      padding: 10px 20px; border-radius: 6px; text-decoration: none;
                                      font-weight: 600;">
                                Configurar mi contraseña
                            </a>
                        </p>
                        <p style="font-size: 12px; color: #94a3b8; margin: 0;">
                            Si no esperabas esta invitación, puedes ignorar este correo.
                            Para dudas:
                            <a href="mailto:info@clinianotes.com"
                            style="color: #0F6E56;">info@clinianotes.com</a>
                        </p>
                    </div>
                </div>
            """,
        }

        response = resend.Emails.send(params)
        # No address in the log: the superadmin approval flow sends this
        # to applicants, whose email must never reach Render's logs.
        logger.info(f"Email: Invite sent — id: {response.get('id', 'unknown')}")
        return True

    except Exception as e:
        logger.error(f"Email: Failed to send invite ({type(e).__name__})")
        return False


SUPERADMIN_PANEL_URL = 'https://app.clinianotes.com/superadmin'


def send_solicitud_notice(superadmin_email: str, applicant_nombre: str) -> bool:
    """
    Tell the superadmin a new access request (solicitudes_registro) came
    in. Carries the applicant's nombre ONLY — email, cédula, phone and
    address stay in the database, behind the superadmin page, not in an
    inbox. Nothing about the applicant is logged here either, not even
    the nombre. Never raises; the request must not fail on this.
    """
    if not Config.RESEND_API_KEY:
        logger.warning("Email: RESEND_API_KEY not configured — skipping solicitud notice.")
        return False

    try:
        resend.api_key = Config.RESEND_API_KEY

        params = {
            "from": f"Clin.IA <{Config.RESEND_SENDER}>",
            "to": [superadmin_email],
            "subject": "Nueva solicitud de acceso — Clin.IA",
            "html": f"""
                <div style="font-family: Arial, sans-serif; max-width: 480px; color: #334155;">
                    <div style="background: #0F6E56; padding: 16px 24px;
                                border-radius: 6px 6px 0 0;">
                        <h2 style="color: white; margin: 0; font-size: 18px;">Clin.IA</h2>
                        <p style="color: #C8EEE4; margin: 4px 0 0 0; font-size: 13px;">
                            Nueva solicitud de acceso
                        </p>
                    </div>
                    <div style="background: #f8fafc; padding: 24px;
                                border: 1px solid #e2e8f0;
                                border-top: none; border-radius: 0 0 6px 6px;">
                        <p style="margin: 0 0 16px 0;">
                            <strong>{html.escape(applicant_nombre)}</strong> solicitó acceso a Clin.IA.
                        </p>
                        <p style="margin: 0 0 24px 0;">
                            <a href="{SUPERADMIN_PANEL_URL}"
                               style="display: inline-block; background: #0F6E56; color: white;
                                      padding: 10px 20px; border-radius: 6px; text-decoration: none;
                                      font-weight: 600;">
                                Revisar solicitudes
                            </a>
                        </p>
                        <p style="font-size: 12px; color: #94a3b8; margin: 0;">
                            Los datos completos de la solicitud están en el panel, no en este correo.
                        </p>
                    </div>
                </div>
            """,
        }

        response = resend.Emails.send(params)
        logger.info(f"Email: solicitud notice sent — id: {response.get('id', 'unknown')}")
        return True

    except Exception as e:
        # type only — a provider error message could echo the recipient
        logger.error(f"Email: Failed to send solicitud notice ({type(e).__name__})")
        return False


def _card_html(subtitle: str, body_html: str) -> str:
    """The house email frame (green header, grey card) around body_html."""
    return f"""
        <div style="font-family: Arial, sans-serif; max-width: 480px; color: #334155;">
            <div style="background: #0F6E56; padding: 16px 24px;
                        border-radius: 6px 6px 0 0;">
                <h2 style="color: white; margin: 0; font-size: 18px;">Clin.IA</h2>
                <p style="color: #C8EEE4; margin: 4px 0 0 0; font-size: 13px;">
                    {html.escape(subtitle)}
                </p>
            </div>
            <div style="background: #f8fafc; padding: 24px;
                        border: 1px solid #e2e8f0;
                        border-top: none; border-radius: 0 0 6px 6px;">
                {body_html}
            </div>
        </div>
    """


def send_usuario_agregado_email(admin_email: str, nuevo_nombre: str,
                                clinica_nombre: str, rol_label: str) -> bool:
    """
    Tell an existing clinic admin/auxiliar that the superadmin approved a
    new user into their clinic (#53). Names only — never the new user's
    email, cédula or phone. Never raises; logs nothing about either person.
    """
    if not Config.RESEND_API_KEY:
        logger.warning("Email: RESEND_API_KEY not configured — skipping usuario-agregado notice.")
        return False

    try:
        resend.api_key = Config.RESEND_API_KEY
        body = f"""
            <p style="margin: 0 0 16px 0;">
                <strong>{html.escape(nuevo_nombre)}</strong> se unió a
                <strong>{html.escape(clinica_nombre)}</strong> en Clin.IA como
                {html.escape(rol_label)}.
            </p>
            <p style="font-size: 12px; color: #94a3b8; margin: 0;">
                Puedes ver y administrar a los usuarios de tu clínica en el panel de administración.
                Si no reconoces a esta persona, escribe a
                <a href="mailto:info@clinianotes.com" style="color: #0F6E56;">info@clinianotes.com</a>.
            </p>
        """
        response = resend.Emails.send({
            "from": f"Clin.IA <{Config.RESEND_SENDER}>",
            "to": [admin_email],
            "subject": f"Nuevo usuario en {clinica_nombre} — Clin.IA",
            "html": _card_html('Nuevo usuario en tu clínica', body),
        })
        logger.info(f"Email: usuario-agregado notice sent — id: {response.get('id', 'unknown')}")
        return True

    except Exception as e:
        logger.error(f"Email: Failed to send usuario-agregado notice ({type(e).__name__})")
        return False


# DRAFT pending legal review — the whole rejection email lives here so
# the wording is edited in one place. The superadmin page shows a preview
# rendered from this same dict (GET /api/superadmin/solicitudes returns
# it), so what Gustavo previews is exactly what is sent. Placeholders:
# {nombre} in 'cuerpo'; {bloque_motivo} is replaced by 'bloque_motivo'
# (with {motivo} filled in) when a motivo was written, or by '' if not.
REJECTION_EMAIL_DRAFT = {
    'asunto': 'Tu solicitud de acceso a Clin.IA',
    'cuerpo': (
        'Hola {nombre}:\n'
        '\n'
        'Gracias por tu interés en Clin.IA. Revisamos tu solicitud de acceso '
        'y por el momento no podemos aprobarla.\n'
        '\n'
        '{bloque_motivo}'
        'Si crees que hay un error o quieres darnos más información, responde '
        'a este correo y con gusto lo revisamos.\n'
        '\n'
        'Saludos,\n'
        'Equipo Clin.IA'
    ),
    'bloque_motivo': 'Motivo: {motivo}\n\n',
}


def render_rejection_text(nombre: str, motivo: str | None) -> str:
    """Plain-text rejection body. str.replace, not str.format, so braces
    typed into motivo or nombre are kept literally (the page's preview
    does the same split/join substitution)."""
    bloque = ''
    if motivo:
        bloque = REJECTION_EMAIL_DRAFT['bloque_motivo'].replace('{motivo}', motivo)
    return (REJECTION_EMAIL_DRAFT['cuerpo']
            .replace('{nombre}', nombre)
            .replace('{bloque_motivo}', bloque))


def render_rejection_html(nombre: str, motivo: str | None) -> str:
    """The same text, every line escaped and line breaks kept as <br>."""
    text = render_rejection_text(nombre, motivo)
    lines = '<br>'.join(html.escape(line) for line in text.split('\n'))
    return _card_html('Solicitud de acceso', f'<p style="margin: 0; line-height: 1.5;">{lines}</p>')


def send_rejection_email(applicant_email: str, nombre: str, motivo: str | None,
                         reply_to: str | None) -> bool:
    """Send REJECTION_EMAIL_DRAFT to an applicant, Reply-To the superadmin
    so a reply reaches a person. Never raises; never logs the applicant."""
    if not Config.RESEND_API_KEY:
        logger.warning("Email: RESEND_API_KEY not configured — skipping rejection email.")
        return False

    try:
        resend.api_key = Config.RESEND_API_KEY
        params = {
            "from": f"Clin.IA <{Config.RESEND_SENDER}>",
            "to": [applicant_email],
            "subject": REJECTION_EMAIL_DRAFT['asunto'],
            "html": render_rejection_html(nombre, motivo),
            "text": render_rejection_text(nombre, motivo),
        }
        if reply_to:
            params["reply_to"] = reply_to
        response = resend.Emails.send(params)
        logger.info(f"Email: rejection sent — id: {response.get('id', 'unknown')}")
        return True

    except Exception as e:
        logger.error(f"Email: Failed to send rejection ({type(e).__name__})")
        return False
