// frontend/session-detail-render.js
// Shared read-only session detail rendering, used by both:
//   - index.html's Historial view (item 24) — options: { isAdmin: false }
//   - admin.html's ARCO session detail (ADM-1 Stage E) — { isAdmin: true, onDownloadPdf }
//
// generateFormattedHTML() extracted from app.js unchanged — confirmed pure
// display markup (no inputs/textareas/listeners), safe to reuse verbatim.

function generateFormattedHTML(data) {
    let html = '';

    // Patient Information
    if (data.informacion_paciente) {
        html += '<div class="soap-section">';
        html += '<h5>👤 INFORMACIÓN DEL PACIENTE</h5>';

        const info = data.informacion_paciente;
        if (info.nombre_del_paciente) {
            html += `<p><strong>Nombre:</strong> ${info.nombre_del_paciente}</p>`;
        }
        if (info.fecha_de_nacimiento) {
            html += `<p><strong>Fecha de Nacimiento:</strong> ${info.fecha_de_nacimiento}</p>`;
        }
        if (info.edad) {
            html += `<p><strong>Edad:</strong> ${info.edad}</p>`;
        }
        if (info.genero) {
            html += `<p><strong>Género:</strong> ${info.genero}</p>`;
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
            html += `<p>${subj.motivo_de_consulta}</p>`;
        }

        if (subj.sintomas && subj.sintomas.length > 0) {
            html += '<h6>Síntomas</h6>';
            html += '<ul>';
            subj.sintomas.forEach(s => html += `<li>${s}</li>`);
            html += '</ul>';
        }

        if (subj.historia_de_enfermedad_actual) {
            html += '<h6>Historia de Enfermedad Actual</h6>';
            html += `<p>${subj.historia_de_enfermedad_actual}</p>`;
        }

        if (subj.duracion_sintomas) {
            html += `<p><strong>Duración:</strong> ${subj.duracion_sintomas}</p>`;
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
            if (vitals.presion_arterial) html += `<p><strong>Presión Arterial:</strong> ${vitals.presion_arterial}</p>`;
            if (vitals.frecuencia_cardiaca) html += `<p><strong>Frecuencia Cardíaca:</strong> ${vitals.frecuencia_cardiaca}</p>`;
            if (vitals.temperatura) html += `<p><strong>Temperatura:</strong> ${vitals.temperatura}</p>`;
            if (vitals.frecuencia_respiratoria) html += `<p><strong>Frecuencia Respiratoria:</strong> ${vitals.frecuencia_respiratoria}</p>`;
            if (vitals.saturacion_oxigeno) html += `<p><strong>Saturación de Oxígeno:</strong> ${vitals.saturacion_oxigeno}</p>`;
        }

        if (obj.examen_fisico) {
            html += '<h6>Examen Físico</h6>';
            html += `<p>${obj.examen_fisico}</p>`;
        }

        if (obj.hallazgos && obj.hallazgos.length > 0) {
            html += '<h6>Hallazgos</h6>';
            html += '<ul>';
            obj.hallazgos.forEach(h => html += `<li>${h}</li>`);
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
            html += `<p><strong>${eval_data.diagnostico}</strong></p>`;
        }

        if (eval_data.diagnosticos_adicionales && eval_data.diagnosticos_adicionales.length > 0) {
            html += '<h6>Diagnósticos Adicionales</h6>';
            html += '<ul>';
            eval_data.diagnosticos_adicionales.forEach(d => html += `<li>${d}</li>`);
            html += '</ul>';
        }

        if (eval_data.impresion_clinica) {
            html += '<h6>Impresión Clínica</h6>';
            html += `<p>${eval_data.impresion_clinica}</p>`;
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
            html += `<p>${plan.tratamiento}</p>`;
        }

        if (plan.medicamentos && plan.medicamentos.length > 0) {
            html += '<h6>Medicamentos Prescritos</h6>';
            html += '<ul>';
            plan.medicamentos.forEach(med => {
                let medText = med.nombre || 'Medicamento';
                if (med.dosis) medText += ` - ${med.dosis}`;
                if (med.frecuencia) medText += `, ${med.frecuencia}`;
                if (med.duracion) medText += ` por ${med.duracion}`;
                html += `<li>${medText}</li>`;
            });
            html += '</ul>';
        }

        if (plan.recomendaciones && plan.recomendaciones.length > 0) {
            html += '<h6>Recomendaciones</h6>';
            html += '<ul>';
            plan.recomendaciones.forEach(r => html += `<li>${r}</li>`);
            html += '</ul>';
        }

        if (plan.estudios_solicitados && plan.estudios_solicitados.length > 0) {
            html += '<h6>Estudios Solicitados</h6>';
            html += '<ul>';
            plan.estudios_solicitados.forEach(e => html += `<li>${e}</li>`);
            html += '</ul>';
        }

        if (plan.seguimiento) {
            html += '<h6>Seguimiento</h6>';
            html += `<p>${plan.seguimiento}</p>`;
        }

        html += '</div>';
    }

    // Metadata
    if (data.metadata) {
        html += '<div class="soap-section">';
        html += '<h5>ℹ️ INFORMACIÓN DE LA CONSULTA</h5>';

        const meta = data.metadata;
        if (meta.fecha_consulta) html += `<p><strong>Fecha:</strong> ${meta.fecha_consulta}</p>`;
        if (meta.medico) html += `<p><strong>Médico:</strong> ${meta.medico}</p>`;
        if (meta.duracion_consulta) html += `<p><strong>Duración:</strong> ${meta.duracion_consulta}</p>`;

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
 *   ({ session_id, structured_data, addenda, autor_nombre? })
 * @param {object} options
 * @param {boolean} [options.isAdmin=false] - when true, shows admin-only actions
 * @param {function} [options.onDownloadPdf] - called when "Descargar PDF" is
 *   clicked (isAdmin only). Not implemented if isAdmin is false.
 * @param {function} [options.onAddAddendum] - extension point for Stage F
 *   (addendum-writing). Not implemented this stage — reserved so this
 *   function's shape doesn't need reworking again when Stage F lands.
 * @param {function} [options.onCancel] - extension point for Stage G
 *   (session cancellation). Not implemented this stage.
 */
function renderSessionDetail(container, sessionData, options = {}) {
    const { isAdmin = false, onDownloadPdf, onAddAddendum, onCancel } = options;
    const sd = sessionData.structured_data || {};

    let html = `<h3 style="margin-bottom: 1rem;">Nota de Consulta</h3>`;

    if (sessionData.autor_nombre) {
        html += `<div class="historial-autor-banner">Nota escrita por Dr(a). ${sessionData.autor_nombre}</div>`;
    }

    if (isAdmin) {
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
                <div class="adenda-meta"><strong>${a.author || 'Médico'}</strong> · ${fecha}</div>
                <p class="adenda-text">${a.text || ''}</p>
            </div>`;
        });
        html += '</div>';
    }

    container.innerHTML = html;

    if (isAdmin && typeof onDownloadPdf === 'function') {
        const btn = container.querySelector('#adminDetailDownloadPdfBtn');
        if (btn) btn.addEventListener('click', onDownloadPdf);
    }

    // onAddAddendum (Stage F) and onCancel (Stage G) are not wired up yet —
    // reserved in the options shape so this function doesn't need reworking
    // again when those stages land.
}
