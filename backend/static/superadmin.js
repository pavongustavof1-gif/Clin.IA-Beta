// backend/static/superadmin.js
// Superadmin review of access requests (#53, Review C). One configured
// person approves, rejects, resends or deletes solicitudes_registro rows.
// Every applicant value is rendered with textContent (via el() below) —
// never innerHTML — since all of it was typed by someone outside.
// getAuthHeaders/logout/handleSessionExpired come from auth-common.js;
// ROL_LABELS from utils.js.

const saState = {
    estado: 'pendiente',
    plantilla: null,      // REJECTION_EMAIL_DRAFT from the server
    maxMotivo: 1000,
    clinicas: null,       // cached GET /api/superadmin/clinicas rows
};

// Small DOM builder: tag, optional class, optional text (always textContent).
function el(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined && text !== null) node.textContent = String(text);
    return node;
}

function button(label, className, onClick) {
    const b = el('button', className, label);
    b.type = 'button';
    b.addEventListener('click', onClick);
    return b;
}

// After an action, refresh the list a moment later so the result message
// can be read first — but only if the same tab is still showing and no
// other card has a form open (a reload would wipe what's being typed).
// force: reload even with a form open (a failed approval's own form,
// which must turn into the card's "Reintentar aprobación").
function reloadSoon(ms, force = false) {
    const estado = saState.estado;
    setTimeout(() => {
        if (saState.estado !== estado) return;
        if (!force && document.querySelector('#saList .sa-form')) return;
        loadList(estado);
    }, ms);
}

function setStatus(text) {
    document.getElementById('saStatus').textContent = text || '';
}

function diasTexto(dias) {
    if (dias === null || dias === undefined) return '';
    if (dias === 0) return 'hoy';
    return dias === 1 ? 'hace 1 día' : `hace ${dias} días`;
}

async function saFetch(path, options = {}) {
    const headers = { ...(await getAuthHeaders()) };
    if (options.body !== undefined) headers['Content-Type'] = 'application/json';
    const res = await fetch(`${window.location.origin}${path}`, {
        method: options.method || 'GET',
        headers,
        body: options.body !== undefined ? JSON.stringify(options.body) : undefined,
    });
    let data = {};
    try { data = await res.json(); } catch { /* non-JSON error page */ }
    // The superadmin API answers 404 to everyone else, an expired token
    // included — re-check the session so an expiry goes to /login.
    if (res.status === 404 && data.error === 'No encontrado' && !path.includes('/solicitudes/')) {
        await recheckSession();
    }
    return { res, data };
}

async function recheckSession() {
    try {
        const res = await fetch(`${window.location.origin}/api/session-check`, { headers: await getAuthHeaders() });
        if (res.status === 401) { handleSessionExpired(); return; }
        const data = await res.json();
        if (!data.superadmin) window.location.replace('/');
    } catch { /* leave the page as it is */ }
}

// ── rendering helpers ───────────────────────────────────────────────────

function fieldList(row, fields) {
    const dl = el('dl', 'sa-fields');
    fields.forEach(([label, value]) => {
        if (value === null || value === undefined || value === '') return;
        const wrap = el('div', 'sa-field');
        wrap.appendChild(el('dt', null, label));
        wrap.appendChild(el('dd', null, value));
        dl.appendChild(wrap);
    });
    return dl;
}

function fechaCorta(iso) {
    if (!iso) return '';
    const d = new Date(iso);
    return isNaN(d) ? '' : d.toLocaleDateString('es-MX', { year: 'numeric', month: 'short', day: 'numeric' });
}

function cardHeader(row, extraBadges) {
    const head = el('div', 'sa-card-head');
    const title = el('div', 'sa-card-title');
    title.appendChild(el('h2', 'sa-name', row.nombre));
    title.appendChild(el('span', 'sa-rol', row.rol_solicitado_label || ''));
    head.appendChild(title);
    const badges = el('div', 'sa-badges');
    (extraBadges || []).forEach(b => badges.appendChild(b));
    head.appendChild(badges);
    return head;
}

function badge(text, kind) {
    return el('span', `sa-badge sa-badge-${kind}`, text);
}

