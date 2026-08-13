// frontend/session-detail-render.js
// Shared read-only session detail rendering, used by both:
//   - index.html's Historial view (item 24) — { isAdmin: false, canDownloadPdf: true, onDownloadPdf }
//   - admin.html's ARCO session detail (ADM-1 Stage E) — { isAdmin: true, canDownloadPdf: true, onDownloadPdf }
// isAdmin and canDownloadPdf are independent: isAdmin gates admin-only
// actions (Stage F/G), canDownloadPdf gates the PDF button regardless of
// caller role — a doctor viewing their own session and an admin viewing
// any clinic session both want that button, via two different backend
// authorization paths (owner-only vs clinic-wide admin route).
//
// generateFormattedHTML() extracted from app.js unchanged — confirmed pure
// display markup (no inputs/textareas/listeners), safe to reuse verbatim.

function generateFormattedHTML(data) {
    let html = '';
    const e = escapeHtml;

    // Patient Information
    if (data.informacion_paciente) {
        html += '<div class="soap-section">';
        html += '<h5>👤 INFORMACIÓN DEL PACIENTE</h5>';

        const info = data.informacion_paciente;
        if (info.nombre_del_paciente) {
            html += `<p><strong>Nombre:</strong> ${e(info.nombre_del_paciente)}</p>`;
        }
        if (info.fecha_de_nacimiento) {
            html += `<p><strong>Fecha de Nacimiento:</strong> ${e(info.fecha_de_nacimiento)}</p>`;
        }
        if (info.curp) {
            html += `<p><strong>CURP:</strong> ${e(info.curp)}</p>`;
        }
        if (info.edad) {
            html += `<p><strong>Edad:</strong> ${e(info.edad)}</p>`;
        }
        if (info.genero) {
            html += `<p><strong>Género:</strong> ${e(info.genero)}</p>`;
        }

        html += '</div>';
    }

    // Subjective
    if (data.subjetivo) {
        html += '<div class="soap-section">';
        html += '<h5>📋 SUBJETIVO (S)</h5>';

        const subj = data.subjetivo;

        if (subj.motivo_de_consulta) {
            html += '<h6>Motivo de Consulta</h6>';
            html += `<p>${e(subj.motivo_de_consulta)}</p>`;
        }

        if (subj.sintomas && subj.sintomas.length > 0) {
            html += '<h6>Síntomas</h6>';
            html += '<ul>';
            subj.sintomas.forEach(s => html += `<li>${e(s)}</li>`);
            html += '</ul>';
        }

        if (subj.historia_de_enfermedad_actual) {
            html += '<h6>Historia de Enfermedad Actual</h6>';
            html += `<p>${e(subj.historia_de_enfermedad_actual)}</p>`;
        }

        if (subj.duracion_sintomas) {
            html += `<p><strong>Duración:</strong> ${e(subj.duracion_sintomas)}</p>`;
        }

        html += '</div>';
    }

    // Objective
    if (data.objetivo) {
        html += '<div class="soap-section">';
        html += '<h5>🔬 OBJETIVO (O)</h5>';

        const obj = data.objetivo;

        if (obj.signos_vitales) {
            html += '<h6>Signos Vitales</h6>';
            const vitals = obj.signos_vitales;
            if (vitals.presion_arterial) html += `<p><strong>Presión Arterial:</strong> ${e(vitals.presion_arterial)}</p>`;
            if (vitals.frecuencia_cardiaca) html += `<p><strong>Frecuencia Cardíaca:</strong> ${e(vitals.frecuencia_cardiaca)}</p>`;
            if (vitals.temperatura) html += `<p><strong>Temperatura:</strong> ${e(vitals.temperatura)}</p>`;
            if (vitals.frecuencia_respiratoria) html += `<p><strong>Frecuencia Respiratoria:</strong> ${e(vitals.frecuencia_respiratoria)}</p>`;
            if (vitals.saturacion_oxigeno) html += `<p><strong>Saturación de Oxígeno:</strong> ${e(vitals.saturacion_oxigeno)}</p>`;
        }

        if (obj.examen_fisico) {
            html += '<h6>Examen Físico</h6>';
            html += `<p>${e(obj.examen_fisico)}</p>`;
        }

        if (obj.hallazgos && obj.hallazgos.length > 0) {
            html += '<h6>Hallazgos</h6>';
            html += '<ul>';
            obj.hallazgos.forEach(h => html += `<li>${e(h)}</li>`);
            html += '</ul>';
        }

        html += '</div>';
    }

    // Assessment
    if (data.evaluacion) {
        html += '<div class="soap-section">';
        html += '<h5>🩺 EVALUACIÓN (A)</h5>';

        const eval_data = data.evaluacion;

        if (eval_data.diagnostico) {
            html += '<h6>Diagnóstico Principal</h6>';
            html += `<p><strong>${e(eval_data.diagnostico)}</strong></p>`;
        }

        if (eval_data.diagnosticos_adicionales && eval_data.diagnosticos_adicionales.length > 0) {
            html += '<h6>Diagnósticos Adicionales</h6>';
            html += '<ul>';
            eval_data.diagnosticos_adicionales.forEach(d => html += `<li>${e(d)}</li>`);
            html += '</ul>';
        }

        if (eval_data.impresion_clinica) {
            html += '<h6>Impresión Clínica</h6>';
            html += `<p>${e(eval_data.impresion_clinica)}</p>`;
        }

        html += '</div>';
    }

    // Plan
    if (data.plan) {
        html += '<div class="soap-section">';
        html += '<h5>💊 PLAN (P)</h5>';

        const plan = data.plan;

        if (plan.tratamiento) {
            html += '<h6>Tratamiento</h6>';
            html += `<p>${e(plan.tratamiento)}</p>`;
        }

        if (plan.medicamentos && plan.medicamentos.length > 0) {
            html += '<h6>Medicamentos Prescritos</h6>';
            html += '<ul>';
            plan.medicamentos.forEach(med => {
                let medText = e(med.nombre || 'Medicamento');
                if (med.dosis) medText += ` - ${e(med.dosis)}`;
                if (med.frecuencia) medText += `, ${e(med.frecuencia)}`;
                if (med.duracion) medText += ` por ${e(med.duracion)}`;
                html += `<li>${medText}</li>`;
            });
            html += '</ul>';
        }

        if (plan.recomendaciones && plan.recomendaciones.length > 0) {
            html += '<h6>Recomendaciones</h6>';
            html += '<ul>';
            plan.recomendaciones.forEach(r => html += `<li>${e(r)}</li>`);
            html += '</ul>';
        }

        if (plan.estudios_solicitados && plan.estudios_solicitados.length > 0) {
            html += '<h6>Estudios Solicitados</h6>';
            html += '<ul>';
            plan.estudios_solicitados.forEach(e2 => html += `<li>${e(e2)}</li>`);
            html += '</ul>';
        }

        if (plan.seguimiento) {
            html += '<h6>Seguimiento</h6>';
            html += `<p>${e(plan.seguimiento)}</p>`;
        }

        html += '</div>';
    }

    // Metadata
    if (data.metadata) {
        html += '<div class="soap-section">';
        html += '<h5>ℹ️ INFORMACIÓN DE LA CONSULTA</h5>';

        const meta = data.metadata;
        if (meta.fecha_consulta) html += `<p><strong>Fecha:</strong> ${e(meta.fecha_consulta)}</p>`;
        if (meta.medico) html += `<p><strong>Médico:</strong> ${e(meta.medico)}</p>`;
        if (meta.duracion_consulta) html += `<p><strong>Duración:</strong> ${e(meta.duracion_consulta)}</p>`;

        html += '</div>';
    }

    return html;
}

