// frontend/admin.js — ADM-1 Stage A: shell, role-gating, read-only doctor list

function getAuthHeaders() {
    return { 'Authorization': 'Bearer ' + sessionStorage.getItem('clinia_token') };
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
});

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
            headers: { ...getAuthHeaders(), 'Content-Type': 'application/json' },
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
    return `<tr>
        <td>${u.nombre || '—'}</td>
        <td>${u.email || '—'}</td>
        <td>${u.especialidad || '—'}</td>
        <td>${u.cedula || '—'}</td>
        <td>${u.rol || '—'}</td>
        <td><span class="admin-status-badge ${badgeClass}">${badgeLabel}</span></td>
    </tr>`;
}

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
window.addEventListener('pageshow', function(event) {
    const token = sessionStorage.getItem('clinia_token');
    if (!token) {
        window.location.href = '/login';
        return;
    }

    if (event.persisted) {
        document.body.classList.add('auth-pending');
    }

    fetch(`${window.location.origin}/api/session-check`, {
        headers: getAuthHeaders()
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
            headers: getAuthHeaders()
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

    } catch (err) {
        console.error('[ClinIA Admin] loadUsuarios error:', err);
        loadingEl.textContent = 'Error al cargar la lista de médicos. Intente de nuevo.';
    }
}
