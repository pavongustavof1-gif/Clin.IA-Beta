// frontend/admin.js — ADM-1 Stage A: shell, role-gating, read-only doctor list

let clinicaLogoObjectUrl = null; // tracked so we can revoke the previous preview URL before replacing it

async function getAuthHeaders() {
    const { data: { session } } = await supabaseClient.auth.getSession();
    return { 'Authorization': 'Bearer ' + session?.access_token };
}

function logout() {
    sessionStorage.removeItem('clinia_token');
    sessionStorage.removeItem('clinia_email');
    window.location.href = '/login';
}

function handleSessionExpired() {
    sessionStorage.removeItem('clinia_token');
    sessionStorage.removeItem('clinia_email');
    window.location.href = '/login';
}

document.addEventListener('DOMContentLoaded', () => {
    const logoutBtn = document.getElementById('logoutBtn');
    if (logoutBtn) {
        logoutBtn.addEventListener('click', (e) => {
            e.preventDefault();
            logout();
        });
    }

    const showBtn = document.getElementById('showAddDoctorBtn');
    const cancelBtn = document.getElementById('cancelAddDoctorBtn');
    const submitBtn = document.getElementById('submitAddDoctorBtn');
    const form = document.getElementById('addDoctorForm');

    if (showBtn) {
        showBtn.addEventListener('click', () => {
            form.style.display = 'block';
            showBtn.style.display = 'none';
        });
    }

    if (cancelBtn) {
        cancelBtn.addEventListener('click', () => {
            resetAddDoctorForm();
        });
    }

    if (submitBtn) {
        submitBtn.addEventListener('click', submitAddDoctor);
    }

    // Pre-fill Desde/Hasta with today's actual committed value, not just a
    // visual default — item 38's real bug was that a native <input
    // type="date">'s calendar picker highlights today in the grid as a
    // reference marker WITHOUT that being the field's real .value. A
    // doctor who saw "today" highlighted and clicked Buscar without
    // explicitly selecting a day got an empty desde/hasta, silently
    // falling through to the 30-day default instead of filtering to
    // today. Setting .value directly here means what's displayed always
    // matches what actually gets submitted.
    const sessionSearchDesdeInput = document.getElementById('sessionSearchDesde');
    const sessionSearchHastaInput = document.getElementById('sessionSearchHasta');
    if (sessionSearchDesdeInput && sessionSearchHastaInput) {
        // Local date components, NOT toISOString() — that's UTC-based and
        // would show the wrong calendar day near midnight for any clinic
        // not in UTC (e.g. Mexico, UTC-6), same class of mismatch as the
        // bug this whole pre-fill exists to fix.
        const now = new Date();
        const todayIso = `${now.getFullYear()}-${String(now.getMonth() + 1).padStart(2, '0')}-${String(now.getDate()).padStart(2, '0')}`;
        sessionSearchDesdeInput.value = todayIso;
        sessionSearchHastaInput.value = todayIso;
    }

    const sessionSearchBtn = document.getElementById('sessionSearchBtn');
    if (sessionSearchBtn) {
        sessionSearchBtn.addEventListener('click', () => searchSessions(true));
    }

    const sessionSearchPrevBtn = document.getElementById('sessionSearchPrevBtn');
    if (sessionSearchPrevBtn) {
        sessionSearchPrevBtn.addEventListener('click', sessionSearchPrevPage);
    }

    const sessionSearchNextBtn = document.getElementById('sessionSearchNextBtn');
    if (sessionSearchNextBtn) {
        sessionSearchNextBtn.addEventListener('click', sessionSearchNextPage);
    }

    const sessionSearchPageSize = document.getElementById('sessionSearchPageSize');
    if (sessionSearchPageSize) {
        sessionSearchPageSize.addEventListener('change', () => {
            // Only auto-refresh if a search has already run — don't fetch
            // before the doctor has actually searched for anything.
            if (document.getElementById('sessionSearchTableWrap').style.display !== 'none') {
                searchSessions(true);
            }
        });
    }

    const sessionSearchByIdBtn = document.getElementById('sessionSearchByIdBtn');
    if (sessionSearchByIdBtn) {
        sessionSearchByIdBtn.addEventListener('click', searchSessionById);
    }
    const sessionSearchByIdInput = document.getElementById('sessionSearchByIdInput');
    if (sessionSearchByIdInput) {
        sessionSearchByIdInput.addEventListener('keydown', (e) => {
            if (e.key === 'Enter') searchSessionById();
        });
    }

    const clinicaColorInput = document.getElementById('clinicaProfileColor');
    if (clinicaColorInput) {
        clinicaColorInput.addEventListener('input', () => {
            document.getElementById('clinicaProfileColorPreview').textContent = clinicaColorInput.value.toUpperCase();
        });
    }

    const clinicaSaveBtn = document.getElementById('clinicaProfileSaveBtn');
    if (clinicaSaveBtn) {
        clinicaSaveBtn.addEventListener('click', submitClinicaProfile);
    }

    const clinicaLogoUploadBtn = document.getElementById('clinicaLogoUploadBtn');
    if (clinicaLogoUploadBtn) {
        clinicaLogoUploadBtn.addEventListener('click', submitClinicaLogo);
    }

    const clinicaLogoRemoveBtn = document.getElementById('clinicaLogoRemoveBtn');
    if (clinicaLogoRemoveBtn) {
        clinicaLogoRemoveBtn.addEventListener('click', removeClinicaLogo);
    }

    if (clinicaColorInput) {
        loadClinicaProfile();
        loadClinicaLogoPreview();
    }
});

