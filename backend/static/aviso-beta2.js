// frontend/aviso-beta2.js
// Roadmap #45: dismissible Beta 2 notice above the app header. External
// file rather than inline because the CSP only allows nonce'd inline
// scripts. No auth logic here: body.auth-pending already keeps the whole
// page (banner included) hidden until session-check passes.
//
// Every localStorage access is wrapped: in private mode or with storage
// blocked it can throw, and the safe failure is a banner that stays
// visible, never a broken page.
(function () {
    const STORAGE_KEY = 'clinia_aviso_beta2_v1';
    const banner = document.getElementById('avisoBeta2');
    const closeBtn = document.getElementById('avisoBeta2Close');
    if (!banner || !closeBtn) return;

    try {
        if (localStorage.getItem(STORAGE_KEY) === '1') banner.hidden = true;
    } catch (e) { /* stays visible */ }

    closeBtn.addEventListener('click', () => {
        banner.hidden = true;
        try { localStorage.setItem(STORAGE_KEY, '1'); } catch (e) { /* not remembered */ }
    });
})();