function solicitudFields(row) {
    return fieldList(row, [
        ['Correo', row.email],
        ['Cédula profesional', row.cedula],
        ['Especialidad', row.especialidad],
        ['Universidad', row.universidad],
        ['Razón social', row.razon_social],
        ['Domicilio', row.domicilio],
        ['Teléfono', row.telefono],
        ['Recomendado por', row.recomendado_por],
        ['Términos aceptados', row.terminos_version ? `${row.terminos_version} · ${fechaCorta(row.terminos_aceptados_at)}` : ''],
        ['Recibida', fechaCorta(row.created_at)],
    ]);
}

// ── lists ───────────────────────────────────────────────────────────────

async function loadList(estado) {
    saState.estado = estado;
    document.querySelectorAll('.sa-tab').forEach(t => {
        t.setAttribute('aria-selected', t.dataset.estado === estado ? 'true' : 'false');
    });
    const list = document.getElementById('saList');
    list.replaceChildren();
    setStatus('Cargando...');

    let res, data;
    try {
        ({ res, data } = await saFetch(`/api/superadmin/solicitudes?estado=${encodeURIComponent(estado)}`));
    } catch {
        setStatus('Error de red. Revisa tu conexión e intenta de nuevo.');
        return;
    }
    if (!res.ok) {
        setStatus(data.error || 'No se pudo cargar la lista.');
        return;
    }
    if (saState.estado !== estado) return; // another tab was clicked meanwhile
    saState.plantilla = data.plantilla_rechazo || saState.plantilla;
    saState.maxMotivo = data.max_motivo || saState.maxMotivo;

    const rows = data.solicitudes || [];
    if (!rows.length) {
        setStatus({ pendiente: 'No hay solicitudes pendientes.', aprobada: 'No hay solicitudes aprobadas.',
                    rechazada: 'No hay solicitudes rechazadas.' }[estado]);
        return;
    }
    setStatus(`${rows.length} ${rows.length === 1 ? 'solicitud' : 'solicitudes'}`);
    const render = { pendiente: renderPendiente, aprobada: renderAprobada, rechazada: renderRechazada }[estado];
    rows.forEach(row => list.appendChild(render(row)));
}

function renderPendiente(row) {
    const card = el('article', 'sa-card');
    const badges = [badge(diasTexto(row.dias), 'muted')];
    if (row.atascada) badges.unshift(badge('Aprobación a medias', 'warn'));
    card.appendChild(cardHeader(row, badges));
    card.appendChild(solicitudFields(row));

    const panel = el('div', 'sa-panel');
    const actions = el('div', 'sa-actions');
    if (row.atascada) {
        actions.appendChild(button('Reintentar aprobación', 'btn btn-primary sa-btn', () => openApprove(row, panel, actions)));
    } else {
        actions.appendChild(button('Aprobar', 'btn btn-primary sa-btn', () => openApprove(row, panel, actions)));
        actions.appendChild(button('Rechazar', 'btn btn-secondary sa-btn', () => openReject(row, panel, actions)));
    }
    card.appendChild(actions);
    card.appendChild(panel);
    return card;
}

function renderAprobada(row) {
    const card = el('article', 'sa-card');
    const badges = [];
    if (row.invitacion_aceptada === true) badges.push(badge('Invitación aceptada', 'ok'));
    else if (row.invitacion_aceptada === false) badges.push(badge('Invitación no aceptada', 'warn'));
    else badges.push(badge('Estado de invitación desconocido', 'muted'));
    badges.push(badge(`aprobada ${diasTexto(row.dias_desde_aprobacion)}`, 'muted'));
    card.appendChild(cardHeader(row, badges));
    card.appendChild(fieldList(row, [
        ['Rol otorgado', row.rol_otorgado_label],
        ['Clínica', (row.clinica_nombre || row.razon_social || '') + (row.clinica_creada ? ' (creada en la aprobación)' : '')],
        ['Correo', row.email],
        ['Aprobada', fechaCorta(row.revisado_at)],
    ]));

    const panel = el('div', 'sa-panel');
    if (row.invitacion_aceptada === false) {
        const actions = el('div', 'sa-actions');
        actions.appendChild(button('Reenviar invitación', 'btn btn-primary sa-btn', (e) => resend(row, e.currentTarget, panel)));
        actions.appendChild(button('Eliminar', 'btn btn-secondary sa-btn sa-btn-danger', () => openDelete(row, panel, actions)));
        card.appendChild(actions);
    }
    card.appendChild(panel);
    return card;
}