async function searchSessionById() {
    const input = document.getElementById('sessionSearchByIdInput');
    const errorEl = document.getElementById('sessionSearchByIdError');
    const sessionId = input.value.trim();

    errorEl.style.display = 'none';

    if (!sessionId) {
        errorEl.textContent = 'Ingrese un ID de sesión.';
        errorEl.style.display = 'block';
        return;
    }

    // Reuses the same GET /api/patient-history/<id> endpoint and detail
    // panel already used for date-range results — no new backend route,
    // no second detail-rendering path. loadAndRenderSessionDetail() already
    // shows "Sesión no encontrada" inside the panel on 404, so no separate
    // not-found handling needed here.
    document.getElementById('sessionSearchTableWrap').style.display = 'none';
    document.getElementById('sessionDetailPanel').style.display = 'block';
    await loadAndRenderSessionDetail(sessionId);
}

function populateDoctorFilter(usuarios) {
    const select = document.getElementById('sessionSearchDoctor');
    if (!select) return;
    // Keep the existing "Todos los doctores" default option, append the rest
    const existingOptions = new Set(Array.from(select.options).map(o => o.value));
    usuarios.forEach(u => {
        if (!existingOptions.has(u.id)) {
            const opt = document.createElement('option');
            opt.value = u.id;
            opt.textContent = u.nombre;
            select.appendChild(opt);
        }
    });
}

function sessionStatusLabel(status) {
    if (status === 'confirmed') return 'Confirmada';
    if (status === 'cancelled') return 'Cancelada';
    if (status === 'pending_review') return 'Pendiente';
    return escapeHtml(status) || '—';
}

let sessionSearchOffset = 0;
let sessionSearchHasMore = false;

