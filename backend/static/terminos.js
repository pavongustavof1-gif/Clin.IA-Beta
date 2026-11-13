// frontend/terminos.js
// 0.9.0 (#81): one-time notice of the updated legal texts. When
// /api/session-check reports terminos.aceptada === false, a modal shows
// what changed, with links to the new documents, and one "Acepto" button
// that records the acceptance (POST /api/terminos/aceptar). Once accepted
// it never appears again for that version. It never blocks anything: no
// route checks acceptance, and if recording it fails the modal just
// closes and comes back at the next login.
//
// The summary of changes is per version (CHANGES below). A version with no
// entry still gets the notice, with every document listed. Built with
// textContent only; the document links are fixed.
//
// Loaded after auth-common.js (getAuthHeaders, handleSessionExpired) and
// before the page script that calls it. External file: the CSP blocks
// un-nonce'd inline scripts.
const Terminos = (() => {
    const DOCS = {
        ToS: { titulo: 'Términos de Servicio', url: 'https://clinianotes.com/legal/terminos/' },
        AU: { titulo: 'Aviso de Privacidad para Usuarios', url: 'https://clinianotes.com/legal/aviso-usuarios/' },
        AP: { titulo: 'Aviso de Privacidad para Pacientes', url: 'https://clinianotes.com/legal/aviso-pacientes/' },
    };
    // Config.TERMINOS_VERSION → what changed in it (tests/test_terminos.py
    // checks the current version has an entry).
    const CHANGES = {
        'ToS-1.3-Beta+AU-1.2-Beta+AP-1.3-Beta': {
            vigencia: 'Vigentes a partir del 28 de noviembre de 2026.',
            cambios: [
                { code: 'ToS', texto: 'Cláusula 12: los cambios sustanciales se le notificarán por correo ' +
                    'electrónico con al menos 15 días naturales de anticipación. Si sigue usando el Servicio ' +
                    'a partir de su entrada en vigor, se entenderá que los acepta; si no, puede terminar el ' +
                    'Servicio antes de esa fecha, sin penalización.' },
                { code: 'AP', texto: 'Sección 4: AssemblyAI, además de transcribir el audio, estructura la ' +
                    'información clínica mediante un modelo de lenguaje que opera a través de Amazon Web ' +
                    'Services, sin conservar el texto ni la respuesta (retención cero). Google (Gemini API) ' +
                    'deja de ser subencargado.' },
            ],
        },
    };
    let version = null;
    let overlay = null;

    // "ToS-1.3-Beta+AU-1.2-Beta+AP-1.3-Beta" → {ToS: '1.3 Beta', AU: '1.2 Beta', AP: '1.3 Beta'}
    function parseVersion(v) {
        const out = {};
        for (const part of String(v || '').split('+')) {
            const i = part.indexOf('-');
            if (i > 0 && DOCS[part.slice(0, i)]) out[part.slice(0, i)] = part.slice(i + 1).replace(/-/g, ' ');
        }
        return out;
    }

    function el(tag, className, text) {
        const node = document.createElement(tag);
        if (className) node.className = className;
        if (text !== undefined) node.textContent = text;
        return node;
    }

    function docLink(code, versions) {
        const a = el('a', null, `${DOCS[code].titulo} (versión ${versions[code]})`);
        a.href = DOCS[code].url;
        a.target = '_blank';
        a.rel = 'noopener';
        return a;
    }

    function build() {
        const versions = parseVersion(version);
        const info = CHANGES[version];
        overlay = el('div', 'terminos-overlay');
        const box = el('div', 'terminos-box');
        box.setAttribute('role', 'dialog');
        box.setAttribute('aria-modal', 'true');
        box.setAttribute('aria-labelledby', 'terminosTitulo');
        const title = el('h2', 'terminos-title', 'Actualizamos nuestros documentos legales');
        title.id = 'terminosTitulo';
        box.appendChild(title);
        if (info && info.vigencia) box.appendChild(el('p', 'terminos-text', info.vigencia));

        const list = el('ul', 'terminos-list');
        const items = info ? info.cambios : Object.keys(versions).map(code => ({ code, texto: '' }));
        for (const item of items) {
            if (!versions[item.code]) continue;
            const li = el('li');
            li.appendChild(docLink(item.code, versions));
            if (item.texto) li.appendChild(el('p', 'terminos-change', item.texto));
            list.appendChild(li);
        }
        box.appendChild(list);
        box.appendChild(el('p', 'terminos-note',
            'Si no está de acuerdo, puede terminar el Servicio conforme a la cláusula 11 de los Términos de Servicio.'));

        const actions = el('div', 'terminos-actions');
        const accept = el('button', 'terminos-btn terminos-btn-primary', 'Acepto');
        accept.type = 'button';
        accept.addEventListener('click', () => submit(accept));
        actions.appendChild(accept);
        box.appendChild(actions);
        overlay.appendChild(box);
        document.body.appendChild(overlay);
        accept.focus();
    }

    function hide() {
        if (overlay) overlay.remove();
        overlay = null;
    }

    async function submit(button) {
        button.disabled = true;
        try {
            const res = await fetch(`${window.location.origin}/api/terminos/aceptar`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json', ...(await getAuthHeaders()) },
                body: JSON.stringify({ version }),
            });
            if (res.status === 401) {
                hide();
                return handleSessionExpired();
            }
        } catch (e) {
            // Not recorded: the notice comes back at the next login.
        }
        hide();
    }

    return {
        // From /api/session-check: the notice if the current version isn't
        // accepted yet (aceptada === false; null = lookup failed → nothing).
        fromSession(data) {
            const t = data && data.terminos;
            if (!t || t.aceptada !== false || !t.version || overlay) return;
            version = t.version;
            build();
        },
    };
})();
