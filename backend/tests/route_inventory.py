# backend/tests/route_inventory.py
# THE SOURCE OF TRUTH for this suite's intended authorization policy —
# one entry per route in app.py. This is a machine-checked fixture:
# test_route_authorization.py's test_every_app_route_is_inventoried
# walks the real Flask url_map and fails if a route exists with no
# entry here, or an entry here names a route that no longer exists.
# A newly-added route with no auth decision recorded is exactly the
# silent-regression class this suite exists to catch.
#
# Built by reading every route's actual decorator stack and body in
# app.py (not inferred from naming) as of the auth_test_suite branch.
#
# Fields:
#   methods         - HTTP methods this entry covers
#   auth_required   - False only for the page shells, /api/health and the
#                     public access request (POST /api/solicitudes)
#   admin_required  - True if @require_admin stacks after @require_auth
#                     (rol admin or auxiliar — auth.is_admin_role)
#   medico_required - True if @require_medico stacks after @require_auth
#                     (rol admin or medico — auth.is_medico_role); an
#                     auxiliar gets 404 on these, never 403
#   superadmin_required - True for /api/superadmin/*: @require_superadmin
#                     (used INSTEAD of @require_auth) answers 404 to every
#                     caller who isn't in Config.SUPERADMIN_USER_IDS —
#                     including no token — never 401/403. Absent = False.
#   resource        - what data/action the route touches, in one phrase
#   rule            - the intended access policy in prose
#   scope           - 'none' | 'owner' | 'owner_or_clinic' | 'clinic' | 'self_scoped_query'
#       none              - no cross-caller resource to isolate
#       owner             - only the authoring/target-matching usuario_id
#       owner_or_clinic   - authoring usuario_id OR same clinica_id (continuity of care)
#       clinic            - any resource in the caller's own clinica_id (admin routes)
#       self_scoped_query - the query is server-built from g.usuario, never client-controlled
#   miss_status     - status code for "found but out of caller's scope" (403 or 404)
#                     None where scope == 'none'/'self_scoped_query' (nothing to miss)

