// frontend/utils.js
// Shared helpers for the render paths in session-detail-render.js, app.js,
// and admin.js. Load this before any of those three files.

// Escapes & < > " ' so a value can never break out of HTML text/attribute
// context when interpolated into an innerHTML string. Every dynamic value
// that reaches innerHTML in this app must pass through this first — stored
// free text (addenda, patient/SOAP fields, clinic branding, doctor names)
// is untrusted regardless of who wrote it.
function escapeHtml(value) {
    return String(value ?? '').replace(/[&<>"']/g, (c) => ({
        '&': '&amp;',
        '<': '&lt;',
        '>': '&gt;',
        '"': '&quot;',
        "'": '&#39;',
    }[c]));
}

// Guards a URL before it's assigned to href/src — blocks javascript: and
// other script-executing schemes. Allows relative URLs (no scheme) and
// http(s). Returns '#' for anything else so a bad value can never execute,
// it just becomes an inert link.
function safeUrl(value) {
    const v = String(value ?? '').trim();
    if (!v) return '#';
    try {
        // Relative URLs resolve against location and come out http(s) here.
        const resolved = new URL(v, window.location.origin);
        if (resolved.protocol === 'http:' || resolved.protocol === 'https:') {
            return v;
        }
    } catch {
        // Not a parseable URL at all — treat as unsafe.
    }
    return '#';
}