function renderRechazada(row) {
    const card = el('article', 'sa-card');
    card.appendChild(cardHeader(row, [badge(`rechazada ${diasTexto(row.dias)}`, 'muted')]));
    card.appendChild(fieldList(row, [
        ['Correo', row.email],
        ['Razón social', row.razon_social],
        ['Rechazada', fechaCorta(row.revisado_at)],
    ]));
    if (row.motivo_rechazo) {
        card.appendChild(el('p', 'sa-label', 'Motivo'));
        card.appendChild(el('p', 'sa-motivo', row.motivo_rechazo));
    }
    card.appendChild(el('p', 'sa-hint', 'Se borra automáticamente 30 días después de rechazarla.'));
    return card;
}

// ── approve ─────────────────────────────────────────────────────────────

async function getClinicas(q) {
    const { res, data } = await saFetch(`/api/superadmin/clinicas?q=${encodeURIComponent(q || '')}`);
    if (!res.ok) throw new Error(data.error || 'No se pudo cargar la lista de clínicas.');
    return data;
}

function rolesPermitidos(row) {
    const roles = [];
    if (row.cedula && row.especialidad) roles.push('medico', 'admin');
    if (row.rol_solicitado === 'auxiliar') roles.push('auxiliar');
    return roles;
}

async function openApprove(row, panel, actions) {
    actions.hidden = true;
    panel.replaceChildren(el('p', 'sa-hint', 'Cargando clínicas...'));

    let clinicasData;
    try {
        clinicasData = await getClinicas(row.razon_social);
    } catch (err) {
        panel.replaceChildren(el('p', 'sa-error', err.message || 'Error de red.'));
        panel.appendChild(button('Cerrar', 'btn btn-secondary sa-btn', () => { panel.replaceChildren(); actions.hidden = false; }));
        return;
    }

    const form = el('div', 'sa-form');
    const resumida = row.atascada && row.clinica_id;
    const uid = `sa-${row.id}`;

    // Clinic: new or existing
    form.appendChild(el('p', 'sa-label', 'Clínica'));
    const opciones = el('div', 'sa-choice-group');
    const nuevaLabel = el('label', 'sa-choice');
    const nuevaRadio = el('input'); nuevaRadio.type = 'radio'; nuevaRadio.name = `${uid}-clinica`; nuevaRadio.value = 'nueva';
    nuevaLabel.appendChild(nuevaRadio);
    nuevaLabel.appendChild(el('span', null, `Crear clínica nueva: ${row.razon_social}`));
    const existLabel = el('label', 'sa-choice');
    const existRadio = el('input'); existRadio.type = 'radio'; existRadio.name = `${uid}-clinica`; existRadio.value = 'existente';
    existLabel.appendChild(existRadio);
    existLabel.appendChild(el('span', null, 'Agregar a una clínica existente'));
    opciones.appendChild(nuevaLabel);
    opciones.appendChild(existLabel);
    form.appendChild(opciones);

    const select = el('select', 'sa-select');
    select.setAttribute('aria-label', 'Clínica existente');
    select.appendChild(new Option('Elige una clínica...', ''));
    (clinicasData.clinicas || []).forEach(c => {
        const label = c.nombre + (c.direccion ? ` — ${c.direccion}` : '') + (c.id === clinicasData.sugerida ? '  (sugerida)' : '');
        select.appendChild(new Option(label, c.id));
    });
    form.appendChild(select);
    const sugerencia = el('p', 'sa-hint');
    form.appendChild(sugerencia);

    // Rol
    form.appendChild(el('p', 'sa-label', 'Rol que se otorga'));
    const rolSelect = el('select', 'sa-select');
    rolSelect.setAttribute('aria-label', 'Rol que se otorga');
    rolesPermitidos(row).forEach(r => rolSelect.appendChild(new Option(ROL_LABELS[r] || r, r)));
    form.appendChild(rolSelect);

    // Admin confirmation (existing clinic + admin/auxiliar)
    const confirmLabel = el('label', 'sa-check');
    const confirmBox = el('input'); confirmBox.type = 'checkbox';
    confirmLabel.appendChild(confirmBox);
    confirmLabel.appendChild(el('span', null, 'Ya lo confirmé con el administrador actual de esa clínica.'));
    form.appendChild(confirmLabel);

    const error = el('p', 'sa-error');
    error.setAttribute('role', 'alert');
    form.appendChild(error);

    // Initial values: a resumed approval keeps what it started with.
    const preferido = row.rol_otorgado || (rolesPermitidos(row).includes(row.rol_solicitado) ? row.rol_solicitado : rolSelect.options[0]?.value);
    if (preferido) rolSelect.value = preferido;
    if (resumida) {
        if (row.clinica_creada) nuevaRadio.checked = true;
        else { existRadio.checked = true; select.value = row.clinica_id; }
        nuevaRadio.disabled = existRadio.disabled = select.disabled = rolSelect.disabled = true;
        sugerencia.textContent = 'Reintento: se usan la clínica y el rol con que empezó la aprobación.';
    } else if (row.atascada && row.rol_otorgado) {
        rolSelect.disabled = true;
    }
    if (!resumida) {
        if (clinicasData.sugerida) {
            existRadio.checked = true;
            select.value = clinicasData.sugerida;
            sugerencia.textContent = 'Sugerida por parecido con la razón social. Revisa que sea la correcta.';
        } else {
            nuevaRadio.checked = true;
            sugerencia.textContent = 'Ninguna clínica existente se parece a la razón social.';
        }
    }

    function refresh() {
        const existente = existRadio.checked;
        select.hidden = !existente;
        if (!resumida && clinicasData.sugerida) sugerencia.hidden = !existente;
        // A new clinic needs an admin: Médico is unavailable while "nueva"
        // is chosen (disabled, not hidden — iOS Safari ignores hidden options).
        [...rolSelect.options].forEach(o => { o.disabled = !existente && !isAdminRole(o.value); });
        if (!existente && !isAdminRole(rolSelect.value)) {
            const firstAdmin = [...rolSelect.options].find(o => isAdminRole(o.value));
            if (firstAdmin) rolSelect.value = firstAdmin.value;
        }
        confirmLabel.hidden = !(existente && isAdminRole(rolSelect.value));
    }
    [nuevaRadio, existRadio, rolSelect].forEach(n => n.addEventListener('change', refresh));
    refresh();

    const buttons = el('div', 'sa-actions');
    const submit = button(row.atascada ? 'Reintentar aprobación' : 'Aprobar', 'btn btn-primary sa-btn', async () => {
        error.textContent = '';
        if (!rolSelect.value) { error.textContent = 'Esta solicitud no permite ningún rol.'; return; }
        const existente = existRadio.checked;
        if (existente && !select.value) { error.textContent = 'Elige la clínica.'; select.focus(); return; }
        submit.disabled = true;
        submit.textContent = 'Aprobando...';
        try {
            const { res, data } = await saFetch(`/api/superadmin/solicitudes/${encodeURIComponent(row.id)}/aprobar`, {
                method: 'POST',
                body: {
                    clinica_id: existente ? select.value : null,
                    rol_otorgado: rolSelect.value,
                    confirma_admin: confirmBox.checked,
                },
            });
            if (!res.ok) {
                error.textContent = data.error || 'No se pudo aprobar.';
                if (res.status === 503) {
                    // Part of it may be done: reload so the card shows the retry.
                    reloadSoon(2500, true);
                }
                return;
            }
            panel.replaceChildren(approveResult(data));
            reloadSoon(4000);
        } catch {
            error.textContent = 'Error de red. Revisa tu conexión e intenta de nuevo.';
        } finally {
            submit.disabled = false;
            submit.textContent = row.atascada ? 'Reintentar aprobación' : 'Aprobar';
        }
    });
    buttons.appendChild(submit);
    buttons.appendChild(button('Cancelar', 'btn btn-secondary sa-btn', () => { panel.replaceChildren(); actions.hidden = false; }));
    form.appendChild(buttons);
    panel.replaceChildren(form);
}

