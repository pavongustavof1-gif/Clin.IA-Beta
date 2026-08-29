// frontend/auth-common.js
// Single source for Supabase client creation and the auth helpers that
// were duplicated three times each (app.js, admin.js, account.html)
// before Stage E3 — getAuthHeaders, logout, handleSessionExpired — plus
// the createClient/storage config that was repeated inline across all
// five templates. Divergence between copies is exactly the bug class
// BL-1 (silent token refresh) and Stage M2 #18 (logout revocation) had
// to be hand-applied to all three copies to fix — this file exists so
// that only has to happen once.
//
// Every template that needs auth sets window.CLINIA_CONFIG via its own
// minimal nonce'd inline script (Jinja-rendered supabase_url/
// supabase_anon_key — this file is a static asset with no Jinja access
// of its own), THEN loads this file, BEFORE any page-specific script
// that uses supabaseClient/getAuthHeaders/logout/handleSessionExpired.
// This is the "how do you hand Jinja-rendered Supabase config to a
// static file" answer app.py's CSP comment (Stage 3, #7-#9) flagged as
// the reason this consolidation hadn't been done until now.
//
// `const` at top level of a classic (non-module) script lives in the
// shared global lexical scope, not on `window` — so every symbol below
// is reachable as a bare identifier from any script loaded after this
// one on the same page, exactly like the inline `const supabaseClient`
// each template used to declare. No call-site changes needed anywhere
// that already does `supabaseClient.auth...` or `await getAuthHeaders()`.

const supabaseClient = window.supabase.createClient(
    window.CLINIA_CONFIG.supabaseUrl,
    window.CLINIA_CONFIG.supabaseAnonKey,
    { auth: { storage: window.sessionStorage, storageKey: 'clinia_token' } }
);

async function getAuthHeaders() {
    const { data: { session } } = await supabaseClient.auth.getSession();
    return { 'Authorization': 'Bearer ' + session?.access_token };
}

async function logout() {
    try {
        await supabaseClient.auth.signOut();
    } catch (e) {
        console.error('Logout: signOut() failed', e);
    }
    sessionStorage.removeItem('clinia_token');
    sessionStorage.removeItem('clinia_email');
    window.location.href = '/login';
}

function handleSessionExpired() {
    sessionStorage.removeItem('clinia_token');
    sessionStorage.removeItem('clinia_email');
    window.location.href = '/login';
}
