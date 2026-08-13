// frontend/app.js
// CHANGES FROM PREVIOUS VERSION:
//   - Max recording duration: 5 min → 45 min
//   - Added pause/resume recording functionality
//   - Pause timer freezes and counts only active recording time
//   - File upload size limit raised: 50MB → 200MB
//   - Fixed error message text to say "45 minutos"

// ─────────────────────────────────────────────
// Auth helpers
// ─────────────────────────────────────────────
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

// Auth guard — handles both normal load and back/forward cache restore.
// Runs on EVERY pageshow (including bfcache restores where scripts don't
// re-execute) because a bfcache-restored page can already have a visible,
// previously-rendered body from before the token went stale — presence
// alone isn't enough at that point, we need to re-validate every time.
let _appInitialized = false;

window.addEventListener('pageshow', async function(event) {
    const { data: { session } } = await supabaseClient.auth.getSession();
    if (!session) {
        window.location.href = '/login';
        return;
    }

    // If restoring from bfcache, re-hide the body while we re-check —
    // it may already be visible from before this tab was backgrounded.
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

        try {
            const data = await res.json();
            if (data.rol === 'admin') {
                const adminBtn = document.getElementById('adminBtn');
                const adminSep = document.getElementById('adminSep');
                if (adminBtn) adminBtn.style.display = '';
                if (adminSep) adminSep.style.display = '';
            }
        } catch (e) {
            // Non-JSON or malformed response — admin link just stays hidden
        }

        // auth-pending stays applied through init() (including its resumed-
        // job check) so the doctor never sees a flash of the default
        // recording screen before the correct destination — review screen,
        // progress bar, or nothing — is already in place. Same mechanism
        // BL-1 introduced for the stale-token case, just held a bit longer.
        if (!_appInitialized) {
            _appInitialized = true;
            await init();
        }
        document.body.classList.remove('auth-pending');
    }).catch(async () => {
        // Network hiccup — fail open rather than lock the doctor out over
        // a flaky connection; this is a validity check, not a connectivity gate.
        if (!_appInitialized) {
            _appInitialized = true;
            await init();
        }
        document.body.classList.remove('auth-pending');
    });
});

// ─────────────────────────────────────────────
// Global state
// ─────────────────────────────────────────────
const state = {
    mediaRecorder: null,
    audioChunks: [],
    recordingStartTime: null,
    recordingInterval: null,
    currentAudioBlob: null,
    sessionId: null,
    isRecording: false,
    isPaused: false,
    pausedAt: null,        // timestamp when the current pause started
    totalPausedMs: 0,      // cumulative milliseconds spent paused this session
    maxDurationSeconds: 2700, // 45 minutes
    consultationTimestamp: null,
    pendingResult: null,
    consentGiven: false,
    consentTimestamp: null
};

// API Configuration
const API_BASE_URL = window.location.origin.replace(/\/$/, '');

// ─────────────────────────────────────────────
// Doctor email persistence (localStorage)
// ─────────────────────────────────────────────
const STORAGE_KEY_EMAIL = 'clinia_doctor_email';

function loadDoctorEmail() {
    try {
        const saved = localStorage.getItem(STORAGE_KEY_EMAIL);
        if (saved && elements.doctorEmail) {
            elements.doctorEmail.value = saved;
        }
    } catch (e) {
        // localStorage unavailable — silent fail
    }
}

function saveDoctorEmail(email) {
    try {
        localStorage.setItem(STORAGE_KEY_EMAIL, email);
        const indicator = document.getElementById('emailSaveIndicator');
        if (indicator) {
            indicator.classList.add('visible');
            setTimeout(() => indicator.classList.remove('visible'), 2000);
        }
    } catch (e) {
        // localStorage unavailable — silent fail
    }
}

// ─────────────────────────────────────────────
// DOM Elements
// ─────────────────────────────────────────────
const elements = {
    recordBtn: document.getElementById('recordBtn'),
    pauseBtn:  document.getElementById('pauseBtn'),
    stopBtn:   document.getElementById('stopBtn'),
    uploadBtn: document.getElementById('uploadBtn'),
    audioFileInput: document.getElementById('audioFileInput'),
    processBtn: document.getElementById('processBtn'),
    
    recordingStatus: document.getElementById('recordingStatus'),
    recordingTime: document.getElementById('recordingTime'),
    audioPlayerContainer: document.getElementById('audioPlayerContainer'),
    audioPlayer: document.getElementById('audioPlayer'),
    audioFileName: document.getElementById('audioFileName'),
    audioDuration: document.getElementById('audioDuration'),
    
    printRawTranscript: document.getElementById('printRawTranscript'),
    createPDF: document.getElementById('createPDF'),
    downloadPdfBtn: document.getElementById('downloadPdfBtn'),
    
    progressSection: document.getElementById('progressSection'),
    progressFill: document.getElementById('progressFill'),
    progressText: document.getElementById('progressText'),

    resultsSection: document.getElementById('resultsSection'),
    documentLink: document.getElementById('documentLink'),
    docLink: document.getElementById('docLink'),
    speakersExpected: document.getElementById('speakersExpected'),
    transcriptResult: document.getElementById('transcriptResult'),
    transcriptConfidence: document.getElementById('transcriptConfidence'),
    transcriptDuration: document.getElementById('transcriptDuration'),
    transcriptWords: document.getElementById('transcriptWords'),
    transcriptText: document.getElementById('transcriptText'),
    structuredDataResult: document.getElementById('structuredDataResult'),
    formattedData: document.getElementById('formattedData'),
    jsonData: document.getElementById('jsonData'),
    downloadJsonBtn: document.getElementById('downloadJsonBtn'),

    doctorEmail: document.getElementById('doctorEmail'),
    consentCheckbox: document.getElementById('consentCheckbox'),
    consentTratamiento: document.getElementById('consentTratamiento'),
    confirmAndGenerateBtn: document.getElementById('confirmAndGenerateBtn'),
    reviewSection: document.getElementById('reviewSection'),
    reviewActionsSection: document.getElementById('reviewActionsSection'),
    errorSection: document.getElementById('errorSection'),
    errorMessage: document.getElementById('errorMessage'),
    retryBtn: document.getElementById('retryBtn'),
    logoutBtn: document.getElementById('logoutBtn'),
    historialBtn: document.getElementById('historialBtn'),
    historialSection: document.getElementById('historialSection'),
    historialSearch: document.getElementById('historialSearch'),
    historialCurpInput: document.getElementById('historialCurpInput'),
    historialResults: document.getElementById('historialResults'),
    historialDetail: document.getElementById('historialDetail'),
    historialDetailContent: document.getElementById('historialDetailContent'),

    pendingSessionsBtn: document.getElementById('pendingSessionsBtn'),
    pendingSessionsBtnLabel: document.getElementById('pendingSessionsBtnLabel'),
    pendingSessionsBadge: document.getElementById('pendingSessionsBadge'),
    pendingSessionsBanner: document.getElementById('pendingSessionsBanner'),
    pendingSessionsBannerText: document.getElementById('pendingSessionsBannerText'),
    pendingSessionsBannerBtn: document.getElementById('pendingSessionsBannerBtn'),
    pendingSessionsSection: document.getElementById('pendingSessionsSection'),
    pendingSessionsList: document.getElementById('pendingSessionsList'),
};