function approveResult(data) {
    const box = el('div', data.correo_invitacion_enviado ? 'sa-result sa-result-ok' : 'sa-result sa-result-warn');
    box.setAttribute('role', 'status');
    box.appendChild(el('p', null, data.correo_invitacion_enviado
        ? 'Aprobada. Se envió la invitación.'
        : (data.warning || 'Aprobada, pero el correo no se envió — usa Reenviar.')));
    if (data.clinica_creada) box.appendChild(el('p', null, 'Se creó la clínica nueva.'));
    const avisos = data.avisos_admins;
    if (avisos) {
        if (avisos.fallidos === null) box.appendChild(el('p', null, 'No se pudo avisar a los administradores de la clínica.'));
        else if (avisos.fallidos) box.appendChild(el('p', null, `Se avisó a ${avisos.enviados} administrador(es); ${avisos.fallidos} aviso(s) no se enviaron.`));
        else if (avisos.enviados) box.appendChild(el('p', null, `Se avisó a ${avisos.enviados} administrador(es) de la clínica.`));
    }
    return box;
}

// ── reject ──────────────────────────────────────────────────────────────

// Same split/join substitution as email_service.render_rejection_text, so
// the preview is the exact text the server sends (no $-patterns, braces
// in the motivo kept literally).
function renderRechazo(nombre, motivo) {
    const p = saState.plantilla;
    if (!p) return '';
    const bloque = motivo ? p.bloque_motivo.split('{motivo}').join(motivo) : '';
    return p.cuerpo.split('{nombre}').join(nombre).split('{bloque_motivo}').join(bloque);
}