async function searchSessions(resetOffset) {
    if (resetOffset) sessionSearchOffset = 0;

    const desde = document.getElementById('sessionSearchDesde').value;
    const hasta = document.getElementById('sessionSearchHasta').value;
    const usuarioId = document.getElementById('sessionSearchDoctor').value;
    const pageSize = parseInt(document.getElementById('sessionSearchPageSize').value, 10) || 20;

    const loadingEl = document.getElementById('sessionSearchLoading');
    const emptyEl = document.getElementById('sessionSearchEmpty');
    const tableWrap = document.getElementById('sessionSearchTableWrap');
    const paginationEl = document.getElementById('sessionSearchPagination');
    const tbody = document.getElementById('sessionSearchTableBody');

    loadingEl.style.display = 'block';
    emptyEl.style.display = 'none';
    tableWrap.style.display = 'none';
    paginationEl.style.display = 'none';

    const params = new URLSearchParams();
    if (desde) params.set('desde', desde);
    if (hasta) params.set('hasta', hasta);
    if (usuarioId) params.set('usuario_id', usuarioId);
    params.set('limit', String(pageSize));
    params.set('offset', String(sessionSearchOffset));

    try {
        const res = await fetch(`${window.location.origin}/api/admin/sessions?${params.toString()}`, {
            headers: await getAuthHeaders()
        });

        if (res.status === 401) return handleSessionExpired();
        if (res.status === 403) {
            loadingEl.textContent = 'No autorizado.';
            return;
        }
        if (!res.ok) throw new Error('Error al buscar sesiones');

        const data = await res.json();
        const sessions = data.sessions || [];
        sessionSearchHasMore = !!data.has_more;
        loadingEl.style.display = 'none';

        if (!sessions.length) {
            emptyEl.textContent = sessionSearchOffset > 0
                ? 'No hay más resultados.'
                : 'No se encontraron sesiones para estos filtros.';
            emptyEl.style.display = 'block';
            return;
        }

        tbody.innerHTML = sessions.map(s => {
            const fecha = s.timestamp
                ? new Date(s.timestamp).toLocaleString('es-MX', {
                    day: '2-digit', month: 'short', year: 'numeric',
                    hour: '2-digit', minute: '2-digit'
                  })
                : '—';
            return `<tr class="session-row" data-session-id="${escapeHtml(s.session_id)}" style="cursor: pointer;">
                <td>${escapeHtml(s.session_id) || '—'}</td>
                <td>${fecha}</td>
                <td>${escapeHtml(s.doctor_nombre) || '—'}</td>
                <td>${sessionStatusLabel(s.status)}</td>
                <td>${s.tiene_adenda ? 'Sí' : '—'}</td>
            </tr>`;
        }).join('');

        tableWrap.style.display = 'block';
        paginationEl.style.display = 'flex';
        document.getElementById('sessionSearchPrevBtn').disabled = sessionSearchOffset === 0;
        document.getElementById('sessionSearchNextBtn').disabled = !sessionSearchHasMore;

    } catch (err) {
        console.error('[ClinIA Admin] searchSessions error:', err);
        loadingEl.style.display = 'none';
        emptyEl.textContent = 'Error al buscar sesiones. Intente de nuevo.';
        emptyEl.style.display = 'block';
    }
}

function sessionSearchNextPage() {
    if (!sessionSearchHasMore) return;
    const pageSize = parseInt(document.getElementById('sessionSearchPageSize').value, 10) || 20;
    sessionSearchOffset += pageSize;
    searchSessions(false);
}

function sessionSearchPrevPage() {
    const pageSize = parseInt(document.getElementById('sessionSearchPageSize').value, 10) || 20;
    sessionSearchOffset = Math.max(0, sessionSearchOffset - pageSize);
    searchSessions(false);
}

document.addEventListener('click', (e) => {
    const row = e.target.closest('.session-row');
    if (!row) return;
    openSessionDetail(row.dataset.sessionId);
});

document.addEventListener('DOMContentLoaded', () => {
    const backBtn = document.getElementById('sessionDetailBackBtn');
    if (backBtn) {
        backBtn.addEventListener('click', () => {
            document.getElementById('sessionDetailPanel').style.display = 'none';
            document.getElementById('sessionSearchTableWrap').style.display = 'block';
        });
    }
});

async function openSessionDetail(sessionId) {
    const tableWrap = document.getElementById('sessionSearchTableWrap');
    const panel = document.getElementById('sessionDetailPanel');

    tableWrap.style.display = 'none';
    panel.style.display = 'block';

    await loadAndRenderSessionDetail(sessionId);
}