// ─────────────────────────────────────────────
// Initialize application
// ─────────────────────────────────────────────
async function init() {
    console.log('[ClinIA] Initializing application...');
    
    // Check browser compatibility
    if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
        showError('Tu navegador no soporta grabación de audio. Por favor usa Chrome, Firefox, Edge o Safari.');
        elements.recordBtn.disabled = true;
        return;
    }

    // Restore saved doctor email; fall back to session login email
    loadDoctorEmail();
    if (elements.doctorEmail && !elements.doctorEmail.value) {
        elements.doctorEmail.value = sessionStorage.getItem('clinia_email') || '';
    }

    if (elements.doctorEmail) {
        elements.doctorEmail.addEventListener('blur', () => {
            const email = elements.doctorEmail.value.trim();
            if (email && email.includes('@')) {
                saveDoctorEmail(email);
            }
        });
    }

    // Record button starts disabled until patient consent is given
    elements.recordBtn.disabled = true;

    // Consent checkbox: gate the Record button
    elements.consentCheckbox.addEventListener('change', () => {
        state.consentGiven = elements.consentCheckbox.checked;
        state.consentTimestamp = state.consentGiven
            ? new Date().toISOString()
            : null;
        elements.recordBtn.disabled  = !state.consentGiven;
        elements.uploadBtn.disabled  = !state.consentGiven;
    });

    // Event listeners
    elements.recordBtn.addEventListener('click', startRecording);
    elements.pauseBtn.addEventListener('click', togglePause);
    elements.stopBtn.addEventListener('click', stopRecording);
    elements.uploadBtn.addEventListener('click', () => elements.audioFileInput.click());
    elements.audioFileInput.addEventListener('change', handleFileUpload);
    elements.processBtn.addEventListener('click', processAudio);
    elements.retryBtn.addEventListener('click', resetApplication);
    if (elements.logoutBtn) elements.logoutBtn.addEventListener('click', (e) => { e.preventDefault(); logout(); });
    elements.downloadJsonBtn.addEventListener('click', downloadJSON);
    elements.downloadPdfBtn.addEventListener('click', async () => {
        if (!state.sessionId) {
            console.error('[ClinIA] No session ID available for PDF download');
            return;
        }
        console.log('[ClinIA] Downloading PDF for session:', state.sessionId);
        try {
            const response = await fetch(`${API_BASE_URL}/api/download-pdf/${state.sessionId}`, {
                headers: await getAuthHeaders()
            });
            if (response.status === 401) return handleSessionExpired();
            if (!response.ok) {
                showError('Error al descargar el PDF');
                return;
            }
            const blob = await response.blob();
            const url = window.URL.createObjectURL(blob);
            const a = document.createElement('a');
            a.href = url;
            a.download = `ClinIA_${state.sessionId}.pdf`;
            document.body.appendChild(a);
            a.click();
            window.URL.revokeObjectURL(url);
            document.body.removeChild(a);
        } catch (error) {
            console.error('[ClinIA] PDF download error:', error);
            showError('Error al descargar el PDF');
        }
    });
    
    // Tab switching
    document.querySelectorAll('.tab-btn').forEach(btn => {
        btn.addEventListener('click', (e) => {
            const tabName = e.target.dataset.tab;
            switchTab(tabName);
        });
    });
    
    // Consent 2 (NOM-004): gates the Confirmar button on the review screen
    if (elements.consentTratamiento) {
        elements.consentTratamiento.addEventListener('change', () => {
            if (elements.confirmAndGenerateBtn) {
                elements.confirmAndGenerateBtn.disabled = !elements.consentTratamiento.checked;
            }
        });
    }

    // Transcript toggle for tablet/mobile
    const transcriptToggle = document.getElementById('reviewTranscriptToggle');
    if (transcriptToggle) {
        transcriptToggle.addEventListener('click', () => {
            const panel = document.getElementById('reviewTranscriptPanel');
            const expanded = panel.classList.toggle('is-expanded');
            transcriptToggle.setAttribute('aria-expanded', String(expanded));
            const chevron = transcriptToggle.querySelector('.toggle-chevron');
            if (chevron) chevron.classList.toggle('rotated', expanded);
            transcriptToggle.childNodes.forEach(n => {
                if (n.nodeType === Node.TEXT_NODE) {
                    n.textContent = expanded ? ' Ocultar transcripción' : ' Ver transcripción completa';
                }
            });
        });
    }

    // Historial button
    elements.historialBtn.addEventListener('click', (e) => {
        e.preventDefault();
        if (elements.historialSection.style.display === 'none') {
            showHistorialView();
        } else {
            hideHistorialView();
        }
    });

    // Enter key in CURP input triggers search
    elements.historialCurpInput.addEventListener('keydown', e => {
        if (e.key === 'Enter') searchPatientHistory();
    });

    // Formerly inline onclick="..." attributes in index.html — moved to
    // addEventListener so a strict CSP script-src (no 'unsafe-inline') can
    // hold; CSP blocks inline handler attributes regardless of how they
    // entered the DOM.
    if (elements.confirmAndGenerateBtn) {
        elements.confirmAndGenerateBtn.addEventListener('click', confirmAndGenerate);
    }
    document.getElementById('cancelReviewBtn')?.addEventListener('click', cancelReview);
    document.getElementById('historialBuscarBtn')?.addEventListener('click', searchPatientHistory);
    document.getElementById('historialScopeMine')?.addEventListener('click', () => setHistorialScope('mine'));
    document.getElementById('historialScopeClinica')?.addEventListener('click', () => setHistorialScope('clinica'));
    document.getElementById('historialBackBtn')?.addEventListener('click', showHistorialSearch);
    document.getElementById('avisoInlineLink')?.addEventListener('click', (e) => {
        e.preventDefault();
        showAvisoModal();
    });
    document.getElementById('avisoFooterLink')?.addEventListener('click', (e) => {
        e.preventDefault();
        showAvisoModal();
    });
    document.getElementById('avisoCloseBtn')?.addEventListener('click', closeAvisoModal);

    // Session-ID direct lookup — mirrors admin's Búsqueda ARCO fast path.
    const historialSessionIdBtn = document.getElementById('historialSessionIdBtn');
    if (historialSessionIdBtn) {
        historialSessionIdBtn.addEventListener('click', searchHistorialBySessionId);
    }
    const historialSessionIdInput = document.getElementById('historialSessionIdInput');
    if (historialSessionIdInput) {
        historialSessionIdInput.addEventListener('keydown', e => {
            if (e.key === 'Enter') searchHistorialBySessionId();
        });
    }

    // Pending sessions: nav button toggles the list view, banner button
    // opens it directly (both lead to the same place).
    elements.pendingSessionsBtn.addEventListener('click', (e) => {
        e.preventDefault();
        if (elements.pendingSessionsSection.style.display === 'none') {
            showPendingSessionsView();
        } else {
            hidePendingSessionsView();
        }
    });
    elements.pendingSessionsBannerBtn.addEventListener('click', () => {
        showPendingSessionsView();
    });

    // Resume watching a job that was in progress before a reload OR a
    // closed/reopened tab (localStorage, unlike sessionStorage, survives
    // tab close — that's the whole point of this check).
    const savedJobId = localStorage.getItem('clinia_job_id');
    if (savedJobId) {
        await checkResumedJob(savedJobId);
    }

    // Own unfinished pending_review sessions — badge + banner, checked
    // independently of the job-resume check above (different concept:
    // this is about old, never-completed drafts, not an interrupted
    // in-flight job).
    await checkPendingSessions();

    console.log('[ClinIA] Application initialized successfully');
}

// ─────────────────────────────────────────────
// Recording functions
// ─────────────────────────────────────────────
async function startRecording() {
    try {
        console.log('[ClinIA] Requesting microphone access...');
        
        const stream = await navigator.mediaDevices.getUserMedia({ 
            audio: {
                echoCancellation: true,
                noiseSuppression: true,
                sampleRate: 44100
            } 
        });
        
        console.log('[ClinIA] Microphone access granted');
        
        // Create MediaRecorder
        const mimeType = MediaRecorder.isTypeSupported('audio/webm') ? 'audio/webm' : 'audio/mp4';
        state.mediaRecorder = new MediaRecorder(stream, { mimeType });
        state.audioChunks = [];
        
        // Event handlers
        state.mediaRecorder.ondataavailable = (event) => {
            if (event.data.size > 0) {
                state.audioChunks.push(event.data);
            }
        };
        
        state.mediaRecorder.onstop = handleRecordingStop;
        
        // Start recording
        state.mediaRecorder.start(1000); // Collect data every second
        state.isRecording = true;
        state.recordingStartTime = Date.now();

        // Capture consultation timestamp at the exact moment recording begins
        const userTimezone = Intl.DateTimeFormat().resolvedOptions().timeZone;
        state.consultationTimestamp = new Date().toLocaleString('es-MX', {
            timeZone: userTimezone,
            dateStyle: 'short',
            timeStyle: 'short'
        });
        
        // Update UI
        elements.recordBtn.disabled = true;
        elements.pauseBtn.disabled = false;
        elements.stopBtn.disabled = false;
        elements.uploadBtn.disabled = true;
        elements.recordingStatus.style.display = 'block';
        elements.audioPlayerContainer.style.display = 'none';
        elements.processBtn.disabled = true;
        
        // Start timer
        updateRecordingTime();
        state.recordingInterval = setInterval(updateRecordingTime, 1000);
        
        console.log('[ClinIA] Recording started');
        
    } catch (error) {
        console.error('[ClinIA] Error starting recording:', error);
        showError('No se pudo acceder al micrófono. Por favor verifica los permisos.');
    }
}