function openReject(row, panel, actions) {
    actions.hidden = true;
    const form = el('div', 'sa-form');
    const id = `sa-motivo-${row.id}`;
    const label = el('label', 'sa-label', 'Motivo (opcional, se incluye en el correo)');
    label.htmlFor = id;
    form.appendChild(label);
    const textarea = el('textarea', 'sa-textarea');
    textarea.id = id;
    textarea.rows = 4;
    textarea.maxLength = saState.maxMotivo;
    form.appendChild(textarea);
    const counter = el('p', 'sa-hint');
    form.appendChild(counter);

    form.appendChild(el('p', 'sa-label', 'Vista previa del correo'));
    const preview = el('div', 'sa-preview');
    const asunto = el('p', 'sa-preview-subject');
    const cuerpo = el('p', 'sa-preview-body');
    preview.appendChild(asunto);
    preview.appendChild(cuerpo);
    form.appendChild(preview);
    form.appendChild(el('p', 'sa-hint', 'Texto en borrador, pendiente de revisión legal.'));

    const error = el('p', 'sa-error');
    error.setAttribute('role', 'alert');
    form.appendChild(error);

    function update() {
        const motivo = textarea.value.trim();
        asunto.textContent = `Asunto: ${saState.plantilla ? saState.plantilla.asunto : ''}`;
        cuerpo.textContent = renderRechazo(row.nombre, motivo);
        counter.textContent = `${textarea.value.length} / ${saState.maxMotivo}`;
    }
    textarea.addEventListener('input', update);
    update();

    async function rechazar(enviar, btn) {
        error.textContent = '';
        const motivo = textarea.value.trim();
        btn.disabled = true;
        try {
            const { res, data } = await saFetch(`/api/superadmin/solicitudes/${encodeURIComponent(row.id)}/rechazar`, {
                method: 'POST',
                body: { motivo: motivo || null, enviar_correo: enviar },
            });
            if (!res.ok) { error.textContent = data.error || 'No se pudo rechazar.'; return; }
            let msg = 'Rechazada. No se envió correo.';
            if (enviar) msg = data.correo_enviado ? 'Rechazada. Se envió el correo.' : 'Rechazada, pero el correo no se pudo enviar.';
            panel.replaceChildren(el('div', data.correo_enviado === false ? 'sa-result sa-result-warn' : 'sa-result sa-result-ok', msg));
            reloadSoon(3000);
        } catch {
            error.textContent = 'Error de red. Revisa tu conexión e intenta de nuevo.';
        } finally {
            btn.disabled = false;
        }
    }

    const buttons = el('div', 'sa-actions');
    const enviarBtn = button('Rechazar y enviar correo', 'btn btn-primary sa-btn sa-btn-danger', () => rechazar(true, enviarBtn));
    const sinBtn = button('Rechazar sin enviar correo', 'btn btn-secondary sa-btn', () => rechazar(false, sinBtn));
    buttons.appendChild(enviarBtn);
    buttons.appendChild(sinBtn);
    buttons.appendChild(button('Cancelar', 'btn btn-secondary sa-btn', () => { panel.replaceChildren(); actions.hidden = false; }));
    form.appendChild(buttons);
    panel.replaceChildren(form);
    textarea.focus();
}