async function loadAndRenderSessionDetail(sessionId) {
    const content = document.getElementById('sessionDetailContent');
    content.innerHTML = '<p style="color: var(--text-secondary);">Cargando...</p>';

    try {
        const res = await fetch(`${window.location.origin}/api/patient-history/${sessionId}`, {
            headers: await getAuthHeaders()
        });

        if (res.status === 401) return handleSessionExpired();
        if (res.status === 404) {
            content.innerHTML = '<p>Sesión no encontrada.</p>';
            return;
        }
        if (!res.ok) throw new Error('Error al cargar sesión');

        const data = await res.json();
        renderSessionDetail(content, data, {
            isAdmin: true,
            canDownloadPdf: true,
            onDownloadPdf: () => downloadAdminSessionPdf(sessionId),
            onAddAddendum: (texto) => submitAdminAddendum(sessionId, texto),
            onCancel: (reason) => submitAdminCancelSession(sessionId, reason)
        });

    } catch (err) {
        console.error('[ClinIA Admin] openSessionDetail error:', err);
        content.innerHTML = '<p>Error al cargar la nota. Intente de nuevo.</p>';
    }
}

async function submitAdminAddendum(sessionId, texto) {
    const res = await fetch(`${window.location.origin}/api/admin/session/${sessionId}/addendum`, {
        method: 'POST',
        headers: { ...(await getAuthHeaders()), 'Content-Type': 'application/json' },
        body: JSON.stringify({ texto })
    });

    if (res.status === 401) {
        handleSessionExpired();
        throw new Error('Sesión expirada');
    }

    const data = await res.json();

    if (!res.ok) {
        throw new Error(data.error || 'No se pudo guardar el adendum.');
    }

    // Success — re-fetch and re-render with the updated addenda, per
    // renderSessionDetail()'s stateless/re-render-based design.
    await loadAndRenderSessionDetail(sessionId);
}

async function submitAdminCancelSession(sessionId, cancellation_reason) {
    const res = await fetch(`${window.location.origin}/api/admin/session/${sessionId}/cancel`, {
        method: 'POST',
        headers: { ...(await getAuthHeaders()), 'Content-Type': 'application/json' },
        body: JSON.stringify({ cancellation_reason })
    });

    if (res.status === 401) {
        handleSessionExpired();
        throw new Error('Sesión expirada');
    }

    const data = await res.json();

    if (!res.ok) {
        throw new Error(data.error || 'No se pudo cancelar la nota.');
    }

    // Success — re-fetch and re-render so the status badge updates to
    // "Cancelada" immediately, no page reload. NOTE: Stage D's search
    // results table (if the admin navigates back to it) is NOT refreshed
    // by this — it keeps showing whatever status it had when originally
    // fetched, until the admin re-runs the search. Acceptable for now:
    // flagged rather than silently left unaddressed.
    await loadAndRenderSessionDetail(sessionId);
}

async function downloadAdminSessionPdf(sessionId) {
    try {
        const res = await fetch(`${window.location.origin}/api/admin/session/${sessionId}/pdf`, {
            headers: await getAuthHeaders()
        });
        if (res.status === 401) return handleSessionExpired();
        if (!res.ok) {
            alert('Error al descargar el PDF.');
            return;
        }
        const blob = await res.blob();
        const url = window.URL.createObjectURL(blob);
        const a = document.createElement('a');
        a.href = url;
        a.download = `ClinIA_${sessionId}.pdf`;
        document.body.appendChild(a);
        a.click();
        window.URL.revokeObjectURL(url);
        document.body.removeChild(a);
    } catch (err) {
        console.error('[ClinIA Admin] downloadAdminSessionPdf error:', err);
        alert('Error al descargar el PDF.');
    }
}

function resetAddDoctorForm() {
    document.getElementById('addDoctorForm').style.display = 'none';
    document.getElementById('showAddDoctorBtn').style.display = '';
    document.getElementById('newDoctorNombre').value = '';
    document.getElementById('newDoctorEmail').value = '';
    document.getElementById('newDoctorEspecialidad').value = '';
    document.getElementById('newDoctorCedula').value = '';
    hideAddDoctorMessages();
}

