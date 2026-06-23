// frontend/app.js
// CHANGES FROM PREVIOUS VERSION:
//   - Max recording duration: 5 min → 45 min
//   - Added pause/resume recording functionality
//   - Pause timer freezes and counts only active recording time
//   - File upload size limit raised: 50MB → 200MB
//   - Fixed error message text to say "45 minutos"

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
    createGoogleDoc: document.getElementById('createGoogleDoc'),
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
    errorSection: document.getElementById('errorSection'),
    errorMessage: document.getElementById('errorMessage'),
    retryBtn: document.getElementById('retryBtn')
};

// ─────────────────────────────────────────────
// Initialize application
// ─────────────────────────────────────────────
function init() {
    console.log('[ClinIA] Initializing application...');
    
    // Check browser compatibility
    if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
        showError('Tu navegador no soporta grabación de audio. Por favor usa Chrome, Firefox, Edge o Safari.');
        elements.recordBtn.disabled = true;
        return;
    }

    // Restore saved doctor email
    loadDoctorEmail();

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
        elements.recordBtn.disabled = !state.consentGiven;
    });

    // Event listeners
    elements.recordBtn.addEventListener('click', startRecording);
    elements.pauseBtn.addEventListener('click', togglePause);
    elements.stopBtn.addEventListener('click', stopRecording);
    elements.uploadBtn.addEventListener('click', () => elements.audioFileInput.click());
    elements.audioFileInput.addEventListener('change', handleFileUpload);
    elements.processBtn.addEventListener('click', processAudio);
    elements.retryBtn.addEventListener('click', resetApplication);
    elements.downloadJsonBtn.addEventListener('click', downloadJSON);
    elements.downloadPdfBtn.addEventListener('click', () => {
        if (!state.sessionId) {
            console.error('[ClinIA] No session ID available for PDF download');
            return;
        }
        const url = `${API_BASE_URL}/api/download-pdf/${state.sessionId}`;
        console.log('[ClinIA] Downloading PDF for session:', state.sessionId);
        const a = document.createElement('a');
        a.href = url;
        a.download = '';
        document.body.appendChild(a);
        a.click();
        document.body.removeChild(a);
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
    
    try {
        // Prepare form data
        const formData = new FormData();
        formData.append('audio', state.currentAudioBlob, 'recording.webm');
        formData.append('print_raw', elements.printRawTranscript.checked);
        formData.append('create_doc', elements.createGoogleDoc.checked);
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
            body: formData
        });

        if (!response.ok) {
            const errorData = await response.json();
            throw new Error(errorData.error || 'Error en el servidor');
        }

        updateProgress(30, 'Transcribiendo audio...', 1);
        await sleep(1000);

        updateProgress(70, 'Extrayendo información médica...', 2);
        await sleep(1000);

        updateProgress(100, 'Listo para revisión.', 2);

        // Get results
        const result = await response.json();
        console.log('[ClinIA] Processing completed:', result);

        // Store session
        state.sessionId = result.session_id;

        // Route to review screen
        await sleep(400);
        displayReviewScreen(result);
        
    } catch (error) {
        console.error('[ClinIA] Processing error:', error);
        showError(`Error durante el procesamiento: ${error.message}`);
        elements.progressSection.style.display = 'none';
    }
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
        elements.docLink.href = result.document.link;
        elements.docLink.innerHTML = `Abrir "${result.document.title}" en Google Docs <svg width="15" height="15" aria-hidden="true"><use href="#icon-external"/></svg>`;
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
    setVal('review_medico',             meta.medico);
    setVal('review_fecha_hora_consulta', meta.fecha_hora_consulta);

    // Populate transcript panel
    const t = result.transcript || {};
    const transcriptEl = document.getElementById('reviewTranscriptText');
    if (transcriptEl) {
        const raw = t.labeled_text || t.text || '';
        // Escape HTML, then bold [Persona X]: speaker labels in teal
        const escaped = raw
            .replace(/&/g, '&amp;')
            .replace(/</g, '&lt;')
            .replace(/>/g, '&gt;');
        transcriptEl.innerHTML = escaped.replace(
            /\[([^\]]+)\]:/g,
            '<strong class="review-speaker-label">[$1]:</strong>'
        );
    }

    elements.reviewSection.scrollIntoView({ behavior: 'smooth', block: 'start' });
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
    if (getVal('review_medico'))              meta.medico              = getVal('review_medico');
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
    elements.progressSection.style.display = 'block';
    updateProgress(10, 'Generando documento...', 3);

    try {
        const response = await fetch(`${API_BASE_URL}/api/confirm-and-generate`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                session_id: state.pendingResult.session_id,
                structured_data: sd,
                create_doc: elements.createGoogleDoc.checked,
                create_pdf: elements.createPDF ? elements.createPDF.checked : true,
                doctor_email: elements.doctorEmail?.value?.trim() || '',
                consent_tratamiento_given: elements.consentTratamiento ? elements.consentTratamiento.checked : false,
                consent_tratamiento_timestamp: new Date().toISOString()
            })
        });

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
    }
}

function cancelReview() {
    state.pendingResult = null;
    elements.reviewSection.style.display = 'none';
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
        const response = await fetch(`${API_BASE_URL}/api/export-json/${state.sessionId}`);
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
// Initialize on page load
// ─────────────────────────────────────────────
if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
} else {
    init();
}
