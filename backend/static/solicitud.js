// backend/static/solicitud.js
// Public access-request form (/solicitud, #53/#54). No account, no
// supabase-js: one POST to /api/solicitudes. The server does every real
// check (and normalizes); this file only hides the doctor-only fields,
// builds the body from the selected rol, and shows the server's
// per-field messages.

(function () {
    const form = document.getElementById('solForm');
    const submitBtn = document.getElementById('solSubmit');
    const formError = document.getElementById('solFormError');

    // médico / médico administrador are doctors (isMedicoRole in utils.js;
    // not loaded here to keep this page standalone).
    const ROLES_MEDICOS = ['medico', 'admin'];

    function selectedRol() {
        const checked = form.querySelector('input[name="rol"]:checked');
        return checked ? checked.value : '';
    }

    function applyRol() {
        const esMedico = ROLES_MEDICOS.includes(selectedRol());
        form.querySelectorAll('.sol-medico-only').forEach(el => {
            el.hidden = !esMedico;
        });
    }

    function value(id) {
        return document.getElementById(id).value.trim();
    }

    function clearErrors() {
        formError.textContent = '';
        form.querySelectorAll('[data-error-for]').forEach(el => { el.textContent = ''; });
        form.querySelectorAll('[aria-invalid]').forEach(el => el.removeAttribute('aria-invalid'));
    }

    // Walks the slots in page order (not the server's dict order) so focus
    // lands on the topmost problem.
    function showFieldErrors(errores) {
        let first = null;
        form.querySelectorAll('[data-error-for]').forEach(slot => {
            const field = slot.dataset.errorFor;
            if (!errores[field]) return;
            slot.textContent = errores[field];
            const input = form.querySelector(`[name="${field}"]`);
            if (input && input.type !== 'radio') input.setAttribute('aria-invalid', 'true');
            if (!first) first = input || slot;
        });
        if (first) {
            first.scrollIntoView({ block: 'center' });
            if (first.focus) first.focus({ preventScroll: true });
        }
        if (!first && Object.keys(errores).length) {
            formError.textContent = Object.values(errores)[0];
        }
    }

    // Hidden fields are never sent: an auxiliar's body has no cédula,
    // especialidad or universidad even if something was typed there first.
    function buildBody() {
        const rol = selectedRol();
        const body = {
            nombre: value('solNombre'),
            email: value('solEmail'),
            rol,
            razon_social: value('solRazonSocial'),
            domicilio: value('solDomicilio'),
            telefono: value('solTelefono'),
            acepta_terminos: document.getElementById('solTerminos').checked,
        };
        const recomendado = value('solRecomendado');
        if (recomendado) body.recomendado_por = recomendado;
        if (ROLES_MEDICOS.includes(rol)) {
            body.cedula = value('solCedula');
            body.especialidad = value('solEspecialidad');
            const universidad = value('solUniversidad');
            if (universidad) body.universidad = universidad;
        }
        const honeypot = document.getElementById('solSitioWeb').value;
        if (honeypot) body.sitio_web = honeypot;
        return body;
    }

    function showDone(mensaje) {
        document.getElementById('solFormWrap').hidden = true;
        document.getElementById('solDoneText').textContent = mensaje;
        document.getElementById('solDone').hidden = false;
        window.scrollTo(0, 0);
    }

    async function submit(e) {
        e.preventDefault();
        clearErrors();

        submitBtn.disabled = true;
        submitBtn.textContent = 'Enviando...';
        try {
            const res = await fetch('/api/solicitudes', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(buildBody()),
            });
            let data = {};
            try { data = await res.json(); } catch { /* non-JSON error page */ }

            if (res.ok) {
                showDone(data.mensaje || 'Recibimos tu solicitud.');
                return;
            }
            if (res.status === 400 && data.errores) {
                showFieldErrors(data.errores);
                return;
            }
            formError.textContent = data.error || 'No pudimos enviar tu solicitud. Intenta de nuevo.';
        } catch (err) {
            formError.textContent = 'Error de red. Revisa tu conexión e intenta de nuevo.';
        } finally {
            submitBtn.disabled = false;
            submitBtn.textContent = 'Enviar solicitud';
        }
    }

    form.querySelectorAll('input[name="rol"]').forEach(r => r.addEventListener('change', applyRol));
    form.addEventListener('submit', submit);
    applyRol();
})();