function hideAddDoctorMessages() {
    document.getElementById('addDoctorError').style.display = 'none';
    document.getElementById('addDoctorSuccess').style.display = 'none';
}

async function submitAddDoctor() {
    const nombre = document.getElementById('newDoctorNombre').value.trim();
    const email = document.getElementById('newDoctorEmail').value.trim();
    const especialidad = document.getElementById('newDoctorEspecialidad').value.trim();
    const cedula = document.getElementById('newDoctorCedula').value.trim();

    const errorEl = document.getElementById('addDoctorError');
    const successEl = document.getElementById('addDoctorSuccess');
    hideAddDoctorMessages();

    if (!nombre || !email || !especialidad || !cedula) {
        errorEl.textContent = 'Todos los campos son obligatorios.';
        errorEl.style.display = 'block';
        return;
    }

    const submitBtn = document.getElementById('submitAddDoctorBtn');
    submitBtn.disabled = true;
    submitBtn.textContent = 'Enviando...';

    try {
        const res = await fetch(`${window.location.origin}/api/admin/usuarios`, {
            method: 'POST',
            headers: { ...(await getAuthHeaders()), 'Content-Type': 'application/json' },
            body: JSON.stringify({ nombre, email, especialidad, cedula })
        });

        if (res.status === 401) return handleSessionExpired();

        const data = await res.json();

        if (!res.ok) {
            errorEl.textContent = data.error || 'Error al invitar al doctor.';
            errorEl.style.display = 'block';
            return;
        }

        addDoctorRow(data);

        successEl.textContent = data.warning
            ? `Doctor creado, pero: ${data.warning}`
            : `Invitación enviada a ${data.email}`;
        successEl.style.display = 'block';

        document.getElementById('newDoctorNombre').value = '';
        document.getElementById('newDoctorEmail').value = '';
        document.getElementById('newDoctorEspecialidad').value = '';
        document.getElementById('newDoctorCedula').value = '';

    } catch (err) {
        console.error('[ClinIA Admin] submitAddDoctor error:', err);
        errorEl.textContent = 'Error de red. Intente de nuevo.';
        errorEl.style.display = 'block';
    } finally {
        submitBtn.disabled = false;
        submitBtn.textContent = 'Enviar invitación';
    }
}

function doctorRowHtml(u) {
    const activo = u.activo !== false;
    const badgeClass = activo ? 'activo' : 'inactivo';
    const badgeLabel = activo ? 'Activo' : 'Inactivo';
    const actionLabel = activo ? 'Desactivar' : 'Reactivar';
    const actionClass = activo ? 'btn-danger' : 'btn-secondary';
    return `<tr data-usuario-id="${escapeHtml(u.id)}">
        <td>${escapeHtml(u.nombre) || '—'}</td>
        <td>${escapeHtml(u.email) || '—'}</td>
        <td>${escapeHtml(u.especialidad) || '—'}</td>
        <td>${escapeHtml(u.cedula) || '—'}</td>
        <td>${escapeHtml(u.rol) || '—'}</td>
        <td><span class="admin-status-badge ${badgeClass}">${badgeLabel}</span></td>
        <td>
            <button class="btn ${actionClass} btn-small toggle-activo-btn"
                    style="max-width: none; width: auto;"
                    data-usuario-id="${escapeHtml(u.id)}"
                    data-nombre="${escapeHtml(u.nombre || '')}"
                    data-target-activo="${!activo}">
                ${actionLabel}
            </button>
        </td>
    </tr>`;
}