function updateRecordingTime() {
    if (!state.isRecording || !state.recordingStartTime) return;
    if (state.isPaused) return; // freeze display while paused

    // Active recording time = wall clock elapsed minus all time spent paused
    const activeMs = (Date.now() - state.recordingStartTime) - state.totalPausedMs;
    const elapsed  = Math.floor(activeMs / 1000);
    const minutes  = Math.floor(elapsed / 60);
    const seconds  = elapsed % 60;
    
    elements.recordingTime.textContent = 
        `${String(minutes).padStart(2, '0')}:${String(seconds).padStart(2, '0')}`;
    
    // Auto-stop at max duration (counts active audio time only)
    if (elapsed >= state.maxDurationSeconds) {
        console.log('[ClinIA] Maximum recording duration reached, stopping...');
        stopRecording();
        showError('Se alcanzó el límite de 45 minutos. La grabación se detuvo automáticamente.');
    }
}

function stopRecording() {
    if (!state.mediaRecorder || !state.isRecording) return;
    
    console.log('[ClinIA] Stopping recording...');
    
    // If paused when stopped, resume briefly so .stop() fires correctly
    if (state.isPaused) {
        state.mediaRecorder.resume();
        state.isPaused = false;
    }

    state.mediaRecorder.stop();
    state.isRecording = false;
    
    // Stop all tracks
    state.mediaRecorder.stream.getTracks().forEach(track => track.stop());
    
    // Clear timer
    if (state.recordingInterval) {
        clearInterval(state.recordingInterval);
        state.recordingInterval = null;
    }
    
    // Update UI — reset pause button to its default state
    elements.recordBtn.disabled = false;
    elements.pauseBtn.disabled = true;
    elements.pauseBtn.classList.remove('btn-resume');
    elements.pauseBtn.classList.add('btn-pause');
    elements.pauseBtn.innerHTML = `
        <span class="btn-icon" aria-hidden="true">
            <svg width="18" height="18"><use href="#icon-pause"/></svg>
        </span>
        Pausar`;
    elements.stopBtn.disabled = true;
    elements.uploadBtn.disabled = false;
    elements.recordingStatus.style.display = 'none';

    // Reset pause tracking state
    state.isPaused    = false;
    state.pausedAt    = null;
    state.totalPausedMs = 0;
    
    console.log('[ClinIA] Recording stopped');
}

function togglePause() {
    if (!state.mediaRecorder || !state.isRecording) return;

    if (!state.isPaused) {
        // ── PAUSING ──────────────────────────────────
        state.mediaRecorder.pause();
        state.isPaused = true;
        state.pausedAt = Date.now();

        // Swap button to "Reanudar" (teal)
        elements.pauseBtn.classList.remove('btn-pause');
        elements.pauseBtn.classList.add('btn-resume');
        elements.pauseBtn.innerHTML = `
            <span class="btn-icon" aria-hidden="true">
                <svg width="18" height="18"><use href="#icon-resume"/></svg>
            </span>
            Reanudar`;

        // Append pause indicator to frozen timer display
        const currentTime = elements.recordingTime.textContent.replace(' ⏸', '');
        elements.recordingTime.textContent = currentTime + ' ⏸';

        console.log('[ClinIA] Recording paused');

    } else {
        // ── RESUMING ─────────────────────────────────
        state.mediaRecorder.resume();
        state.isPaused = false;

        // Accumulate the time spent in this pause
        state.totalPausedMs += (Date.now() - state.pausedAt);
        state.pausedAt = null;

        // Swap button back to "Pausar" (amber)
        elements.pauseBtn.classList.remove('btn-resume');
        elements.pauseBtn.classList.add('btn-pause');
        elements.pauseBtn.innerHTML = `
            <span class="btn-icon" aria-hidden="true">
                <svg width="18" height="18"><use href="#icon-pause"/></svg>
            </span>
            Pausar`;

        console.log('[ClinIA] Recording resumed');
    }
}

function handleRecordingStop() {
    console.log('[ClinIA] Processing recorded audio...');
    
    // Create blob from chunks
    const mimeType = state.mediaRecorder.mimeType;
    const audioBlob = new Blob(state.audioChunks, { type: mimeType });
    
    // Store blob
    state.currentAudioBlob = audioBlob;
    
    // Create URL and display player
    const audioUrl = URL.createObjectURL(audioBlob);
    elements.audioPlayer.src = audioUrl;
    elements.audioPlayerContainer.style.display = 'block';
    
    // Show active recording duration (wall clock minus paused time)
    const activeMs = (Date.now() - state.recordingStartTime) - state.totalPausedMs;
    const durationSeconds = activeMs / 1000;
    elements.audioFileName.textContent = 'Grabación de consulta';
    elements.audioDuration.textContent = `${Math.floor(durationSeconds / 60)}:${String(Math.floor(durationSeconds % 60)).padStart(2, '0')}`;
    
    // Enable process button
    elements.processBtn.disabled = false;
    
    console.log('[ClinIA] Audio ready for processing');
}

function handleFileUpload(event) {
    const file = event.target.files[0];
    
    if (!file) return;
    
    console.log('[ClinIA] File uploaded:', file.name);
    
    // Validate file type
    const validTypes = ['audio/wav', 'audio/mp3', 'audio/mpeg', 'audio/webm', 'audio/ogg', 'audio/m4a'];
    if (!validTypes.includes(file.type) && !file.name.match(/\.(wav|mp3|webm|ogg|m4a)$/i)) {
        showError('Formato de archivo no soportado. Use WAV, MP3, WEBM, OGG o M4A.');
        return;
    }
    
    // Validate file size (200MB max — raised to support long consultations)
    const maxSize = 200 * 1024 * 1024;
    if (file.size > maxSize) {
        showError('El archivo es demasiado grande. Máximo 200MB.');
        return;
    }
    
    // Store file as blob
    state.currentAudioBlob = file;
    
    // Display player
    const audioUrl = URL.createObjectURL(file);
    elements.audioPlayer.src = audioUrl;
    elements.audioPlayerContainer.style.display = 'block';
    elements.audioFileName.textContent = file.name;
    
    // Get duration when metadata loads
    elements.audioPlayer.onloadedmetadata = () => {
        const duration = elements.audioPlayer.duration;
        const minutes = Math.floor(duration / 60);
        const seconds = Math.floor(duration % 60);
        elements.audioDuration.textContent = `${minutes}:${String(seconds).padStart(2, '0')}`;
    };
    
    // Enable process button
    elements.processBtn.disabled = false;
    
    // Hide recording status
    elements.recordingStatus.style.display = 'none';
    
    console.log('[ClinIA] File ready for processing');
}

