<?php
/**
 * Plugin Name: Polly PDF & Fido Core
 * Description: Powered by SeaMonster Studios. Extracts images from uploads and performs serverless structural PDF/UA alt-tagging.
 * Version: 0.2.0
 * Author: SeaMonster Studios
 * Author URI: https://www.seamonsterstudios.com
 * Text Domain: polly-pdf
 */

if ( ! defined( 'ABSPATH' ) ) exit;

define( 'POLLY_PDF_VERSION', '0.2.0' );
define( 'POLLY_PDF_PLUGIN_FILE', __FILE__ );

function polly_pdf_get_available_models() {
    $api_key = get_option( 'polly_pdf_api_key', '' );
    if ( empty( $api_key ) ) {
        return [];
    }

    $transient_key = 'polly_pdf_models_list';
    $cached = get_transient( $transient_key );
    if ( false !== $cached ) {
        return $cached;
    }

    $url = 'https://generativelanguage.googleapis.com/v1beta/models?key=' . esc_attr( $api_key );
    $response = wp_remote_get( $url, [ 'timeout' => 15 ] );

    if ( is_wp_error( $response ) ) {
        return [ 'gemini-2.0-flash' => 'Gemini 2.0 Flash (Fallback)' ];
    }

    $body = wp_remote_retrieve_body( $response );
    $data = json_decode( $body, true );

    if ( empty( $data['models'] ) || ! is_array( $data['models'] ) ) {
        return [ 'gemini-2.0-flash' => 'Gemini 2.0 Flash (Fallback)' ];
    }

    $models = [];
    foreach ( $data['models'] as $m ) {
        if ( empty( $m['name'] ) || empty( $m['supportedGenerationMethods'] ) ) {
            continue;
        }
        if ( ! in_array( 'generateContent', $m['supportedGenerationMethods'], true ) ) {
            continue;
        }
        $clean_name = str_replace( 'models/', '', $m['name'] );
        if ( 
            strpos( $clean_name, 'embedding' ) !== false || 
            strpos( $clean_name, 'text' ) !== false || 
            strpos( $clean_name, 'tuning' ) !== false
        ) {
            continue;
        }
        $display_name = ! empty( $m['displayName'] ) ? $m['displayName'] : $clean_name;
        $models[ $clean_name ] = $display_name;
    }

    if ( empty( $models ) ) {
        $models = [ 'gemini-2.0-flash' => 'Gemini 2.0 Flash (Fallback)' ];
    }

    set_transient( $transient_key, $models, DAY_IN_SECONDS );
    return $models;
}

// =============================================================================
// 2. Add Menus
// =============================================================================

add_action( 'admin_menu', function () {
    // 1. Dedicated PDF Workspace screen
    add_menu_page(
        'Polly PDF Remediation',
        'Polly PDF',
        'manage_options',
        'polly-pdf-workspace',
        'polly_pdf_workspace_page',
        'dashicons-pdf',
        30
    );

    // 2. Settings sub-menu page
    add_submenu_page(
        'polly-pdf-workspace',
        'Polly PDF Settings',
        'Settings',
        'manage_options',
        'polly-pdf-settings',
        'polly_pdf_settings_page'
    );
} );

// Action links shortcut on the primary Plugins page row
add_filter( 'plugin_action_links_' . plugin_basename( POLLY_PDF_PLUGIN_FILE ), function ( $links ) {
    $settings_link = '<a href="' . esc_url( admin_url( 'admin.php?page=polly-pdf-settings' ) ) . '">'
        . __( 'Settings' ) . '</a>';
    array_unshift( $links, $settings_link );
    return $links;
} );