document.addEventListener('click', async (e) => {
    const btn = e.target.closest('.toggle-activo-btn');
    if (!btn) return;

    const usuarioId = btn.dataset.usuarioId;
    const nombre = btn.dataset.nombre || 'este doctor';
    const targetActivo = btn.dataset.targetActivo === 'true';
    const actionWord = targetActivo ? 'reactivar' : 'desactivar';

    const confirmed = window.confirm(
        `¿Seguro que deseas ${actionWord} a ${nombre}?` +
        (targetActivo ? '' : ' Perderá acceso a la aplicación de inmediato.')
    );
    if (!confirmed) return;

    btn.disabled = true;
    const originalText = btn.textContent;
    btn.textContent = 'Procesando...';

    try {
        const res = await fetch(`${window.location.origin}/api/admin/usuarios/${usuarioId}/activo`, {
            method: 'PATCH',
            headers: { ...(await getAuthHeaders()), 'Content-Type': 'application/json' },
            body: JSON.stringify({ activo: targetActivo })
        });

        if (res.status === 401) return handleSessionExpired();

        const data = await res.json();

        if (!res.ok) {
            alert(data.error || 'No se pudo actualizar el estado del usuario.');
            btn.disabled = false;
            btn.textContent = originalText;
            return;
        }

        const row = btn.closest('tr');
        const badge = row.querySelector('.admin-status-badge');
        badge.className = `admin-status-badge ${data.activo ? 'activo' : 'inactivo'}`;
        badge.textContent = data.activo ? 'Activo' : 'Inactivo';

        btn.dataset.targetActivo = String(!data.activo);
        btn.className = `btn ${data.activo ? 'btn-danger' : 'btn-secondary'} btn-small toggle-activo-btn`;
        btn.style.maxWidth = 'none';
        btn.style.width = 'auto';
        btn.disabled = false;
        btn.textContent = data.activo ? 'Desactivar' : 'Reactivar';

    } catch (err) {
        console.error('[ClinIA Admin] toggle activo error:', err);
        alert('Error de red. Intente de nuevo.');
        btn.disabled = false;
        btn.textContent = originalText;
    }
});

function addDoctorRow(u) {
    const tbody = document.getElementById('adminTableBody');
    const tableWrap = document.getElementById('adminTableWrap');
    const emptyEl = document.getElementById('adminEmpty');

    tbody.insertAdjacentHTML('beforeend', doctorRowHtml(u));
    emptyEl.style.display = 'none';
    tableWrap.style.display = 'block';
}

// Same token-presence + validity-check pattern as app.js, but this page
// additionally requires rol === 'admin' — anything else bounces silently
// to / rather than showing an error page confirming /admin exists.
window.addEventListener('pageshow', async function(event) {
    const { data: { session } } = await supabaseClient.auth.getSession();
    if (!session) {
        window.location.href = '/login';
        return;
    }

    if (event.persisted) {
        document.body.classList.add('auth-pending');
    }

    fetch(`${window.location.origin}/api/session-check`, {
        headers: await getAuthHeaders()
    }).then(async res => {
        if (res.status === 401) {
            handleSessionExpired();
            return;
        }

        const data = await res.json();
        if (data.rol !== 'admin') {
            window.location.href = '/';
            return;
        }

        document.body.classList.remove('auth-pending');
        loadUsuarios();

    }).catch(() => {
        // Network hiccup on the validity check itself — retry is on the
        // doctor via reload; we don't reveal admin content on a failed check.
    });
});

async function loadUsuarios() {
    const loadingEl = document.getElementById('adminLoading');
    const emptyEl = document.getElementById('adminEmpty');
    const tableWrap = document.getElementById('adminTableWrap');
    const tbody = document.getElementById('adminTableBody');

    try {
        const res = await fetch(`${window.location.origin}/api/admin/usuarios`, {
            headers: await getAuthHeaders()
        });

        if (res.status === 401) return handleSessionExpired();
        if (res.status === 403) {
            loadingEl.textContent = 'No autorizado.';
            return;
        }
        if (!res.ok) throw new Error('Error al cargar usuarios');

        const data = await res.json();
        const usuarios = data.doctores || [];
        loadingEl.style.display = 'none';

        const clinicaNameEl = document.getElementById('adminClinicaNombre');
        if (clinicaNameEl && data.clinica_nombre) {
            clinicaNameEl.textContent = ` — ${data.clinica_nombre}`;
        }

        if (!usuarios.length) {
            emptyEl.style.display = 'block';
            return;
        }

        tbody.innerHTML = usuarios.map(doctorRowHtml).join('');

        tableWrap.style.display = 'block';
        populateDoctorFilter(usuarios);

    } catch (err) {
        console.error('[ClinIA Admin] loadUsuarios error:', err);
        loadingEl.textContent = 'Error al cargar la lista de médicos. Intente de nuevo.';
    }
}