// ─────────────────────────────────────────────
// Processing functions
// ─────────────────────────────────────────────
async function processAudio() {
    if (!state.currentAudioBlob) {
        showError('No hay audio para procesar');
        return;
    }
    
    console.log('[ClinIA] Starting audio processing pipeline...');
    
    // Hide previous results and errors
    elements.resultsSection.style.display = 'none';
    elements.errorSection.style.display = 'none';
    
    // Show progress
    elements.progressSection.style.display = 'block';
    updateProgress(0, 'Preparando audio...');
    elements.progressSection.scrollIntoView({ behavior: 'smooth', block: 'start' });
    
    try {
        // Prepare form data
        const formData = new FormData();
        formData.append('audio', state.currentAudioBlob, 'recording.webm');
        formData.append('print_raw', elements.printRawTranscript.checked);
        formData.append('create_doc', false);
        formData.append('speakers_expected', elements.speakersExpected.value);
        const _now = new Date();
        const _pad = n => String(n).padStart(2, '0');
        const localTimestamp = `${_now.getFullYear()}-${_pad(_now.getMonth()+1)}-${_pad(_now.getDate())} ${_pad(_now.getHours())}:${_pad(_now.getMinutes())}`;
        formData.append('local_timestamp', localTimestamp);
        formData.append('consultation_timestamp', state.consultationTimestamp || localTimestamp);
        formData.append('consent_given', state.consentGiven);
        formData.append('consent_timestamp', state.consentTimestamp || '');
        
        // Step 1: Upload and transcribe
        updateProgress(10, 'Enviando audio al servidor...', 1);

        const response = await fetch(`${API_BASE_URL}/api/process-audio`, {
            method: 'POST',
            headers: await getAuthHeaders(),
            body: formData
        });

        if (response.status === 401) return handleSessionExpired();

        if (!response.ok) {
            const errorData = await response.json();
            throw new Error(errorData.error || 'Error en el servidor');
        }

        if (response.status === 202) {
            const { job_id } = await response.json();
            // localStorage (not sessionStorage) — must survive a tab close
            // so the doctor can resume watching progress after reopening,
            // even though the job keeps running server-side regardless.
            localStorage.setItem('clinia_job_id', job_id);
            updateProgress(20, 'Audio recibido — iniciando transcripción...', 1);
            startJobPolling(job_id);
            return;
        }

        // Fallback for any synchronous 200 response (should not occur in normal flow)
        const result = await response.json();
        state.sessionId = result.session_id;
        await sleep(400);
        displayReviewScreen(result);

    } catch (error) {
        console.error('[ClinIA] Processing error:', error);
        showError(`Error durante el procesamiento: ${error.message}`);
        elements.progressSection.style.display = 'none';
    }
}

// Shared by startJobPolling's done branch AND checkResumedJob's done
// branch, so the two can never independently diverge on what "done"
// means — a 'done' job has only finished transcription/extraction, it
// has NOT been reviewed/confirmed (CURP doesn't exist yet at this point,
// entered only during review), so the ONLY correct behavior is to enter
// the review screen directly, exactly as an uninterrupted flow would.
async function handleJobDone(data) {
    elements.progressSection.style.display = 'block';
    updateProgress(100, 'Listo para revisión.', 3);
    state.sessionId = data.session_id;
    await sleep(400);
    displayReviewScreen({
        session_id:      data.session_id,
        status:          'pending_review',
        transcript:      data.transcript,
        structured_data: data.structured_data,
    });
}

// One-time status check for a job_id resumed from localStorage (page
// load / reopened tab) — deliberately separate from startJobPolling's
// own polling loop, since this only needs a single check, but MUST
// share the same 'done' handling (see handleJobDone above).
async function checkResumedJob(jobId) {
    try {
        const res = await fetch(`${API_BASE_URL}/api/job-status/${jobId}`, {
            headers: await getAuthHeaders()
        });

        if (res.status === 401) return handleSessionExpired();

        if (!res.ok) {
            // 404 (job gone) or 403 (not this doctor's job) — clear
            // silently, no error shown for a job that's simply gone.
            localStorage.removeItem('clinia_job_id');
            return;
        }

        const data = await res.json();

        if (data.status === 'transcribing' || data.status === 'extracting') {
            elements.progressSection.style.display = 'block';
            if (data.status === 'transcribing') {
                updateProgress(30, 'Reanudando — transcribiendo audio...', 1);
            } else {
                updateProgress(70, 'Reanudando — extrayendo información médica...', 2);
            }
            startJobPolling(jobId);
        } else if (data.status === 'done') {
            localStorage.removeItem('clinia_job_id');
            await handleJobDone(data);
        } else {
            // 'error' status, or any unexpected value — clear silently,
            // same as a not-found job. No error shown for a stale job the
            // doctor already walked away from.
            localStorage.removeItem('clinia_job_id');
        }
    } catch (err) {
        // Fetch itself failed (network blip while the page was loading) —
        // NOT the same as "job is gone". Leave the stored job_id alone so
        // the next reload can retry; clearing here would permanently lose
        // the ability to resume a job that may still be running fine.
        console.warn('[ClinIA] checkResumedJob: could not reach server, will retry on next load:', err);
    }
}

function startJobPolling(jobId) {
    let pollCount = 0;
    const pollInterval = setInterval(async () => {
        pollCount++;
        if (pollCount >= 360) {
            clearInterval(pollInterval);
            localStorage.removeItem('clinia_job_id');
            elements.progressSection.style.display = 'none';
            showError('El procesamiento está tardando más de lo esperado. Por favor intente subir el audio nuevamente.');
            return;
        }
        try {
            const res = await fetch(`${API_BASE_URL}/api/job-status/${jobId}`, {
                headers: await getAuthHeaders()
            });
            if (res.status === 401) {
                clearInterval(pollInterval);
                return handleSessionExpired();
            }
            if (!res.ok) return; // transient error — keep polling

            const data = await res.json();

            if (data.status === 'transcribing') {
                updateProgress(30, 'Transcribiendo audio...', 1);
            } else if (data.status === 'extracting') {
                updateProgress(70, 'Extrayendo información médica...', 2);
            } else if (data.status === 'done') {
                clearInterval(pollInterval);
                localStorage.removeItem('clinia_job_id');
                await handleJobDone(data);
            } else if (data.status === 'error') {
                clearInterval(pollInterval);
                localStorage.removeItem('clinia_job_id');
                elements.progressSection.style.display = 'none';
                showError(data.error_message || 'Error durante el procesamiento');
            }
        } catch (err) {
            // Network blip — keep polling silently
            console.warn('[ClinIA] Poll error (will retry):', err);
        }
    }, 2500);
}

function updateProgress(percentage, text, step = null) {
    elements.progressFill.style.width = `${percentage}%`;
    elements.progressText.textContent = text;
    
    // Update step indicators
    if (step !== null) {
        document.querySelectorAll('.step').forEach((el, index) => {
            if (index + 1 < step) {
                el.classList.add('complete');
                el.classList.remove('active');
            } else if (index + 1 === step) {
                el.classList.add('active');
                el.classList.remove('complete');
            } else {
                el.classList.remove('active', 'complete');
            }
        });
    }
}

function displayResults(result) {
    console.log('[ClinIA] Displaying results...');
    
    // Hide progress
    elements.progressSection.style.display = 'none';
    
    // Show results section
    elements.resultsSection.style.display = 'block';
    
    // 1. Document link
    if (result.document && result.document.link) {
        elements.documentLink.style.display = 'block';
        elements.docLink.href = safeUrl(result.document.link);
        elements.docLink.innerHTML = `Abrir "${escapeHtml(result.document.title)}" en Google Docs <svg width="15" height="15" aria-hidden="true"><use href="#icon-external"/></svg>`;
    } else {
        elements.documentLink.style.display = 'none';
    }
    
    // 2. Transcript — only shown when "Mostrar transcripción completa" is checked
    const showTranscript = elements.printRawTranscript?.checked ?? true;
    if (elements.transcriptResult) {
        elements.transcriptResult.style.display = showTranscript ? 'block' : 'none';
    }

    if (result.transcript) {
        const t = result.transcript;
        
        elements.transcriptConfidence.textContent = 
            `Confianza: ${(t.confidence * 100).toFixed(1)}%`;
        elements.transcriptDuration.textContent = 
            `Duración: ${Math.floor(t.duration_seconds / 60)}:${String(Math.floor(t.duration_seconds % 60)).padStart(2, '0')}`;
        elements.transcriptWords.textContent = 
            `Palabras: ${t.word_count}`;
        
        elements.transcriptText.textContent = t.labeled_text || t.text;
    }
    
    // 3. Structured data
    if (result.structured_data) {
        displayStructuredData(result.structured_data);
    }

    // 4. PDF download button
    if (result.pdf_available && state.sessionId) {
        elements.downloadPdfBtn.style.display = 'inline-flex';
    } else {
        elements.downloadPdfBtn.style.display = 'none';
    }

    // Scroll to results
    elements.resultsSection.scrollIntoView({ behavior: 'smooth', block: 'start' });
}