ROUTE_INVENTORY = [
    # ── Public page shells — no server-side auth gate; real boundary is
    #    the API routes each page calls client-side ──────────────────────
    {'path': '/', 'methods': ['GET'], 'auth_required': False, 'admin_required': False, 'medico_required': False,
     'resource': 'app page shell', 'rule': 'always 200, no data', 'scope': 'none', 'miss_status': None},
    {'path': '/login', 'methods': ['GET'], 'auth_required': False, 'admin_required': False, 'medico_required': False,
     'resource': 'login page shell', 'rule': 'always 200, no data', 'scope': 'none', 'miss_status': None},
    {'path': '/set-password', 'methods': ['GET'], 'auth_required': False, 'admin_required': False, 'medico_required': False,
     'resource': 'invite/set-password page shell', 'rule': 'always 200, no data', 'scope': 'none', 'miss_status': None},
    {'path': '/admin', 'methods': ['GET'], 'auth_required': False, 'admin_required': False, 'medico_required': False,
     'resource': 'admin page shell', 'rule': 'always 200, no data (role gate is client-side + the /api/admin/* routes)', 'scope': 'none', 'miss_status': None},
    {'path': '/account', 'methods': ['GET'], 'auth_required': False, 'admin_required': False, 'medico_required': False,
     'resource': 'account page shell', 'rule': 'always 200, no data', 'scope': 'none', 'miss_status': None},
    {'path': '/superadmin', 'methods': ['GET'], 'auth_required': False, 'admin_required': False, 'medico_required': False,
     'resource': 'superadmin review page shell', 'rule': 'always 200, no data, noindex (gate is client-side + /api/superadmin/*)', 'scope': 'none', 'miss_status': None},
    {'path': '/api/health', 'methods': ['GET'], 'auth_required': False, 'admin_required': False, 'medico_required': False,
     'resource': 'health check', 'rule': 'always 200, no data, rate-limit exempt', 'scope': 'none', 'miss_status': None},

    # ── Public access request (#53/#54) — no account exists yet ─────────
    {'path': '/solicitud', 'methods': ['GET'], 'auth_required': False, 'admin_required': False, 'medico_required': False,
     'resource': 'access-request form page shell', 'rule': 'always 200, no data, noindex', 'scope': 'none', 'miss_status': None},
    {'path': '/aviso-privacidad-usuarios', 'methods': ['GET'], 'auth_required': False, 'admin_required': False, 'medico_required': False,
     'resource': 'privacy notice page (placeholder)', 'rule': 'always 200, no data', 'scope': 'none', 'miss_status': None},
    {'path': '/api/solicitudes', 'methods': ['POST'], 'auth_required': False, 'admin_required': False, 'medico_required': False,
     'resource': 'new solicitudes_registro row (write-only, nothing is read back)',
     'rule': 'public; JSON only; rate-limited per IP + globally; same generic 200 for created, honeypot and duplicates',
     'scope': 'none', 'miss_status': None},

    # ── Authenticated, no cross-caller resource ─────────────────────────
    {'path': '/api/session-check', 'methods': ['GET'], 'auth_required': True, 'admin_required': False, 'medico_required': False,
     'resource': "caller's own rol/clinica_id", 'rule': 'auth only', 'scope': 'none', 'miss_status': None},
    {'path': '/api/process-audio', 'methods': ['POST'], 'auth_required': True, 'admin_required': False, 'medico_required': True,
     'resource': 'new trabajos row, scoped to caller', 'rule': 'auth only — creates under own identity', 'scope': 'none', 'miss_status': None},

    # ── Owner-only (in-flight jobs / not-yet-signed drafts) ─────────────
    {'path': '/api/job-status/<job_id>', 'methods': ['GET'], 'auth_required': True, 'admin_required': False, 'medico_required': True,
     'resource': 'trabajos row (background job)',
     'rule': 'owner only — 404 for wrong-owner, indistinguishable from not-found (normalized from a leaking 403)',
     'scope': 'owner', 'miss_status': 404},
    {'path': '/api/confirm-and-generate', 'methods': ['POST'], 'auth_required': True, 'admin_required': False, 'medico_required': True,
     'resource': 'sesiones row (sign a pending draft)', 'rule': 'owner only (caller_can_addend_session)', 'scope': 'owner', 'miss_status': 404},
    {'path': '/api/pending-sessions', 'methods': ['GET'], 'auth_required': True, 'admin_required': False, 'medico_required': True,
     'resource': "caller's own pending_review sesiones", 'rule': 'owner only, self-scoped query', 'scope': 'self_scoped_query', 'miss_status': None},
    {'path': '/api/pending-sessions/<session_id>', 'methods': ['GET'], 'auth_required': True, 'admin_required': False, 'medico_required': True,
     'resource': 'one pending_review sesiones row', 'rule': 'owner only + must still be pending_review', 'scope': 'owner', 'miss_status': 404},
    {'path': '/api/pending-sessions/<session_id>', 'methods': ['DELETE'], 'auth_required': True, 'admin_required': False, 'medico_required': True,
     'resource': 'one pending_review sesiones row (hard delete)', 'rule': 'owner only + must still be pending_review', 'scope': 'owner', 'miss_status': 404},

    # ── Owner-or-clinic (confirmed/signed notes — continuity of care) ───
    {'path': '/api/session/<session_id>', 'methods': ['GET'], 'auth_required': True, 'admin_required': False, 'medico_required': True,
     'resource': 'sesiones row, full_response', 'rule': 'owner OR same clinic (caller_can_read_session)', 'scope': 'owner_or_clinic', 'miss_status': 404},
    {'path': '/api/export-json/<session_id>', 'methods': ['GET'], 'auth_required': True, 'admin_required': False, 'medico_required': True,
     'resource': 'sesiones row, structured_data as JSON', 'rule': 'owner OR same clinic', 'scope': 'owner_or_clinic', 'miss_status': 404},
    {'path': '/api/download-pdf/<session_id>', 'methods': ['GET'], 'auth_required': True, 'admin_required': False, 'medico_required': True,
     'resource': 'sesiones row, regenerated PDF', 'rule': 'owner OR same clinic', 'scope': 'owner_or_clinic', 'miss_status': 404},
    {'path': '/api/patient-history/<session_id>', 'methods': ['GET'], 'auth_required': True, 'admin_required': False, 'medico_required': True,
     'resource': 'sesiones row, full detail', 'rule': 'owner OR same clinic', 'scope': 'owner_or_clinic', 'miss_status': 404},

    # ── Self-scoped query (server builds the filter from g.usuario) ─────
    {'path': '/api/patient-history', 'methods': ['GET'], 'auth_required': True, 'admin_required': False, 'medico_required': True,
     'resource': 'sesiones list by CURP, scope=mine|clinica', 'rule': "scope=clinica means caller's own clinic, never client-chosen clinic", 'scope': 'self_scoped_query', 'miss_status': None},

    # ── Admin routes — clinic-wide within the admin's own clinic ────────
    {'path': '/api/admin/usuarios', 'methods': ['GET'], 'auth_required': True, 'admin_required': True, 'medico_required': False,
     'resource': 'usuarios list', 'rule': "admin only, own clinic (server-built filter)", 'scope': 'clinic', 'miss_status': None},
    {'path': '/api/admin/usuarios', 'methods': ['POST'], 'auth_required': True, 'admin_required': True, 'medico_required': False,
     'resource': 'new usuarios row (invite)', 'rule': 'admin only, created in own clinic (server-forced)', 'scope': 'none', 'miss_status': None},
    {'path': '/api/admin/usuarios/<usuario_id>/activo', 'methods': ['PATCH'], 'auth_required': True, 'admin_required': True, 'medico_required': False,
     'resource': 'usuarios.activo for a target user', 'rule': 'admin only, target must be in own clinic', 'scope': 'clinic', 'miss_status': 404},
    {'path': '/api/admin/clinica', 'methods': ['GET'], 'auth_required': True, 'admin_required': True, 'medico_required': False,
     'resource': 'clinicas row (profile)', 'rule': 'admin only, own clinic (server-built filter)', 'scope': 'clinic', 'miss_status': None},
    {'path': '/api/admin/clinica', 'methods': ['PATCH'], 'auth_required': True, 'admin_required': True, 'medico_required': False,
     'resource': 'clinicas row (profile)', 'rule': 'admin only, own clinic (server-built filter)', 'scope': 'clinic', 'miss_status': None},
    {'path': '/api/admin/clinica/logo', 'methods': ['POST'], 'auth_required': True, 'admin_required': True, 'medico_required': False,
     'resource': 'clinic logo (Storage)', 'rule': 'admin only, own clinic (server-built path)', 'scope': 'clinic', 'miss_status': None},
    {'path': '/api/admin/clinica/logo', 'methods': ['GET'], 'auth_required': True, 'admin_required': True, 'medico_required': False,
     'resource': 'clinic logo (Storage)', 'rule': 'admin only, own clinic (server-built path)', 'scope': 'clinic', 'miss_status': None},
    {'path': '/api/admin/clinica/logo', 'methods': ['DELETE'], 'auth_required': True, 'admin_required': True, 'medico_required': False,
     'resource': 'clinic logo (Storage)', 'rule': 'admin only, own clinic (server-built path)', 'scope': 'clinic', 'miss_status': None},
    {'path': '/api/admin/sessions', 'methods': ['GET'], 'auth_required': True, 'admin_required': True, 'medico_required': False,
     'resource': 'sesiones list (ARCO search)', 'rule': 'admin only, own clinic (server-built filter)', 'scope': 'clinic', 'miss_status': None},
    {'path': '/api/admin/session/<session_id>', 'methods': ['GET'], 'auth_required': True, 'admin_required': True, 'medico_required': False,
     'resource': 'sesiones row, full detail (admin panel viewer)', 'rule': 'admin only, any session in own clinic', 'scope': 'clinic', 'miss_status': 404},
    {'path': '/api/admin/session/<session_id>/pdf', 'methods': ['GET'], 'auth_required': True, 'admin_required': True, 'medico_required': False,
     'resource': 'sesiones row, regenerated PDF', 'rule': 'admin only, any session in own clinic', 'scope': 'clinic', 'miss_status': 404},
    {'path': '/api/admin/session/<session_id>/addendum', 'methods': ['POST'], 'auth_required': True, 'admin_required': True, 'medico_required': False,
     'resource': 'sesiones.addenda (append)', 'rule': 'admin only, any session in own clinic', 'scope': 'clinic', 'miss_status': 404},
    {'path': '/api/admin/session/<session_id>/cancel', 'methods': ['POST'], 'auth_required': True, 'admin_required': True, 'medico_required': False,
     'resource': 'sesiones row (soft-cancel)', 'rule': 'admin only, any session in own clinic', 'scope': 'clinic', 'miss_status': 404},
    # ── Superadmin (#53, Review C) — one configured person, 404 for all others ──
    {'path': '/api/superadmin/solicitudes', 'methods': ['GET'], 'auth_required': True, 'admin_required': False, 'medico_required': False,
     'superadmin_required': True, 'resource': 'solicitudes_registro lists (all clinics)',
     'rule': 'superadmin only; purges rechazadas older than 30 days', 'scope': 'none', 'miss_status': 404},
    {'path': '/api/superadmin/clinicas', 'methods': ['GET'], 'auth_required': True, 'admin_required': False, 'medico_required': False,
     'superadmin_required': True, 'resource': 'every clinicas row (id, nombre, direccion)',
     'rule': 'superadmin only', 'scope': 'none', 'miss_status': 404},
    {'path': '/api/superadmin/solicitudes/<solicitud_id>/aprobar', 'methods': ['POST'], 'auth_required': True, 'admin_required': False, 'medico_required': False,
     'superadmin_required': True, 'resource': 'solicitud → clinic + auth user + usuarios row + invite',
     'rule': 'superadmin only; resumable state machine', 'scope': 'none', 'miss_status': 404},
    {'path': '/api/superadmin/solicitudes/<solicitud_id>/rechazar', 'methods': ['POST'], 'auth_required': True, 'admin_required': False, 'medico_required': False,
     'superadmin_required': True, 'resource': 'solicitud estado → rechazada (+ optional email)',
     'rule': 'superadmin only; only from pendiente', 'scope': 'none', 'miss_status': 404},
    {'path': '/api/superadmin/solicitudes/<solicitud_id>/reenviar', 'methods': ['POST'], 'auth_required': True, 'admin_required': False, 'medico_required': False,
     'superadmin_required': True, 'resource': 'new recovery link + invite email',
     'rule': 'superadmin only; aprobada and invite not accepted', 'scope': 'none', 'miss_status': 404},
    {'path': '/api/superadmin/solicitudes/<solicitud_id>/eliminar', 'methods': ['POST'], 'auth_required': True, 'admin_required': False, 'medico_required': False,
     'superadmin_required': True, 'resource': 'auth user (+usuarios by cascade), created clinic if empty, solicitud row',
     'rule': 'superadmin only; aprobada, invite not accepted, no sesiones/trabajos/lecturas', 'scope': 'none', 'miss_status': 404},
]

# Sanity: no duplicate (path, method) pairs — each real route handled once.
_seen = set()
for _entry in ROUTE_INVENTORY:
    for _m in _entry['methods']:
        _key = (_entry['path'], _m)
        assert _key not in _seen, f"duplicate inventory entry: {_key}"
        _seen.add(_key)