async function loadClinicaProfile() {
    const loadingEl = document.getElementById('clinicaProfileLoading');
    const formEl = document.getElementById('clinicaProfileForm');

    try {
        const res = await fetch(`${window.location.origin}/api/admin/clinica`, {
            headers: await getAuthHeaders()
        });

        if (res.status === 401) return handleSessionExpired();
        if (!res.ok) throw new Error('Error al cargar el perfil de la clínica');

        const data = await res.json();
        const color = data.color_primario || '#0F6E56';

        document.getElementById('clinicaProfileColor').value = color;
        document.getElementById('clinicaProfileColorPreview').textContent = color.toUpperCase();
        document.getElementById('clinicaProfileDireccion').value = data.direccion || '';
        document.getElementById('clinicaProfileTelefono').value = data.telefono || '';

        loadingEl.style.display = 'none';
        formEl.style.display = 'block';

    } catch (err) {
        console.error('[ClinIA Admin] loadClinicaProfile error:', err);
        loadingEl.textContent = 'Error al cargar el perfil de la clínica. Intente de nuevo.';
    }
}

async function submitClinicaProfile() {
    const color = document.getElementById('clinicaProfileColor').value;
    const direccion = document.getElementById('clinicaProfileDireccion').value.trim();
    const telefono = document.getElementById('clinicaProfileTelefono').value.trim();

    const errorEl = document.getElementById('clinicaProfileError');
    const successEl = document.getElementById('clinicaProfileSuccess');
    errorEl.style.display = 'none';
    successEl.style.display = 'none';

    const saveBtn = document.getElementById('clinicaProfileSaveBtn');
    saveBtn.disabled = true;
    saveBtn.textContent = 'Guardando...';

    try {
        const res = await fetch(`${window.location.origin}/api/admin/clinica`, {
            method: 'PATCH',
            headers: { ...(await getAuthHeaders()), 'Content-Type': 'application/json' },
            body: JSON.stringify({ color_primario: color, direccion, telefono })
        });

        if (res.status === 401) return handleSessionExpired();

        const data = await res.json();

        if (!res.ok) {
            errorEl.textContent = data.error || 'Error al guardar el perfil de la clínica.';
            errorEl.style.display = 'block';
            return;
        }

        successEl.textContent = 'Perfil de la clínica guardado.';
        successEl.style.display = 'block';

    } catch (err) {
        console.error('[ClinIA Admin] submitClinicaProfile error:', err);
        errorEl.textContent = 'Error de red. Intente de nuevo.';
        errorEl.style.display = 'block';
    } finally {
        saveBtn.disabled = false;
        saveBtn.textContent = 'Guardar';
    }
}