// ─────────────────────────────────────────────
// Review screen
// ─────────────────────────────────────────────
function displayReviewScreen(result) {
    state.pendingResult = result;

    elements.progressSection.style.display = 'none';
    elements.reviewSection.style.display = 'block';
    if (elements.reviewActionsSection) elements.reviewActionsSection.style.display = 'flex';

    // Consent 2: reset and disable Confirmar on every new review
    if (elements.consentTratamiento) elements.consentTratamiento.checked = false;
    if (elements.confirmAndGenerateBtn) elements.confirmAndGenerateBtn.disabled = true;

    const sd  = result.structured_data || {};
    const info    = sd.informacion_paciente || {};
    const subj    = sd.subjetivo || {};
    const obj     = sd.objetivo || {};
    const vitales = obj.signos_vitales || {};
    const ev      = sd.evaluacion || {};
    const plan    = sd.plan || {};
    const meta    = sd.metadata || {};

    function setVal(id, val) {
        const el = document.getElementById(id);
        if (el) el.value = (val != null) ? val : '';
    }

    // Información del paciente
    setVal('review_numero_expediente',  info.numero_expediente);
    setVal('review_nombre_del_paciente', info.nombre_del_paciente);
    setVal('review_fecha_de_nacimiento', info.fecha_de_nacimiento);
    setVal('review_curp',               info.curp);
    setVal('review_edad',               info.edad);
    setVal('review_genero',             info.genero);

    // Subjetivo
    setVal('review_motivo_de_consulta',          subj.motivo_de_consulta);
    setVal('review_sintomas', Array.isArray(subj.sintomas) ? subj.sintomas.join('\n') : (subj.sintomas || ''));
    setVal('review_historia_de_enfermedad_actual', subj.historia_de_enfermedad_actual);
    setVal('review_duracion_sintomas',             subj.duracion_sintomas);

    // Objetivo — vitales
    setVal('review_presion_arterial',       vitales.presion_arterial);
    setVal('review_frecuencia_cardiaca',    vitales.frecuencia_cardiaca);
    setVal('review_temperatura',            vitales.temperatura);
    setVal('review_frecuencia_respiratoria',vitales.frecuencia_respiratoria);
    setVal('review_saturacion_oxigeno',     vitales.saturacion_oxigeno);
    setVal('review_peso',                   vitales.peso);
    setVal('review_talla',                  vitales.talla);
    setVal('review_habitus',                obj.habitus_exterior);
    setVal('review_examen_fisico',          obj.examen_fisico);

    // Evaluación
    setVal('review_diagnostico',      ev.diagnostico);
    setVal('review_impresion_clinica', ev.impresion_clinica);
    setVal('review_pronostico',        ev.pronostico);

    // Pre-fill CIE-11 from AI suggestion
    setVal('review_codigo_cie11', (ev.codigo_cie11 || '').toUpperCase());
    document.getElementById('review_titulo_cie11').value = ev.titulo_cie11 || '';

    // Plan
    setVal('review_tratamiento', plan.tratamiento);

    const meds = Array.isArray(plan.medicamentos) ? plan.medicamentos : [];
    setVal('review_medicamentos', meds.map(m =>
        typeof m === 'object'
            ? [m.nombre, m.dosis, m.frecuencia, m.duracion].filter(Boolean).join(' - ')
            : String(m)
    ).join('\n'));

    setVal('review_recomendaciones',
        Array.isArray(plan.recomendaciones) ? plan.recomendaciones.join('\n') : (plan.recomendaciones || ''));
    setVal('review_estudios_solicitados',
        Array.isArray(plan.estudios_solicitados) ? plan.estudios_solicitados.join('\n') : (plan.estudios_solicitados || ''));
    setVal('review_seguimiento', plan.seguimiento);

    // Metadatos
    setVal('review_fecha_hora_consulta', meta.fecha_hora_consulta);

    // Populate transcript panel
    const t = result.transcript || {};
    const transcriptEl = document.getElementById('reviewTranscriptText');
    if (transcriptEl) {
        const raw = t.labeled_text || t.text || '';
        // Escape HTML, then bold [Persona X]: speaker labels in teal
        const escaped = escapeHtml(raw);
        transcriptEl.innerHTML = escaped.replace(
            /\[([^\]]+)\]:/g,
            '<strong class="review-speaker-label">[$1]:</strong>'
        );
    }

    elements.reviewSection.scrollIntoView({ behavior: 'smooth', block: 'start' });

    // Keep active review field in view as doctor tabs through form
    document.querySelectorAll('#reviewForm input, #reviewForm textarea, #reviewForm select').forEach(el => {
        el.addEventListener('focus', () => {
            el.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
        });
    });
}

function buildStructuredDataFromForm() {
    function getVal(id) {
        const el = document.getElementById(id);
        return el ? el.value.trim() : '';
    }
    function parseLines(id) {
        return getVal(id).split('\n').map(s => s.trim()).filter(Boolean);
    }

    const sd = {};

    // informacion_paciente
    const info = {};
    if (getVal('review_numero_expediente'))    info.numero_expediente    = getVal('review_numero_expediente');
    if (getVal('review_nombre_del_paciente'))  info.nombre_del_paciente  = getVal('review_nombre_del_paciente');
    if (getVal('review_fecha_de_nacimiento'))  info.fecha_de_nacimiento  = getVal('review_fecha_de_nacimiento');
    if (getVal('review_curp'))                 info.curp                 = getVal('review_curp').toUpperCase();
    if (getVal('review_edad'))                 info.edad                 = getVal('review_edad');
    if (getVal('review_genero'))               info.genero               = getVal('review_genero');
    if (Object.keys(info).length) sd.informacion_paciente = info;

    // subjetivo
    const subj = {};
    if (getVal('review_motivo_de_consulta'))           subj.motivo_de_consulta           = getVal('review_motivo_de_consulta');
    const sintomas = getVal('review_sintomas').split(/[\n,]/).map(s => s.trim()).filter(Boolean);
    if (sintomas.length)                               subj.sintomas                     = sintomas;
    if (getVal('review_historia_de_enfermedad_actual')) subj.historia_de_enfermedad_actual = getVal('review_historia_de_enfermedad_actual');
    if (getVal('review_duracion_sintomas'))             subj.duracion_sintomas             = getVal('review_duracion_sintomas');
    if (Object.keys(subj).length) sd.subjetivo = subj;

    // objetivo
    const obj = {};
    const vitales = {};
    if (getVal('review_presion_arterial'))        vitales.presion_arterial        = getVal('review_presion_arterial');
    if (getVal('review_frecuencia_cardiaca'))     vitales.frecuencia_cardiaca     = getVal('review_frecuencia_cardiaca');
    if (getVal('review_temperatura'))             vitales.temperatura             = getVal('review_temperatura');
    if (getVal('review_frecuencia_respiratoria')) vitales.frecuencia_respiratoria = getVal('review_frecuencia_respiratoria');
    if (getVal('review_saturacion_oxigeno'))      vitales.saturacion_oxigeno      = getVal('review_saturacion_oxigeno');
    if (getVal('review_peso'))                    vitales.peso                    = getVal('review_peso');
    if (getVal('review_talla'))                   vitales.talla                   = getVal('review_talla');
    if (Object.keys(vitales).length) obj.signos_vitales = vitales;
    if (getVal('review_habitus'))   obj.habitus_exterior = getVal('review_habitus');
    if (getVal('review_examen_fisico')) obj.examen_fisico = getVal('review_examen_fisico');
    if (Object.keys(obj).length) sd.objetivo = obj;

    // evaluacion
    const ev = {};
    if (getVal('review_diagnostico'))       ev.diagnostico       = getVal('review_diagnostico');
    if (getVal('review_impresion_clinica')) ev.impresion_clinica  = getVal('review_impresion_clinica');
    if (getVal('review_pronostico'))        ev.pronostico         = getVal('review_pronostico');
    const cie11Code  = document.getElementById('review_codigo_cie11').value.trim();
    const cie11Title = document.getElementById('review_titulo_cie11').value.trim();
    if (cie11Code) {
        ev.codigo_cie11 = cie11Code;
        if (cie11Title) ev.titulo_cie11 = cie11Title;
    }
    if (Object.keys(ev).length) sd.evaluacion = ev;

    // plan
    const plan = {};
    if (getVal('review_tratamiento')) plan.tratamiento = getVal('review_tratamiento');
    const meds = parseLines('review_medicamentos').map(line => {
        const parts = line.split('-').map(s => s.trim());
        return { nombre: parts[0] || '', dosis: parts[1] || '', frecuencia: parts[2] || '', duracion: parts[3] || '' };
    }).filter(m => m.nombre);
    if (meds.length) plan.medicamentos = meds;
    const recomendaciones = parseLines('review_recomendaciones');
    if (recomendaciones.length) plan.recomendaciones = recomendaciones;
    const estudios = parseLines('review_estudios_solicitados');
    if (estudios.length) plan.estudios_solicitados = estudios;
    if (getVal('review_seguimiento')) plan.seguimiento = getVal('review_seguimiento');
    if (Object.keys(plan).length) sd.plan = plan;

    // metadata
    const meta = {};
    if (getVal('review_fecha_hora_consulta')) meta.fecha_hora_consulta = getVal('review_fecha_hora_consulta');
    if (Object.keys(meta).length) sd.metadata = meta;

    return sd;
}