/**
 * Renders read-only session detail into `container` — extracted from
 * item 24's openHistoryDetail() so index.html and admin.html share one
 * implementation instead of parallel copies.
 *
 * @param {HTMLElement} container - element to render into (innerHTML replaced)
 * @param {object} sessionData - response from GET /api/patient-history/<id>
 *   ({ session_id, timestamp, structured_data, addenda, autor_nombre? })
 * @param {object} options
 * @param {boolean} [options.isAdmin=false] - gates admin-only actions
 *   (Stage F/G's addendum-write and cancel buttons, when built). Does
 *   NOT gate PDF download — see canDownloadPdf, a separate authorization
 *   path (doctor-owner vs admin-clinic-wide) that happens to want the
 *   same button.
 * @param {boolean} [options.canDownloadPdf=false] - independently gates
 *   the "Descargar PDF" button's visibility, regardless of isAdmin.
 * @param {function} [options.onDownloadPdf] - called when "Descargar PDF"
 *   is clicked. Only wired up if canDownloadPdf is true.
 * @param {function} [options.onAddAddendum] - async function(texto) called
 *   when the admin submits a new adendum. Only rendered/wired when
 *   isAdmin is true — doctor-facing Historial (isAdmin: false) never
 *   shows this, stays fully read-only. Should perform the POST and
 *   throw on failure (caught here to show an inline error); on success
 *   the CALLER is responsible for re-fetching session data and calling
 *   renderSessionDetail() again with the updated addenda — this function
 *   stays stateless rather than mutating its own DOM in place.
 * @param {function} [options.onCancel] - async function(cancellation_reason)
 *   called when the admin confirms cancellation. Only rendered/wired
 *   when isAdmin is true AND sessionData.status !== 'cancelled'. The
 *   confirm button stays disabled until a non-empty reason is typed —
 *   deliberately harder to trigger than the addendum flow, given this
 *   action is irreversible. Should perform the POST and throw on
 *   failure (caught here to show an inline error); on success the
 *   CALLER re-fetches and re-invokes renderSessionDetail(), same
 *   stateless pattern as onAddAddendum.
 */
