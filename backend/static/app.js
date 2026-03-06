// frontend/app.js

// Global state
const state = {
    mediaRecorder: null,
    audioChunks: [],
    recordingStartTime: null,
    recordingInterval: null,
    currentAudioBlob: null,
    sessionId: null,
    isRecording: false,
    maxDurationSeconds: 300 // 5 minutes for Alpha
};

// API Configuration
const API_BASE_URL = window.location.origin.replace(/\/$/, '');
// DOM Elements
const elements = {
    recordBtn: document.getElementById('recordBtn'),
    stopBtn: document.getElementById('stopBtn'),
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
    
    progressSection: document.getElementById('progressSection'),
    progressFill: document.getElementById('progressFill'),
    progressText: document.getElementById('progressText'),
    
    resultsSection: document.getElementById('resultsSection'),
    documentLink: document.getElementById('documentLink'),
    docLink: document.getElementById('docLink'),
    transcriptResult: document.getElementById('transcriptResult'),
    transcriptConfidence: document.getElementById('transcriptConfidence'),
    transcriptDuration: document.getElementById('transcriptDuration'),
    transcriptWords: document.getElementById('transcriptWords'),
    transcriptText: document.getElementById('transcriptText'),
    structuredDataResult: document.getElementById('structuredDataResult'),
    formattedData: document.getElementById('formattedData'),
    jsonData: document.getElementById('jsonData'),
    downloadJsonBtn: document.getElementById('downloadJsonBtn'),
    
    errorSection: document.getElementById('errorSection'),
    errorMessage: document.getElementById('errorMessage'),
    retryBtn: document.getElementById('retryBtn')
};

// Initialize application
function init() {
    console.log('[ClinIA] Initializing application...');
    
    // Check browser compatibility
    if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
        showError('Tu navegador no soporta grabación de audio. Por favor usa Chrome, Firefox, Edge o Safari.');
        elements.recordBtn.disabled = true;
        return;
    }
    
    // Event listeners
    elements.recordBtn.addEventListener('click', startRecording);
    elements.stopBtn.addEventListener('click', stopRecording);
    elements.uploadBtn.addEventListener('click', () => elements.audioFileInput.click());
    elements.audioFileInput.addEventListener('change', handleFileUpload);
    elements.processBtn.addEventListener('click', processAudio);
    elements.retryBtn.addEventListener('click', resetApplication);
    elements.downloadJsonBtn.addEventListener('click', downloadJSON);
    
    // Tab switching
    document.querySelectorAll('.tab-btn').forEach(btn => {
        btn.addEventListener('click', (e) => {
            const tabName = e.target.dataset.tab;
            switchTab(tabName);
        });
    });
    
    console.log('[ClinIA] Application initialized successfully');
}

// Recording functions
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
        
        // Update UI
        elements.recordBtn.disabled = true;
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
    
    const elapsed = Math.floor((Date.now() - state.recordingStartTime) / 1000);
    const minutes = Math.floor(elapsed / 60);
    const seconds = elapsed % 60;
    
    elements.recordingTime.textContent = 
        `${String(minutes).padStart(2, '0')}:${String(seconds).padStart(2, '0')}`;
    
    // Auto-stop at max duration
    if (elapsed >= state.maxDurationSeconds) {
        console.log('[ClinIA] Maximum recording duration reached, stopping...');
        stopRecording();
        showError('Se alcanzó el límite de 5 minutos. La grabación se detuvo automáticamente.');
    }
}

function stopRecording() {
    if (!state.mediaRecorder || !state.isRecording) return;
    
    console.log('[ClinIA] Stopping recording...');
    
    state.mediaRecorder.stop();
    state.isRecording = false;
    
    // Stop all tracks
    state.mediaRecorder.stream.getTracks().forEach(track => track.stop());
    
    // Clear timer
    if (state.recordingInterval) {
        clearInterval(state.recordingInterval);
        state.recordingInterval = null;
    }
    
    // Update UI
    elements.recordBtn.disabled = false;
    elements.stopBtn.disabled = true;
    elements.uploadBtn.disabled = false;
    elements.recordingStatus.style.display = 'none';
    
    console.log('[ClinIA] Recording stopped');
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
    
    // Calculate duration
    const durationSeconds = (Date.now() - state.recordingStartTime) / 1000;
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
    
    // Validate file size (50MB max)
    const maxSize = 50 * 1024 * 1024;
    if (file.size > maxSize) {
        showError('El archivo es demasiado grande. Máximo 50MB.');
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

// Processing functions
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
        
        // Wait a bit for visual feedback
        await sleep(1000);
        
        updateProgress(60, 'Extrayendo información médica...', 2);
        await sleep(1000);
        
        updateProgress(90, 'Generando documento...', 3);
        
        // Get results
        const result = await response.json();
        
        console.log('[ClinIA] Processing completed:', result);
        
        updateProgress(100, '¡Completado!', 3);
        
        // Store session
        state.sessionId = result.session_id;
        
        // Display results
        await sleep(500);
        displayResults(result);
        
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
    
    // 2. Transcript
    if (result.transcript) {
        const t = result.transcript;
        
        elements.transcriptConfidence.textContent = 
            `Confianza: ${(t.confidence * 100).toFixed(1)}%`;
        elements.transcriptDuration.textContent = 
            `Duración: ${Math.floor(t.duration_seconds / 60)}:${String(Math.floor(t.duration_seconds % 60)).padStart(2, '0')}`;
        elements.transcriptWords.textContent = 
            `Palabras: ${t.word_count}`;
        
        elements.transcriptText.textContent = t.text;
    }
    
    // 3. Structured data
    if (result.structured_data) {
        displayStructuredData(result.structured_data);
    }
    
    // Scroll to results
    elements.resultsSection.scrollIntoView({ behavior: 'smooth', block: 'start' });
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

// Utility functions
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
    
    // Reset UI
    elements.audioPlayerContainer.style.display = 'none';
    elements.processBtn.disabled = true;
    elements.progressSection.style.display = 'none';
    elements.resultsSection.style.display = 'none';
    elements.errorSection.style.display = 'none';
    elements.recordingStatus.style.display = 'none';
    
    // Clear file input
    elements.audioFileInput.value = '';
    
    // Scroll to top
    window.scrollTo({ top: 0, behavior: 'smooth' });
}

function sleep(ms) {
    return new Promise(resolve => setTimeout(resolve, ms));
}

// Initialize on page load
if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
} else {
    init();
}