async function confirmAndGenerate() {
    const sd = buildStructuredDataFromForm();

    // Preserve actualizacion_antecedentes (background field — no form input)
    const antecedentes = state.pendingResult?.structured_data?.actualizacion_antecedentes;
    if (antecedentes) sd.actualizacion_antecedentes = antecedentes;

    elements.reviewSection.style.display = 'none';
    if (elements.reviewActionsSection) elements.reviewActionsSection.style.display = 'none';
    elements.progressSection.style.display = 'block';
    updateProgress(10, 'Generando documento...', 3);

    try {
        const response = await fetch(`${API_BASE_URL}/api/confirm-and-generate`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json', ...(await getAuthHeaders()) },
            body: JSON.stringify({
                session_id: state.pendingResult.session_id,
                structured_data: sd,
                create_doc: false,
                create_pdf: elements.createPDF ? elements.createPDF.checked : true,
                doctor_email: elements.doctorEmail?.value?.trim() || '',
                consent_tratamiento_given: elements.consentTratamiento ? elements.consentTratamiento.checked : false,
                consent_tratamiento_timestamp: new Date().toISOString()
            })
        });

        if (response.status === 401) return handleSessionExpired();

        if (!response.ok) {
            const errorData = await response.json();
            throw new Error(errorData.error || 'Error al generar el documento');
        }

        updateProgress(100, '¡Completado!', 3);
        const result = await response.json();

        // Ensure state.sessionId is current before displayResults() shows the PDF button
        if (result.session_id) {
            state.sessionId = result.session_id;
        }

        await sleep(500);
        displayResults(result);

        // Confirming moves this session out of pending_review — refresh the
        // nav badge/banner so a note continued from the pending list (or
        // any confirm at all) doesn't leave a stale count behind.
        checkPendingSessions();

        // Lock review form — note is now immutable (NOM-024)
        document.querySelectorAll('.review-input').forEach(el => {
            el.setAttribute('readonly', true);
            el.style.backgroundColor = 'var(--bg-secondary, #f5f5f5)';
            el.style.color = 'var(--text-muted, #888)';
        });

    } catch (error) {
        console.error('[ClinIA] Confirm error:', error);
        showError(`Error al generar el documento: ${error.message}`);
        elements.progressSection.style.display = 'none';
        elements.reviewSection.style.display = 'block';
        if (elements.reviewActionsSection) elements.reviewActionsSection.style.display = 'flex';
    }
}

function cancelReview() {
    state.pendingResult = null;
    elements.reviewSection.style.display = 'none';
    if (elements.reviewActionsSection) elements.reviewActionsSection.style.display = 'none';
    elements.processBtn.disabled = false;
    window.scrollTo({ top: 0, behavior: 'smooth' });
}

function displayStructuredData(data) {
    // Formatted view
    const formattedHTML = generateFormattedHTML(data);
    elements.formattedData.innerHTML = formattedHTML;
    
    // JSON view
    elements.jsonData.textContent = JSON.stringify(data, null, 2);
}

function switchTab(tabName) {
    // Update buttons
    document.querySelectorAll('.tab-btn').forEach(btn => {
        if (btn.dataset.tab === tabName) {
            btn.classList.add('active');
        } else {
            btn.classList.remove('active');
        }
    });
    
    // Update content
    document.querySelectorAll('.tab-content').forEach(content => {
        if (content.id === `${tabName}View`) {
            content.classList.add('active');
        } else {
            content.classList.remove('active');
        }
    });
}

async function downloadJSON() {
    if (!state.sessionId) return;
    
    try {
        const response = await fetch(`${API_BASE_URL}/api/export-json/${state.sessionId}`, {
            headers: await getAuthHeaders()
        });
        if (response.status === 401) return handleSessionExpired();
        const blob = await response.blob();
        
        const url = window.URL.createObjectURL(blob);
        const a = document.createElement('a');
        a.href = url;
        a.download = `clinia_${state.sessionId}.json`;
        document.body.appendChild(a);
        a.click();
        window.URL.revokeObjectURL(url);
        document.body.removeChild(a);
        
        console.log('[ClinIA] JSON downloaded');
    } catch (error) {
        console.error('[ClinIA] Download error:', error);
        showError('Error al descargar el JSON');
    }
}

// ─────────────────────────────────────────────
// Utility functions
// ─────────────────────────────────────────────
function showError(message) {
    console.error('[ClinIA] Error:', message);

    elements.errorSection.style.display = 'block';
    elements.errorMessage.textContent = message;

    elements.errorSection.scrollIntoView({ behavior: 'smooth' });
}

function resetApplication() {
    console.log('[ClinIA] Resetting application...');
    
    // Reset state
    state.currentAudioBlob = null;
    state.sessionId = null;
    state.audioChunks = [];
    state.isPaused = false;
    state.pausedAt = null;
    state.totalPausedMs = 0;
    state.consentGiven = false;
    state.consentTimestamp = null;
    
    // Reset UI
    elements.audioPlayerContainer.style.display = 'none';
    elements.processBtn.disabled = true;
    elements.progressSection.style.display = 'none';
    elements.resultsSection.style.display = 'none';
    elements.errorSection.style.display = 'none';
    elements.recordingStatus.style.display = 'none';
    
    // Reset pause button appearance in case it was left in "Reanudar" state
    elements.pauseBtn.disabled = true;
    elements.pauseBtn.classList.remove('btn-resume');
    elements.pauseBtn.classList.add('btn-pause');
    elements.pauseBtn.innerHTML = `
        <span class="btn-icon" aria-hidden="true">
            <svg width="18" height="18"><use href="#icon-pause"/></svg>
        </span>
        Pausar`;
    
    // Reset consent and re-disable Record button
    elements.consentCheckbox.checked = false;
    elements.recordBtn.disabled = true;

    // Reset PDF button and checkbox
    elements.downloadPdfBtn.style.display = 'none';
    if (elements.createPDF) elements.createPDF.checked = true;

    // Reset patient/evaluation fields
    const numExpField = document.getElementById('review_numero_expediente');
    if (numExpField) numExpField.value = '';
    const curpField = document.getElementById('review_curp');
    if (curpField) curpField.value = '';
    setVal('review_codigo_cie11', '');
    document.getElementById('review_titulo_cie11').value = '';

    // Clear file input
    elements.audioFileInput.value = '';
    
    // Scroll to top
    window.scrollTo({ top: 0, behavior: 'smooth' });
}