function renderSessionDetail(container, sessionData, options = {}) {
    const { isAdmin = false, canDownloadPdf = false, onDownloadPdf, onAddAddendum, onCancel } = options;
    const sd = sessionData.structured_data || {};
    const e = escapeHtml;

    let html = `<h3 style="margin-bottom: 0.25rem;">Nota de Consulta</h3>`;

    // Session identifier + fully-formatted date/time — renders for every
    // caller regardless of isAdmin/canDownloadPdf, since this is plain
    // information display (closes the gap where a doctor had no way to
    // reference a specific session's identifier for ARCO purposes).
    const fechaCompleta = sessionData.timestamp
        ? new Date(sessionData.timestamp).toLocaleString('es-MX', {
            day: '2-digit', month: 'long', year: 'numeric',
            hour: '2-digit', minute: '2-digit', second: '2-digit'
          })
        : '—';
    const statusLabel = sessionData.status === 'confirmed' ? 'Confirmada'
        : sessionData.status === 'cancelled' ? 'Cancelada'
        : sessionData.status === 'pending_review' ? 'Pendiente'
        : (e(sessionData.status) || '—');
    const statusClass = sessionData.status === 'confirmed' ? 'status-confirmed'
        : sessionData.status === 'cancelled' ? 'status-cancelled'
        : 'status-other';
    html += `<p style="color: var(--text-secondary); font-size: 0.85rem; margin-bottom: 1rem;">
        ID de sesión: <strong>${e(sessionData.session_id) || '—'}</strong> · ${fechaCompleta}
        · <span id="sessionDetailStatusBadge" class="historial-status ${statusClass}">${statusLabel}</span>
    </p>`;

    if (sessionData.autor_nombre) {
        html += `<div class="historial-autor-banner">Nota escrita por Dr(a). ${e(sessionData.autor_nombre)}</div>`;
    }

    if (canDownloadPdf) {
        html += `<div class="admin-detail-actions" style="margin-bottom: 1rem; display: flex; gap: 0.5rem;">
            <button id="adminDetailDownloadPdfBtn" class="btn btn-secondary btn-small" style="max-width: none; width: auto;">Descargar PDF</button>
        </div>`;
    }

    html += generateFormattedHTML(sd);

    const addenda = sessionData.addenda || [];
    if (addenda.length) {
        html += '<div class="soap-section" style="margin-top: 1.5rem;">';
        html += '<h5>📝 ADENDA</h5>';
        addenda.forEach(a => {
            const fecha = a.timestamp ? new Date(a.timestamp).toLocaleDateString('es-MX', { day: '2-digit', month: 'short', year: 'numeric', hour: '2-digit', minute: '2-digit' }) : '—';
            html += `<div class="adenda-entry">
                <div class="adenda-meta"><strong>${e(a.author) || 'Médico'}</strong> · ${fecha}</div>
                <p class="adenda-text">${e(a.text)}</p>
            </div>`;
        });
        html += '</div>';
    }

    // Admin-only "Agregar adendum" affordance — inline (not a modal),
    // consistent with the rest of this view. Doctor-facing Historial
    // (isAdmin: false) never sees this; it stays fully read-only there
    // per item 24's original design.
    if (isAdmin && typeof onAddAddendum === 'function') {
        html += `<div class="admin-add-addendum" style="margin-top: 1rem;">
            <button id="showAddAddendumBtn" class="btn btn-secondary btn-small" style="max-width: none; width: auto;">Agregar adendum</button>
            <div id="addAddendumForm" style="display: none; margin-top: 0.75rem;">
                <label class="review-label" for="addAddendumTextarea">Texto del adendum</label>
                <textarea id="addAddendumTextarea" class="review-input" rows="3" maxlength="2000" style="width: 100%;"></textarea>
                <div id="addAddendumError" style="display: none; color: #c0392b; font-size: 0.85rem; margin-top: 0.5rem;"></div>
                <div style="display: flex; gap: 0.5rem; margin-top: 0.75rem;">
                    <button id="submitAddendumBtn" class="btn btn-primary btn-small" style="max-width: none; width: auto;">Guardar adendum</button>
                    <button id="cancelAddendumBtn" class="btn btn-secondary btn-small" style="max-width: none; width: auto;">Cancelar</button>
                </div>
            </div>
        </div>`;
    }

    // Admin-only session cancellation — irreversible, so deliberately
    // harder to trigger than the addendum flow: a typed, non-empty reason
    // is required before the confirm button is even enabled (not a bare
    // confirm()/cancel dialog). Hidden entirely once already cancelled.
    if (isAdmin && typeof onCancel === 'function' && sessionData.status !== 'cancelled') {
        html += `<div class="admin-cancel-session" style="margin-top: 1.5rem; padding-top: 1rem; border-top: 1px solid var(--border-color);">
            <button id="showCancelSessionBtn" class="btn btn-danger btn-small" style="max-width: none; width: auto;">Cancelar nota</button>
            <div id="cancelSessionForm" style="display: none; margin-top: 0.75rem;">
                <p style="color: #c0392b; font-size: 0.85rem; margin-bottom: 0.5rem;">
                    Esta acción es irreversible. Escribe el motivo de la cancelación para continuar.
                </p>
                <label class="review-label" for="cancelSessionReasonTextarea">Motivo de cancelación</label>
                <textarea id="cancelSessionReasonTextarea" class="review-input" rows="3" style="width: 100%;"></textarea>
                <div id="cancelSessionError" style="display: none; color: #c0392b; font-size: 0.85rem; margin-top: 0.5rem;"></div>
                <div style="display: flex; gap: 0.5rem; margin-top: 0.75rem;">
                    <button id="confirmCancelSessionBtn" class="btn btn-danger btn-small" style="max-width: none; width: auto;" disabled>Confirmar cancelación</button>
                    <button id="dismissCancelSessionBtn" class="btn btn-secondary btn-small" style="max-width: none; width: auto;">Volver</button>
                </div>
            </div>
        </div>`;
    }

    container.innerHTML = html;

    if (canDownloadPdf && typeof onDownloadPdf === 'function') {
        const btn = container.querySelector('#adminDetailDownloadPdfBtn');
        if (btn) btn.addEventListener('click', onDownloadPdf);
    }

    if (isAdmin && typeof onAddAddendum === 'function') {
        const showBtn = container.querySelector('#showAddAddendumBtn');
        const form = container.querySelector('#addAddendumForm');
        const textarea = container.querySelector('#addAddendumTextarea');
        const errorEl = container.querySelector('#addAddendumError');
        const submitBtn = container.querySelector('#submitAddendumBtn');
        const cancelBtn = container.querySelector('#cancelAddendumBtn');

        showBtn.addEventListener('click', () => {
            form.style.display = 'block';
            showBtn.style.display = 'none';
        });

        cancelBtn.addEventListener('click', () => {
            form.style.display = 'none';
            showBtn.style.display = '';
            textarea.value = '';
            errorEl.style.display = 'none';
        });

        submitBtn.addEventListener('click', async () => {
            const texto = textarea.value.trim();
            errorEl.style.display = 'none';

            if (!texto) {
                errorEl.textContent = 'El texto del adendum no puede estar vacío.';
                errorEl.style.display = 'block';
                return;
            }

            submitBtn.disabled = true;
            submitBtn.textContent = 'Guardando...';

            try {
                await onAddAddendum(texto);
                // Caller (admin.js) is responsible for re-fetching and
                // re-invoking renderSessionDetail() with the updated
                // sessionData — this function stays stateless/re-render-
                // based rather than mutating its own DOM in place.
            } catch (err) {
                errorEl.textContent = err && err.message ? err.message : 'Error al guardar el adendum.';
                errorEl.style.display = 'block';
                submitBtn.disabled = false;
                submitBtn.textContent = 'Guardar adendum';
            }
        });
    }

    if (isAdmin && typeof onCancel === 'function' && sessionData.status !== 'cancelled') {
        const showBtn = container.querySelector('#showCancelSessionBtn');
        const form = container.querySelector('#cancelSessionForm');
        const textarea = container.querySelector('#cancelSessionReasonTextarea');
        const errorEl = container.querySelector('#cancelSessionError');
        const confirmBtn = container.querySelector('#confirmCancelSessionBtn');
        const dismissBtn = container.querySelector('#dismissCancelSessionBtn');

        showBtn.addEventListener('click', () => {
            form.style.display = 'block';
            showBtn.style.display = 'none';
        });

        dismissBtn.addEventListener('click', () => {
            form.style.display = 'none';
            showBtn.style.display = '';
            textarea.value = '';
            errorEl.style.display = 'none';
            confirmBtn.disabled = true;
        });

        // Confirm button stays disabled until a non-empty reason is typed —
        // deliberately harder to trigger than the addendum flow, given
        // this action is irreversible.
        textarea.addEventListener('input', () => {
            confirmBtn.disabled = textarea.value.trim().length === 0;
        });

        confirmBtn.addEventListener('click', async () => {
            const reason = textarea.value.trim();
            errorEl.style.display = 'none';

            if (!reason) {
                errorEl.textContent = 'El motivo de cancelación no puede estar vacío.';
                errorEl.style.display = 'block';
                return;
            }

            confirmBtn.disabled = true;
            dismissBtn.disabled = true;
            confirmBtn.textContent = 'Cancelando...';

            try {
                await onCancel(reason);
                // Caller re-fetches and re-invokes renderSessionDetail()
                // with the updated status, same stateless pattern as
                // onAddAddendum above.
            } catch (err) {
                errorEl.textContent = err && err.message ? err.message : 'Error al cancelar la nota.';
                errorEl.style.display = 'block';
                confirmBtn.disabled = false;
                dismissBtn.disabled = false;
                confirmBtn.textContent = 'Confirmar cancelación';
            }
        });
    }
}