// ── resend / delete ─────────────────────────────────────────────────────

async function resend(row, btn, panel) {
    btn.disabled = true;
    const original = btn.textContent;
    btn.textContent = 'Enviando...';
    try {
        const { res, data } = await saFetch(`/api/superadmin/solicitudes/${encodeURIComponent(row.id)}/reenviar`, { method: 'POST', body: {} });
        panel.replaceChildren(el('div', res.ok ? 'sa-result sa-result-ok' : 'sa-result sa-result-warn',
            res.ok ? 'Invitación reenviada.' : (data.error || 'No se pudo reenviar.')));
    } catch {
        panel.replaceChildren(el('div', 'sa-result sa-result-warn', 'Error de red. Revisa tu conexión e intenta de nuevo.'));
    } finally {
        btn.disabled = false;
        btn.textContent = original;
    }
}

function openDelete(row, panel, actions) {
    actions.hidden = true;
    const box = el('div', 'sa-form sa-confirm');
    box.setAttribute('role', 'alertdialog');
    let texto = `Se eliminará la cuenta de ${row.nombre}`;
    if (row.clinica_se_elimina) texto += ` y la clínica ${row.clinica_nombre || row.razon_social}`;
    texto += ', junto con esta solicitud. No se puede deshacer.';
    box.appendChild(el('p', 'sa-confirm-text', texto));
    const error = el('p', 'sa-error');
    error.setAttribute('role', 'alert');
    box.appendChild(error);

    const buttons = el('div', 'sa-actions');
    const yes = button('Sí, eliminar', 'btn btn-primary sa-btn sa-btn-danger', async () => {
        error.textContent = '';
        yes.disabled = true;
        try {
            const { res, data } = await saFetch(`/api/superadmin/solicitudes/${encodeURIComponent(row.id)}/eliminar`, { method: 'POST', body: {} });
            if (!res.ok) { error.textContent = data.error || 'No se pudo eliminar.'; return; }
            panel.replaceChildren(el('div', 'sa-result sa-result-ok',
                data.clinica_eliminada ? 'Eliminada, junto con su clínica.' : 'Eliminada.'));
            reloadSoon(2500);
        } catch {
            error.textContent = 'Error de red. Revisa tu conexión e intenta de nuevo.';
        } finally {
            yes.disabled = false;
        }
    });
    const no = button('Cancelar', 'btn btn-secondary sa-btn', () => { panel.replaceChildren(); actions.hidden = false; });
    buttons.appendChild(yes);
    buttons.appendChild(no);
    box.appendChild(buttons);
    panel.replaceChildren(box);
    no.focus();
}

// ── boot ────────────────────────────────────────────────────────────────

document.addEventListener('DOMContentLoaded', () => {
    const logoutBtn = document.getElementById('logoutBtn');
    if (logoutBtn) {
        logoutBtn.addEventListener('click', (e) => { e.preventDefault(); logout(); });
    }
    document.querySelectorAll('.sa-tab').forEach(tab => {
        tab.addEventListener('click', () => loadList(tab.dataset.estado));
    });
});

// Same guard as admin.js: no session → /login; a valid session without
// "superadmin": true goes back to / without showing anything.
window.addEventListener('pageshow', async function (event) {
    const { data: { session } } = await supabaseClient.auth.getSession();
    if (!session) {
        window.location.href = '/login';
        return;
    }
    if (event.persisted) document.body.classList.add('auth-pending');

    fetch(`${window.location.origin}/api/session-check`, {
        headers: await getAuthHeaders()
    }).then(async res => {
        if (res.status === 401) { handleSessionExpired(); return; }
        const data = await res.json();
        if (!data.superadmin) { window.location.replace('/'); return; }
        document.body.classList.remove('auth-pending');
        loadList(saState.estado);
    }).catch(() => {
        // Network hiccup on the check itself: nothing is revealed; reload retries.
    });
});