function sleep(ms) {
    return new Promise(resolve => setTimeout(resolve, ms));
}

// ─────────────────────────────────────────────
// Aviso de Privacidad modal
// ─────────────────────────────────────────────
function showAvisoModal() {
    document.getElementById('avisoModal').style.display = 'flex';
    document.body.style.overflow = 'hidden';
}

function closeAvisoModal() {
    document.getElementById('avisoModal').style.display = 'none';
    document.body.style.overflow = '';
}

// Close on overlay click (but not on card click)
document.addEventListener('DOMContentLoaded', () => {
    const overlay = document.getElementById('avisoModal');
    if (overlay) {
        overlay.addEventListener('click', function(e) {
            if (e.target === this) closeAvisoModal();
        });
    }

});

// ─────────────────────────────────────────────
// Patient history (Item 24)
// ─────────────────────────────────────────────

const MAIN_SECTIONS = [
    'consentCard', 'progressSection', 'reviewSection', 'reviewActionsSection',
    'resultsSection', 'errorSection'
];

function _mainSections() {
    return [
        ...MAIN_SECTIONS.map(id => document.getElementById(id)),
        document.querySelector('.recording-section'),
        document.querySelector('.options-section'),
    ].filter(Boolean);
}

// Never persisted — always resets to 'mine' whenever the history view opens
let historialScope = 'mine';

function setHistorialScope(scope) {
    historialScope = scope;
    document.getElementById('historialScopeMine').classList.toggle('active', scope === 'mine');
    document.getElementById('historialScopeClinica').classList.toggle('active', scope === 'clinica');
    // Re-run the search if there's already a CURP entered, so the toggle feels live
    if (elements.historialCurpInput.value.trim()) searchPatientHistory();
}

function showHistorialView() {
    elements.pendingSessionsSection.style.display = 'none'; // only one standalone view open at a time
    _mainSections().forEach(el => { el._histSavedDisplay = el.style.display; el.style.display = 'none'; });
    elements.historialSection.style.display = 'block';
    elements.historialBtn.textContent = '← Volver';
    setHistorialScope('mine');
    showHistorialSearch();
}

function hideHistorialView() {
    elements.historialSection.style.display = 'none';
    _mainSections().forEach(el => { el.style.display = el._histSavedDisplay ?? ''; });
    elements.historialBtn.textContent = 'Historial';
}

function showHistorialSearch() {
    elements.historialSearch.style.display = 'block';
    elements.historialDetail.style.display = 'none';
}

// ─────────────────────────────────────────────
// Pending Sessions (own unfinished pending_review sessions)
// ─────────────────────────────────────────────

function showPendingSessionsView() {
    elements.historialSection.style.display = 'none'; // only one standalone view open at a time
    elements.pendingSessionsBanner.style.display = 'none'; // redundant once the list itself is open
    _mainSections().forEach(el => { el._pendingSavedDisplay = el.style.display; el.style.display = 'none'; });
    elements.pendingSessionsSection.style.display = 'block';
    elements.pendingSessionsBtnLabel.textContent = '← Volver';
    loadPendingSessionsList();
}

function hidePendingSessionsView() {
    elements.pendingSessionsSection.style.display = 'none';
    _mainSections().forEach(el => { el.style.display = el._pendingSavedDisplay ?? ''; });
    elements.pendingSessionsBtnLabel.textContent = 'Notas pendientes';
}

// Called once at init() — populates the nav badge and, if any pending
// sessions exist, the load-time banner. Deliberately assertive (not just
// the nav link) since these are unsigned notes, not something to leave
// easy to miss.
// Single source of truth for the nav badge count — called from every
// place the pending-sessions count can change (init, after confirm,
// after discard, when the list itself loads) so the badge can never
// independently drift out of sync the way the banner-only version of
// this logic just did.
function updatePendingBadge(count) {
    if (count > 0) {
        elements.pendingSessionsBadge.textContent = String(count);
        elements.pendingSessionsBadge.style.display = 'inline-block';
    } else {
        elements.pendingSessionsBadge.style.display = 'none';
    }
}

// Called at init() and after a confirm — the only two moments the
// load-time banner should be able to (re)appear. Deliberately NOT
// called by loadPendingSessionsList(): a doctor already looking at the
// list doesn't need the banner popping back in on top of it (see
// showPendingSessionsView(), which hides the banner on entry).
async function checkPendingSessions() {
    try {
        const res = await fetch(`${API_BASE_URL}/api/pending-sessions`, {
            headers: await getAuthHeaders()
        });
        if (res.status === 401) return handleSessionExpired();
        if (!res.ok) return;

        const sessions = await res.json();
        updatePendingBadge(sessions.length);

        if (!sessions.length) {
            // Must actively hide, not skip — this runs after a confirm too,
            // when the count may have just dropped to zero. A bare early
            // return would leave a stale banner from before the confirm.
            elements.pendingSessionsBanner.style.display = 'none';
            return;
        }

        elements.pendingSessionsBannerText.textContent = sessions.length === 1
            ? 'Tienes 1 nota procesada que aún no has revisado ni confirmado.'
            : `Tienes ${sessions.length} notas procesadas que aún no has revisado ni confirmado.`;
        elements.pendingSessionsBanner.style.display = 'block';

    } catch (err) {
        console.warn('[ClinIA] checkPendingSessions error:', err);
    }
}

async function loadPendingSessionsList() {
    elements.pendingSessionsList.innerHTML = '<p style="color: var(--text-secondary);">Cargando...</p>';

    try {
        const res = await fetch(`${API_BASE_URL}/api/pending-sessions`, {
            headers: await getAuthHeaders()
        });
        if (res.status === 401) return handleSessionExpired();
        if (!res.ok) throw new Error('Error al cargar notas pendientes');

        const sessions = await res.json();
        updatePendingBadge(sessions.length);

        if (!sessions.length) {
            elements.pendingSessionsList.innerHTML = '<p style="color: var(--text-secondary);">No tienes notas pendientes.</p>';
            elements.pendingSessionsBanner.style.display = 'none';
            return;
        }

        const rows = sessions.map(s => {
            const fecha = s.timestamp
                ? new Date(s.timestamp).toLocaleString('es-MX', { day: '2-digit', month: 'short', year: 'numeric', hour: '2-digit', minute: '2-digit' })
                : '—';
            const motivo = escapeHtml(s.motivo_de_consulta) || '—';
            const sid = escapeHtml(s.session_id);
            return `<div class="pending-row">
                <div class="pending-row-main">
                    <span class="pending-fecha">${fecha}</span>
                    <span class="pending-motivo">${motivo}</span>
                </div>
                <div class="pending-row-actions">
                    <button class="btn btn-primary btn-small pending-continue-btn" style="max-width: none; width: auto;" data-session-id="${sid}">Continuar revisión</button>
                    <button class="btn btn-danger btn-small pending-discard-btn" style="max-width: none; width: auto;" data-session-id="${sid}">Descartar</button>
                </div>
            </div>`;
        }).join('');

        elements.pendingSessionsList.innerHTML = `<div class="pending-list">${rows}</div>`;

    } catch (err) {
        console.error('[ClinIA] loadPendingSessionsList error:', err);
        elements.pendingSessionsList.innerHTML = '<p style="color: var(--text-secondary);">Error al cargar notas pendientes. Intente de nuevo.</p>';
    }
}

// Delegated listeners for pending-row buttons — rendered via innerHTML above,
// so no inline onclick (CSP script-src blocks inline handler attributes
// regardless of how they entered the DOM). Same pattern as admin.js's
// toggle-activo-btn delegation.
document.addEventListener('click', (e) => {
    const continueBtn = e.target.closest('.pending-continue-btn');
    if (continueBtn) {
        continuePendingSession(continueBtn.dataset.sessionId);
        return;
    }
    const discardBtn = e.target.closest('.pending-discard-btn');
    if (discardBtn) {
        discardPendingSession(discardBtn.dataset.sessionId);
    }
});