async function loadClinicaLogoPreview() {
    const previewEl = document.getElementById('clinicaLogoPreview');
    const removeBtn = document.getElementById('clinicaLogoRemoveBtn');

    try {
        // Private bucket, proxied — a plain <img src="..."> can't send
        // auth headers, so fetch as a blob and set an object URL as the
        // src. Same blob-handling mechanics as the PDF download handlers
        // (fetch -> response.blob() -> URL.createObjectURL()), just
        // landing in an <img> instead of a triggered download.
        const res = await fetch(`${window.location.origin}/api/admin/clinica/logo`, {
            headers: await getAuthHeaders()
        });

        if (res.status === 401) return handleSessionExpired();

        if (res.status === 404) {
            previewEl.style.display = 'none';
            removeBtn.style.display = 'none';
            return;
        }
        if (!res.ok) throw new Error('Error al cargar el logo');

        const blob = await res.blob();
        if (clinicaLogoObjectUrl) URL.revokeObjectURL(clinicaLogoObjectUrl);
        clinicaLogoObjectUrl = URL.createObjectURL(blob);
        previewEl.src = clinicaLogoObjectUrl;
        previewEl.style.display = 'inline-block';
        removeBtn.style.display = '';

    } catch (err) {
        console.error('[ClinIA Admin] loadClinicaLogoPreview error:', err);
        // No error shown for a missing/failed logo preview — it's not
        // the primary content of the card, fails silently like other
        // optional preview loads in this app.
    }
}

async function submitClinicaLogo() {
    const fileInput = document.getElementById('clinicaLogoFileInput');
    const errorEl = document.getElementById('clinicaLogoError');
    const successEl = document.getElementById('clinicaLogoSuccess');
    errorEl.style.display = 'none';
    successEl.style.display = 'none';

    const file = fileInput.files[0];
    if (!file) {
        errorEl.textContent = 'Seleccione un archivo de imagen (PNG o JPEG).';
        errorEl.style.display = 'block';
        return;
    }

    const uploadBtn = document.getElementById('clinicaLogoUploadBtn');
    uploadBtn.disabled = true;
    uploadBtn.textContent = 'Subiendo...';

    try {
        const formData = new FormData();
        formData.append('logo', file);

        const res = await fetch(`${window.location.origin}/api/admin/clinica/logo`, {
            method: 'POST',
            headers: await getAuthHeaders(),
            body: formData
        });

        if (res.status === 401) return handleSessionExpired();

        const data = await res.json();

        if (!res.ok) {
            errorEl.textContent = data.error || 'Error al subir el logo.';
            errorEl.style.display = 'block';
            return;
        }

        successEl.textContent = 'Logo actualizado.';
        successEl.style.display = 'block';
        fileInput.value = '';
        await loadClinicaLogoPreview();

    } catch (err) {
        console.error('[ClinIA Admin] submitClinicaLogo error:', err);
        errorEl.textContent = 'Error de red. Intente de nuevo.';
        errorEl.style.display = 'block';
    } finally {
        uploadBtn.disabled = false;
        uploadBtn.textContent = 'Subir logo';
    }
}

async function removeClinicaLogo() {
    const errorEl = document.getElementById('clinicaLogoError');
    const successEl = document.getElementById('clinicaLogoSuccess');
    errorEl.style.display = 'none';
    successEl.style.display = 'none';

    const removeBtn = document.getElementById('clinicaLogoRemoveBtn');
    removeBtn.disabled = true;
    removeBtn.textContent = 'Quitando...';

    try {
        const res = await fetch(`${window.location.origin}/api/admin/clinica/logo`, {
            method: 'DELETE',
            headers: await getAuthHeaders()
        });

        if (res.status === 401) return handleSessionExpired();

        const data = await res.json();

        if (!res.ok) {
            errorEl.textContent = data.error || 'Error al quitar el logo.';
            errorEl.style.display = 'block';
            return;
        }

        const previewEl = document.getElementById('clinicaLogoPreview');
        if (clinicaLogoObjectUrl) {
            URL.revokeObjectURL(clinicaLogoObjectUrl);
            clinicaLogoObjectUrl = null;
        }
        previewEl.src = '';
        previewEl.style.display = 'none';
        removeBtn.style.display = 'none';
        successEl.textContent = 'Logo eliminado.';
        successEl.style.display = 'block';

    } catch (err) {
        console.error('[ClinIA Admin] removeClinicaLogo error:', err);
        errorEl.textContent = 'Error de red. Intente de nuevo.';
        errorEl.style.display = 'block';
    } finally {
        removeBtn.disabled = false;
        removeBtn.textContent = 'Quitar logo';
    }
}