add_action( 'admin_init', function () {
    register_setting( 'polly_pdf_group', 'polly_pdf_api_key' );
    register_setting( 'polly_pdf_group', 'polly_pdf_model' );
    register_setting( 'polly_pdf_group', 'polly_pdf_server_url' );

    add_settings_section( 'polly_pdf_main_section', "Configuration", null, 'polly-pdf-settings' );

    add_settings_field( 'api_key', 'Gemini API Key', function () {
        $val = get_option( 'polly_pdf_api_key', '' );
        ?>
        <input type="password" name="polly_pdf_api_key" value="<?php echo esc_attr( $val ); ?>" class="regular-text" autocomplete="off">
        <p class="description">Get your API key at <a href="https://aistudio.google.com/app/apikey" target="_blank">Google AI Studio</a>.</p>
        <?php
    }, 'polly-pdf-settings', 'polly_pdf_main_section' );

    add_settings_field( 'model', 'AI Model', function () {
        $models = polly_pdf_get_available_models();
        if ( empty( $models ) ) {
            $models = [
                'gemini-2.0-flash' => 'Gemini 2.0 Flash',
                'gemini-1.5-flash' => 'Gemini 1.5 Flash',
            ];
        }

        // --- Dynamic Default Engine (Mirroring Polly Alt) ---
        // 1. Look for the absolute newest active flash model to mark as recommended
        $recommended_model = '';
        foreach ( array_keys( $models ) as $model_key ) {
            if ( strpos( $model_key, '-flash' ) !== false ) {
                if ( empty( $recommended_model ) || version_compare( $model_key, $recommended_model, '>' ) ) {
                    $recommended_model = $model_key;
                }
            }
        }
        
        // 2. Fall back to the first available model if no explicitly named flash model exists
        if ( empty( $recommended_model ) ) {
            $model_keys = array_keys( $models );
            $recommended_model = ! empty( $model_keys ) ? $model_keys[0] : '';
        }

        // 3. Check the database, defaulting to our dynamically calculated recommendation
        $current_model = get_option( 'polly_pdf_model', $recommended_model );
        ?>
        <select name="polly_pdf_model" id="polly-pdf-model" style="min-width: 250px;">
            <?php foreach ( $models as $value => $label ) : 
                // Dynamically append the label to the live recommended model
                $display_label = ( $recommended_model === $value ) ? $label . ' (Recommended)' : $label;
                ?>
                <option value="<?php echo esc_attr( $value ); ?>" <?php selected( $current_model, $value ); ?>>
                    <?php echo esc_html( $display_label ); ?>
                </option>
            <?php endforeach; ?>
        </select>
        <p class="description">
            Select the active Gemini model for PDF remediation. This list stays updated dynamically via Google's live endpoints.
        </p>
        <?php
    }, 'polly-pdf-settings', 'polly_pdf_main_section' );

    add_settings_field( 'server_url', 'Remediation Server URL', function () {
        $val = get_option( 'polly_pdf_server_url', 'http://localhost:5001' );
        ?>
        <input type="url" name="polly_pdf_server_url" value="<?php echo esc_url( $val ); ?>" class="regular-text" placeholder="http://localhost:5001">
        <p class="description">
            Your Python backend endpoint. Keep as <code>http://localhost:5001</code> for local testing, or change to SeaMonster's AWS Gateway URL once deployed!
        </p>
        <?php
    }, 'polly-pdf-settings', 'polly_pdf_main_section' );
} );

function polly_pdf_settings_page() {
    ?>
    <div class="wrap">
        <h1>⚙️ Polly PDF Settings</h1>
        <form method="post" action="options.php">
            <?php
            settings_fields( 'polly_pdf_group' );
            do_settings_sections( 'polly-pdf-settings' );
            submit_button();
            ?>
        </form>
    </div>
    <?php
}

add_action('wp_ajax_polly_pdf_proxy', function() {
    if (!check_ajax_referer('polly_pdf_nonce', 'nonce', false)) {
        wp_send_json_error(['message' => 'Security check failed.'], 403);
    }
    if (!current_user_can('upload_files')) {
        wp_send_json_error(['message' => 'Permission denied.'], 403);
    }

    $api_key = get_option('polly_pdf_api_key', '');
    if (!$api_key) {
        wp_send_json_error(['message' => 'No API key configured.'], 400);
    }

    $model = sanitize_text_field(wp_unslash($_POST['model'] ?? 'gemini-2.0-flash'));
    $body  = wp_unslash($_POST['payload'] ?? '');

    $response = wp_remote_post(
        "https://generativelanguage.googleapis.com/v1beta/models/{$model}:generateContent?key={$api_key}",
        [
            'headers' => ['Content-Type' => 'application/json'],
            'body'    => $body,
            'timeout' => 30,
        ]
    );

    if (is_wp_error($response)) {
        wp_send_json_error(['message' => $response->get_error_message()], 500);
    }

    $code = wp_remote_retrieve_response_code($response);
    $data = json_decode(wp_remote_retrieve_body($response), true);

    if ($code !== 200) {
        wp_send_json_error(['message' => $data['error']['message'] ?? 'Gemini error.'], $code);
    }

    wp_send_json_success($data);
});