async function continuePendingSession(sessionId) {
    try {
        const res = await fetch(`${API_BASE_URL}/api/pending-sessions/${sessionId}`, {
            headers: await getAuthHeaders()
        });
        if (res.status === 401) return handleSessionExpired();
        if (!res.ok) {
            alert('No se pudo cargar esta nota. Puede que ya haya sido confirmada o descartada.');
            loadPendingSessionsList();
            return;
        }

        const data = await res.json();
        hidePendingSessionsView();
        state.sessionId = data.session_id;
        displayReviewScreen({
            session_id:      data.session_id,
            status:          'pending_review',
            transcript:      data.transcript,
            structured_data: data.structured_data,
        });

    } catch (err) {
        console.error('[ClinIA] continuePendingSession error:', err);
        alert('Error de red al cargar la nota. Intente de nuevo.');
    }
}

async function discardPendingSession(sessionId) {
    const ok = confirm('¿Descartar esta nota de forma permanente? Esta acción no se puede deshacer.');
    if (!ok) return;

    try {
        const res = await fetch(`${API_BASE_URL}/api/pending-sessions/${sessionId}`, {
            method: 'DELETE',
            headers: await getAuthHeaders()
        });
        if (res.status === 401) return handleSessionExpired();
        if (!res.ok) {
            let serverError = '';
            try { serverError = (await res.json()).error || ''; } catch (_) { /* non-JSON body */ }
            console.error(`[ClinIA] discardPendingSession failed: HTTP ${res.status}`, serverError);
            alert(serverError || 'No se pudo descartar la nota. Intente de nuevo.');
            return;
        }

        await loadPendingSessionsList();

    } catch (err) {
        console.error('[ClinIA] discardPendingSession error:', err);
        alert('Error de red al descartar la nota. Intente de nuevo.');
    }
}

async function searchHistorialBySessionId() {
    const input = document.getElementById('historialSessionIdInput');
    const errorEl = document.getElementById('historialSessionIdError');
    const sessionId = input.value.trim();

    errorEl.style.display = 'none';

    if (!sessionId) {
        errorEl.textContent = 'Ingrese un ID de sesión.';
        errorEl.style.display = 'block';
        return;
    }

    // Reuses the same GET /api/patient-history/<id> endpoint and detail
    // view already used for CURP-search results — no new backend route,
    // mirroring admin's Búsqueda ARCO fast path exactly. openHistoryDetail
    // already shows "Sesión no encontrada" inside the panel on 404, so no
    // separate not-found handling needed here.
    await openHistoryDetail(sessionId);
}

async function searchPatientHistory() {
    const raw = elements.historialCurpInput.value;
    const curp = raw.trim().toUpperCase();
    if (!curp) return;

    elements.historialResults.innerHTML = '<p style="color: var(--text-secondary);">Buscando...</p>';

    try {
        const url = `${API_BASE_URL}/api/patient-history?curp=${encodeURIComponent(curp)}&scope=${historialScope}`;
        const res = await fetch(url, { headers: await getAuthHeaders() });
        if (res.status === 401) return handleSessionExpired();
        if (!res.ok) throw new Error('Error al buscar historial');

        const sessions = await res.json();

        if (!sessions.length) {
            elements.historialResults.innerHTML = '<p style="color: var(--text-secondary);">No se encontraron consultas para este CURP.</p>';
            return;
        }

        const hasMissingMotivo = sessions.some(s => !s.motivo_de_consulta);
        if (hasMissingMotivo) {
            console.warn('[ClinIA] motivo_de_consulta vacío en uno o más resultados — verificar ruta JSONB en backend');
        }

        const rows = sessions.map(s => {
            const fecha = s.timestamp ? new Date(s.timestamp).toLocaleDateString('es-MX', { day: '2-digit', month: 'short', year: 'numeric' }) : '—';
            const statusLabel = s.status === 'confirmed' ? 'Confirmada' : s.status === 'cancelled' ? 'Cancelada' : (escapeHtml(s.status) || '—');
            const statusClass = s.status === 'confirmed' ? 'status-confirmed' : s.status === 'cancelled' ? 'status-cancelled' : 'status-other';
            const motivo = escapeHtml(s.motivo_de_consulta) || '—';
            const doctorLine = s.doctor_nombre
                ? `<div class="historial-doctor">Dr(a). ${escapeHtml(s.doctor_nombre)}</div>`
                : '';
            return `<div class="historial-row" data-session-id="${escapeHtml(s.session_id)}">
                <div class="historial-row-main">
                    <span class="historial-fecha">${fecha}</span>
                    <span class="historial-status ${statusClass}">${statusLabel}</span>
                </div>
                <div class="historial-motivo">${motivo}</div>
                ${doctorLine}
            </div>`;
        }).join('');

        elements.historialResults.innerHTML = `<div class="historial-list">${rows}</div>`;

    } catch (err) {
        console.error('[ClinIA] searchPatientHistory error:', err);
        elements.historialResults.innerHTML = '<p style="color: var(--text-secondary);">Error al cargar el historial. Intente de nuevo.</p>';
    }
}

// Delegated listener for historial-row clicks — rendered via innerHTML
// above, so no inline onclick (CSP script-src blocks inline handler
// attributes regardless of how they entered the DOM).
document.addEventListener('click', (e) => {
    const row = e.target.closest('.historial-row');
    if (row) openHistoryDetail(row.dataset.sessionId);
});

async function openHistoryDetail(sessionId) {
    elements.historialSearch.style.display = 'none';
    elements.historialDetail.style.display = 'block';
    elements.historialDetailContent.innerHTML = '<p style="color: var(--text-secondary);">Cargando...</p>';

    try {
        const res = await fetch(`${API_BASE_URL}/api/patient-history/${sessionId}`, {
            headers: await getAuthHeaders()
        });
        if (res.status === 401) return handleSessionExpired();
        if (res.status === 404) {
            elements.historialDetailContent.innerHTML = '<p>Sesión no encontrada.</p>';
            return;
        }
        if (!res.ok) throw new Error('Error al cargar sesión');

        const data = await res.json();
        renderSessionDetail(elements.historialDetailContent, data, {
            isAdmin: false,
            canDownloadPdf: true,
            onDownloadPdf: () => downloadHistorialSessionPdf(sessionId)
        });

    } catch (err) {
        console.error('[ClinIA] openHistoryDetail error:', err);
        elements.historialDetailContent.innerHTML = '<p>Error al cargar la nota. Intente de nuevo.</p>';
    }
}

async function downloadHistorialSessionPdf(sessionId) {
    // Same owner-only GET /api/download-pdf/<id> endpoint and blob-download
    // pattern already used by the main downloadPdfBtn handler above — no
    // new backend route, just reused for a session opened via Historial.
    try {
        const response = await fetch(`${API_BASE_URL}/api/download-pdf/${sessionId}`, {
            headers: await getAuthHeaders()
        });
        if (response.status === 401) return handleSessionExpired();
        if (!response.ok) {
            showError('Error al descargar el PDF');
            return;
        }
        const blob = await response.blob();
        const url = window.URL.createObjectURL(blob);
        const a = document.createElement('a');
        a.href = url;
        a.download = `ClinIA_${sessionId}.pdf`;
        document.body.appendChild(a);
        a.click();
        window.URL.revokeObjectURL(url);
        document.body.removeChild(a);
    } catch (error) {
        console.error('[ClinIA] Historial PDF download error:', error);
        showError('Error al descargar el PDF');
    }
}

// ─────────────────────────────────────────────
// Initialization is now driven entirely by the pageshow handler above —
// it fires on both the initial load and bfcache restores, and calls
// init() itself (guarded by _appInitialized) once the session-check
// validity fetch resolves.
// ─────────────────────────────────────────────