function polly_pdf_workspace_page() {
    ?>
    <div class="wrap" style="margin-top: 20px;">
        <h1 style="margin-bottom: 20px;">🦜 Polly PDF & Fido Workspace</h1>
        
        <div id="polly-workspace-container" style="display: flex; height: calc(100vh - 160px); background: #f6f7f7; border: 1px solid #ccd0d4; box-shadow: 0 1px 1px rgba(0,0,0,.04); overflow: hidden;">
            
            <div id="gallery" style="width: 420px; background: white; border-right: 1px solid #ccd0d4; display: flex; flex-direction: column; box-shadow: 2px 0 5px rgba(0,0,0,0.02); z-index: 10;">
                <div class="gallery-header" style="padding: 20px; border-bottom: 1px solid #ccd0d4; background: #0073aa; color: white;">
                    <h2 style="margin: 0; font-size: 1.2rem; color: white; line-height: 1.2;">Polly Gallery</h2>
                    <p id="image-count" style="margin: 5px 0 0; opacity: 0.8; font-size: 0.85rem;">No PDF loaded</p>
                </div>
                
                <div id="image-list" style="flex-grow: 1; overflow-y: auto; padding: 15px;">
                    <div id="placeholder-msg" style="text-align: center; color: #888; margin-top: 50px; padding: 20px;">
                        <p>Drop a PDF document into the workspace to scan for assets.</p>
                    </div>
                </div>
            </div>

            <div id="workspace" style="flex-grow: 1; display: flex; flex-direction: column; align-items: center; justify-content: center; position: relative; background: #f6f7f7; padding: 40px; box-sizing: border-box;">
                <div id="drop-zone" style="width: 100%; max-width: 600px; height: 350px; border: 3px dashed #0073aa; border-radius: 20px; display: flex; flex-direction: column; align-items: center; justify-content: center; color: #0073aa; transition: all 0.3s; text-align: center; padding: 20px; background: white; box-sizing: border-box;">
                    
                    <div id="idle-view">
                        <svg width="64" height="64" viewBox="0 0 24 24" fill="currentColor" style="margin-bottom: 20px; opacity: 0.3; display: inline-block;">
                            <path d="M14,2H6A2,2 0 0,0 4,4V20A2,2 0 0,0 6,22H18A2,2 0 0,0 20,20V8L14,2M18,20H6V4H13V9H18V20Z"/>
                        </svg>
                        <h3 style="margin: 0 0 10px; font-size: 1.4rem;">System Ready</h3>
                        <p style="margin: 0; color: #666;">Drag and drop a PDF file here.</p>
                    </div>
                    
                    <div id="loading-view" class="hidden">
                        <div class="spinner" style="margin: 0 auto 20px; width: 40px; height: 40px; border: 3px solid #f3f3f3; border-top: 3px solid #0073aa; border-radius: 50%; animation: spin 1s linear infinite;"></div>
                        <h3 id="loading-title">Analyzing PDF...</h3>
                        <p id="loading-sub" style="margin: 0; color: #666;">Processing page streams...</p>
                    </div>

                    <div id="ready-view" class="hidden" style="width: 100%;">
                        <div id="compliance-info" style="margin-bottom: 20px;">
                            <span id="doc-type-badge" class="badge" style="display: inline-block; padding: 6px 12px; border-radius: 4px; font-weight: bold; text-transform: uppercase; font-size: 0.75rem;">Checking Structure...</span>
                        </div>
                        <h3 id="ready-title">PDF Processed</h3>
                        <p id="ready-sub" style="margin: 0 0 20px; color: #666;">0 images remediated.</p>
                        <button class="primary download-btn" id="download-btn" disabled style="background: #46b450; color: white; border: none; padding: 15px 30px; border-radius: 4px; cursor: pointer; font-weight: bold; display: flex; align-items: center; justify-content: center; gap: 10px; margin: 0 auto; font-size: 1rem;">
                            <svg width="20" height="20" viewBox="0 0 24 24" fill="white"><path d="M5,20H19V18H5M19,9H15V3H9V9H5L12,16L19,9Z"/></svg>
                            Download Remediated PDF
                        </button>
                    </div>
                </div>

                <style>
                    #drop-zone.hover { background: #f0faff !important; border-color: #00a0d2 !important; transform: scale(1.01); }
                    #drop-zone.has-file { border-style: solid !important; height: auto !important; padding: 40px !important; }
                    .hidden { display: none !important; }
                    @keyframes spin { 0% { transform: rotate(0deg); } 100% { transform: rotate(360deg); } }
                    .polly-badge-legacy { background: #fff3cd !important; color: #856404 !important; border: 1px solid #ffeeba !important; }
                    .polly-badge-tagged { background: #d4edda !important; color: #155724 !important; border: 1px solid #c3e6cb !important; }
                    .image-card { background: #f6f7f7; border: 1px solid #ccd0d4; border-radius: 8px; margin-bottom: 20px; overflow: hidden; }
                    .image-card img { width: 100%; height: 200px; object-fit: contain; background: #e5e5e5; display: block; }
                    .image-card .badge-untagged { background: #fcf0f1; color: #d63638; border: 1px solid #f1c3c4; }
                    .image-card .badge-queued { background: #fff8e5; color: #856404; border: 1px solid #ffeeba; }
                    .image-card .badge-remediated { background: #ecf7ed; color: #46b450; border: 1px solid #c1e8c5; }
                    .alt-result { margin-top: 10px; padding: 10px; background: #f9f9f9; border-left: 3px solid #0073aa; font-style: italic; word-wrap: break-word; font-size: 0.8rem; }
                    .char-counter { text-align: right; font-size: 0.7rem; color: #999; margin-top: 4px; }
                    .char-counter.over-limit { color: #d63638; font-weight: bold; }
                </style>

                <div id="status-bar" style="position: absolute; bottom: 0; left: 0; right: 0; padding: 10px 20px; background: white; border-top: 1px solid #ccd0d4; font-size: 0.85rem; display: flex; justify-content: space-between;">
                    <span id="status-left">Polly PDF & Fido Core v0.2.0</span>
                    <span id="status-right" style="font-weight: 600;">Idle</span>
                </div>
            </div>
        </div>
    </div>

    <!-- Enqueue JS Handlers & pdf-lib Injected Directly into Dashboard -->
    <script>
        (function() {
            const config = {
                model: '<?php echo esc_js( get_option( "polly_pdf_model", "gemini-2.0-flash" ) ); ?>',
                serverUrl: '<?php echo esc_js( get_option( "polly_pdf_server_url", "http://localhost:5001" ) ); ?>',
                ajaxUrl: '<?php echo esc_js( admin_url( "admin-ajax.php" ) ); ?>',
                nonce: '<?php echo esc_js( wp_create_nonce( "polly_pdf_nonce" ) ); ?>'
            };

            const { PDFDocument, PDFName } = PDFLib;
            const pdfjsLib = window['pdfjs-dist/build/pdf'];
            pdfjsLib.GlobalWorkerOptions.workerSrc = 'https://cdnjs.cloudflare.com/ajax/libs/pdf.js/3.11.174/pdf.worker.min.js';

            // DOM
            const dropZone = document.getElementById('drop-zone');
            const idleView = document.getElementById('idle-view');
            const loadingView = document.getElementById('loading-view');
            const loadingTitle = document.getElementById('loading-title');
            const loadingSub = document.getElementById('loading-sub');
            const readyView = document.getElementById('ready-view');
            const readySub = document.getElementById('ready-sub');
            const downloadBtn = document.getElementById('download-btn');
            const docTypeBadge = document.getElementById('doc-type-badge');
            const imageList = document.getElementById('image-list');
            const imageCountLabel = document.getElementById('image-count');
            const statusRight = document.getElementById('status-right');

            // State
            let remediationQueue = [];
            let isProcessingQueue = false;
            let remediatedCount = 0;
            let currentPdfFile = null; 
            let currentFileName = "";
            let remediationResults = {};

            // Drag & Drop
            dropZone.ondragover = (e) => { e.preventDefault(); dropZone.classList.add('hover'); };
            dropZone.ondragleave = () => dropZone.classList.remove('hover');
            dropZone.ondrop = async (e) => {
                e.preventDefault();
                dropZone.classList.remove('hover');
                const file = e.dataTransfer.files[0];
                if (file?.type === "application/pdf") {
                    currentPdfFile = file;
                    currentFileName = file.name.replace(".pdf", "-remediated.pdf");
                    processPDF(file);
                }
            };

            async function processPDF(file) {
                idleView.classList.add('hidden');
                readyView.classList.add('hidden');
                loadingView.classList.remove('hidden');
                imageList.innerHTML = "";
                remediatedCount = 0;
                remediationResults = {};

                try {
                    const arrayBuffer = await file.arrayBuffer();
                    
                    // Check Structure Root via PDF-lib
                    const pdfDocCheck = await PDFDocument.load(arrayBuffer.slice(0));
                    const isTagged = !!pdfDocCheck.catalog.get(PDFName.of('StructTreeRoot'));

                    if (isTagged) {
                        docTypeBadge.innerText = "Tagged PDF Structure Detected";
                        docTypeBadge.className = "badge polly-badge-tagged";
                    } else {
                        docTypeBadge.innerText = "Legacy/Untagged Document Detected";
                        docTypeBadge.className = "badge polly-badge-legacy";
                    }

                    const loadingTask = pdfjsLib.getDocument({
                        data: arrayBuffer.slice(0),
                        disableAutoFetch: true,
                        disableStream: true
                    });
                    const pdf = await loadingTask.promise;
                    let foundCount = 0;

                    for (let i = 1; i <= pdf.numPages; i++) {
                        loadingSub.innerText = `Scanning Page ${i} of ${pdf.numPages}...`;
                        const page = await pdf.getPage(i);
                        const operatorList = await page.getOperatorList();
                        
                        for (let j = 0; j < operatorList.fnArray.length; j++) {
                            if (operatorList.fnArray[j] === pdfjsLib.OPS.paintImageXObject || 
                                operatorList.fnArray[j] === pdfjsLib.OPS.paintJpegXObject) {
                                
                                const imgName = operatorList.argsArray[j][0];
                                const imgObj = await new Promise(r => page.objs.get(imgName, r));
                                if (imgObj) {
                                    foundCount++;
                                    renderImageToGallery(imgObj, i, foundCount, i - 1, imgName);
                                }
                            }
                        }
                    }

                    loadingView.classList.add('hidden');
                    readyView.classList.remove('hidden');
                    dropZone.classList.add('has-file');
                    imageCountLabel.innerText = `${foundCount} Assets Identified`;
                    updateDownloadButton();
                    statusRight.innerText = "Scanning complete";
                } catch (err) {
                    console.error(err);
                    loadingTitle.innerText = "Scanning Failed";
                    loadingView.classList.remove('hidden');
                }
            }

            function renderImageToGallery(imgObj, pageNum, index, pageIdx, imgName) {
                const canvas = document.createElement('canvas');
                canvas.width = imgObj.width; canvas.height = imgObj.height;
                const ctx = canvas.getContext('2d');
                try {
                    if (imgObj.bitmap) ctx.drawImage(imgObj.bitmap, 0, 0);
                    else {
                        const totalPixels = imgObj.width * imgObj.height;
                        const rgbaData = imgObj.data.length === totalPixels * 3 ? new Uint8ClampedArray(totalPixels * 4) : imgObj.data;
                        if (imgObj.data.length === totalPixels * 3) {
                            for (let i = 0, j = 0; i < imgObj.data.length; i += 3, j += 4) {
                                rgbaData[j] = imgObj.data[i]; rgbaData[j+1] = imgObj.data[i+1];
                                rgbaData[j+2] = imgObj.data[i+2]; rgbaData[j+3] = 255;
                            }
                        }
                        ctx.putImageData(new ImageData(rgbaData, imgObj.width, imgObj.height), 0, 0);
                    }
                    const dataUrl = canvas.toDataURL('image/jpeg', 0.8);
                    const card = document.createElement('div');
                    card.className = 'image-card';
                    card.id = `img-card-${index}`;
                    card.style.padding = '12px';
                    card.style.background = '#fff';
                    card.innerHTML = `
                        <img src="${dataUrl}">
                        <div class="image-info" style="margin-top: 10px; font-size: 0.8rem;">
                            <span class="badge badge-untagged" id="badge-${index}" style="display: inline-block; padding: 2px 6px; border-radius: 4px; font-weight: bold; text-transform: uppercase; font-size: 0.65rem; margin-bottom: 8px;">Untagged</span>
                            <div><strong>Asset #${index}</strong> (Page ${pageNum})</div>
                            <div id="content-${index}">
                                <button class="button button-secondary" id="btn-${index}" style="width: 100%; margin-top: 10px; font-weight: bold;" onclick="window.addToQueue(${index}, '${dataUrl.split(',')[1]}', ${pageIdx}, '${imgName}')">Recommend Alt Text</button>
                            </div>
                        </div>
                    `;
                    imageList.appendChild(card);
                } catch (e) { console.error(e); }
            }

            window.addToQueue = function(index, base64, pageIdx, imgName) {
                const badge = document.getElementById(`badge-${index}`);
                const btn = document.getElementById(`btn-${index}`);
                const container = document.getElementById(`content-${index}`);
                badge.className = "badge badge-queued";
                badge.innerText = "Queued";
                if (btn) { btn.disabled = true; btn.innerText = "Queued..."; }
                remediationQueue.push({ index, base64, container, badge, pageIdx, imgName });
                processQueue();
            }

            async function processQueue() {
                if (isProcessingQueue || remediationQueue.length === 0) return;
                isProcessingQueue = true;
                const task = remediationQueue.shift();
                
                task.container.innerHTML = `<div style="display:flex; align-items:center; gap:10px; margin-top:10px;"><div class="spinner" style="width: 18px; height: 18px; border: 2px solid #f3f3f3; border-top: 2px solid #0073aa; border-radius: 50%; animation: spin 1s linear infinite;"></div><span id="polly-feedback-${task.index}">Pacing request...</span></div>`;
                await new Promise(r => setTimeout(r, 2500)); 

                task.container.innerHTML = `<div style="display:flex; align-items:center; gap:10px; margin-top:10px;"><div class="spinner" style="width: 18px; height: 18px; border: 2px solid #f3f3f3; border-top: 2px solid #0073aa; border-radius: 50%; animation: spin 1s linear infinite;"></div><span id="polly-feedback-${task.index}">Analyzing...</span></div>`;
                try {
                    await runRemediation(task);
                } catch (err) {
                    task.container.innerHTML = `<p style="color:#d63638; font-size:0.75rem;">Failed: ${err.message}</p><button class="button button-primary" onclick="window.addToQueue(${task.index}, '${task.base64}', ${task.pageIdx}, '${task.imgName}')">Retry</button>`;
                }
                isProcessingQueue = false;
                setTimeout(processQueue, 500);
            }

            async function runRemediation(task, retryCount = 0) {
                const prompt = `Task: Generate accessibility alt text and technical logic for this image. HARD CONSTRAINT: The 'alt' value MUST NOT exceed 125 characters. LOGIC CONSTRAINT: The 'explanation' value must be no more than two sentences. Format: JSON ONLY {"alt": "...", "explanation": "..."}`;

                const payload = JSON.stringify({
                    contents: [{ parts: [{ text: prompt }, { inline_data: { mime_type: "image/jpeg", data: task.base64 } }] }],
                    generationConfig: { responseMimeType: 'application/json' }
                });

                const formData = new FormData();
                formData.append('action', 'polly_pdf_proxy');
                formData.append('nonce', config.nonce);
                formData.append('model', config.model);
                formData.append('payload', payload);

                const response = await fetch(config.ajaxUrl, {
                    method: 'POST',
                    body: formData
                });

                const resultData = await response.json();
                if (!resultData.success) {
                    if (response.status === 429 && retryCount < 4) {
                        const waitTime = Math.pow(2, retryCount) * 5000;
                        document.getElementById(`polly-feedback-${task.index}`).innerText = `Cooldown (${waitTime/1000}s)...`;
                        await new Promise(r => setTimeout(r, waitTime));
                        return runRemediation(task, retryCount + 1);
                    }
                    throw new Error(resultData.data?.message || "Secure proxy error.");
                }

                const responsePayload = resultData.data;
                const rawText = responsePayload.candidates[0].content.parts[0].text.replace(/```json/g, '').replace(/```/g, '').trim();
                const data = JSON.parse(rawText);
                
                remediationResults[task.index] = {
                    alt: data.alt,
                    pageIdx: task.pageIdx,
                    imgName: task.imgName
                };
                
                remediatedCount++;
                task.badge.className = "badge badge-remediated";
                task.badge.innerText = "Remediated";
                
                const isLong = data.alt.length > 125;
                task.container.innerHTML = `
                    <div class="alt-result">${data.alt}</div>
                    <div class="char-counter ${isLong ? 'over-limit' : ''}">${data.alt.length} / 125 characters</div>
                    <div class="explanation" style="margin-top: 8px; color: #666; font-size: 0.7rem;"><strong>Logic:</strong> ${data.explanation}</div>
                `;
                updateDownloadButton();
            }

            function updateDownloadButton() {
                readySub.innerText = `${remediatedCount} assets remediated.`;
                downloadBtn.disabled = remediatedCount === 0;
            }

            // Post compiled remediation payload to our python serverless backend
            downloadBtn.onclick = async () => {
                statusRight.innerText = "Transmitting to engine...";
                downloadBtn.disabled = true;

                try {
                    // Clean up our remediation results metadata map to make sure it includes the asset names
                    const formattedMetadata = {};
                    for (const [key, value] of Object.entries(remediationResults)) {
                        formattedMetadata[key] = {
                            alt: value.alt,
                            pageIdx: value.pageIdx,
                            imgName: value.imgName // Crucial Phase 3 addition!
                        };
                    }

                    const formPayload = new FormData();
                    formPayload.append('pdf', currentPdfFile);
                    formPayload.append('metadata', JSON.stringify(formattedMetadata));

                    const remoteResponse = await fetch(`${config.serverUrl}/remediate`, {
                        method: 'POST',
                        body: formPayload
                    });

                    if (!remoteResponse.ok) {
                        const errorJson = await remoteResponse.json();
                        throw new Error(errorJson.error || "Compiler service returned error state.");
                    }

                    const pdfBlob = await remoteResponse.blob();
                    const url = window.URL.createObjectURL(pdfBlob);
                    const link = document.createElement('a');
                    link.href = url;
                    link.download = currentFileName;
                    document.body.appendChild(link);
                    link.click();
                    link.remove();
                    window.URL.revokeObjectURL(url);

                    statusRight.innerText = "File fully compiled & saved!";
                } catch(err) {
                    console.error("Compile Service Fail", err);
                    alert("Compilation failed. Ensure your local Python microserver is running at: " + config.serverUrl + "\n\nError: " + err.message);
                    statusRight.innerText = "Compile Fail";
                } finally {
                    downloadBtn.disabled = false;
                }
            };
        })();
    </script>
    <?php
}

// Enqueue dependencies for our PDF dashboard screen
add_action( 'admin_enqueue_scripts', function ( $hook ) {
    if ( 'toplevel_page_polly-pdf-workspace' !== $hook ) return;

    wp_enqueue_script(
        'pdf-lib-script',
        'https://cdnjs.cloudflare.com/ajax/libs/pdf-lib/1.17.1/pdf-lib.min.js',
        [],
        null,
        false
    );

    wp_enqueue_script(
        'pdf-js-script',
        'https://cdnjs.cloudflare.com/ajax/libs/pdf.js/3.11.174/pdf.min.js',
        [],
        null,
        false
    );
} );