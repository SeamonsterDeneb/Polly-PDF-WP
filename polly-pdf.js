/**
 * Polly PDF Remediation System - Core Logic v0.4.0
 **/
(function() {
    function initPolly() {
        if (typeof PDFLib === 'undefined' || typeof window['pdfjs-dist/build/pdf'] === 'undefined') {
            setTimeout(initPolly, 40);
            return;
        }
        startPolly();
    }

    function startPolly() {
        const config = typeof pollyPdfConfig !== 'undefined' ? pollyPdfConfig : {
            model: 'gemini-2.0-flash',
            serverUrl: 'http://localhost:5001',
            choiceCount: 3,
            ajaxUrl: '/wp-admin/admin-ajax.php',
            nonce: ''
        };

        const { PDFDocument, PDFName } = PDFLib;
        const pdfjsLib = window['pdfjs-dist/build/pdf'];
        pdfjsLib.GlobalWorkerOptions.workerSrc = 'https://cdnjs.cloudflare.com/ajax/libs/pdf.js/3.11.174/pdf.worker.min.js';

        // Dynamic Alt Text Tips Rotator System
        const ALT_TEXT_TIPS = [
            'Keep it snappy — aim for about 20% shorter than a standard text message, roughly 125 characters or less.',
            'Skip "image of" or "photo of" — screen readers already announce that it\'s an image.',
            'Lead with the main subject first in case someone moves on before the description finishes.',
            'Describe what\'s actually visible — save interpretation and assumptions for the caption, not the alt text.',
            'If there\'s text inside the image, decide whether it needs to be transcribed into the alt text.',
            'Purely decorative? If it adds no understanding or emotional response to the page, mark it as a decorative artifact.',
            'Avoid repeating words already in the caption or surrounding text — alt text should add information.',
            'Keep sentences plain and simple — screen readers read alt text aloud, word for word.',
        ];
        const TIP_ROTATION_MS = 10000;

        // DOM Pointers
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

        // Logic Matrix State
        let remediationQueue = [];
        let isProcessingQueue = false;
        let remediatedCount = 0;
        let currentPdfFile = null; 
        let currentFileName = "";
        let remediationResults = {};

        // Frame Protection Bounds
        window.addEventListener('dragover', (e) => e.preventDefault(), false);
        window.addEventListener('drop', (e) => e.preventDefault(), false);

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

                const inspectForm = new FormData();
                inspectForm.append('pdf', file);
                let inspectData = { pages: {} };
                try {
                    const inspectResp = await fetch(`${config.serverUrl}/inspect`, {
                        method: 'POST',
                        body: inspectForm
                    });
                    if (inspectResp.ok) {
                        inspectData = await inspectResp.json();
                    }
                } catch(e) {
                    console.warn('Could not reach inspect endpoint:', e);
                }
                
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
                let pageCounts = {};

                for (let i = 1; i <= pdf.numPages; i++) {
                    loadingSub.innerText = `Scanning Page ${i} of ${pdf.numPages}...`;
                    const page = await pdf.getPage(i);
                    const operatorList = await page.getOperatorList();

                    // Track the canvas CTM stack trace state natively on the client side
                    let ctm = [1, 0, 0, 1, 0, 0];
                    let ctmStack = [];
                    
                    for (let j = 0; j < operatorList.fnArray.length; j++) {
                        const fn = operatorList.fnArray[j];
                        const args = operatorList.argsArray[j];

                        if (fn === pdfjsLib.OPS.save) {
                            ctmStack.push([...ctm]);
                        } else if (fn === pdfjsLib.OPS.restore) {
                            if (ctmStack.length > 0) ctm = ctmStack.pop();
                        } else if (fn === pdfjsLib.OPS.transform) {
                            const a1 = ctm[0], b1 = ctm[1], c1 = ctm[2], d1 = ctm[3], e1 = ctm[4], f1 = ctm[5];
                            const a2 = args[0], b2 = args[1], c2 = args[2], d2 = args[3], e2 = args[4], f2 = args[5];
                            
                            // Matrix multiplication tracking: Old CTM x Transform Matrix
                            ctm[0] = a1 * a2 + c1 * b2;
                            ctm[1] = b1 * a2 + d1 * b2;
                            ctm[2] = a1 * c2 + c1 * d2;
                            ctm[3] = b1 * c2 + d1 * d2;
                            ctm[4] = a1 * e2 + c1 * f2 + e1;
                            ctm[5] = b1 * e2 + d1 * f2 + f1;
                        } else if (fn === pdfjsLib.OPS.paintImageXObject || fn === pdfjsLib.OPS.paintJpegXObject) {
                            const imgName = args[0];
                            const imgObj = await new Promise(r => page.objs.get(imgName, r));
                            
                            if (imgObj) {
                                const pageIdx = i - 1;
                                const pageImages = (inspectData && inspectData.pages) ? (inspectData.pages[String(pageIdx)] || []) : [];
                                
                                let pageImgIndex;
                                const nameMatch = imgName.match(/img_p\d+_(\d+)/);
                                if (nameMatch) {
                                    pageImgIndex = parseInt(nameMatch[1]) - 1;
                                } else {
                                    pageImgIndex = pageCounts[pageIdx] || 0;
                                }
                                pageCounts[pageIdx] = (pageCounts[pageIdx] || 0) + 1;
                                
                                const imgMeta = pageImages[pageImgIndex] || null;
                                const existingAlt = imgMeta ? (imgMeta.existingAlt || '') : '';
                                
                                // Catch the flip: if cumulative rendering height scaling factor is negative, it is an inverted print asset
                                const isFlipped = ctm[3] < 0;
                                
                                foundCount++;
                                renderImageToGallery(imgObj, i, foundCount, pageIdx, imgName, existingAlt, pageImgIndex, isFlipped);
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

        function renderImageToGallery(imgObj, pageNum, index, pageIdx, imgName, existingAlt = '', pageImgIndex = 0, isFlipped = false) {
            const canvas = document.createElement('canvas');
            canvas.width = imgObj.width; canvas.height = imgObj.height;
            const ctx = canvas.getContext('2d');

            const scratchCanvas = document.createElement('canvas');
            scratchCanvas.width = imgObj.width; scratchCanvas.height = imgObj.height;
            const scratchCtx = scratchCanvas.getContext('2d');

            try {
                if (imgObj.bitmap) {
                    scratchCtx.drawImage(imgObj.bitmap, 0, 0);
                } else {
                    const totalPixels = imgObj.width * imgObj.height;
                    const rgbaData = imgObj.data.length === totalPixels * 3 ? new Uint8ClampedArray(totalPixels * 4) : imgObj.data;
                    if (imgObj.data.length === totalPixels * 3) {
                        for (let i = 0, j = 0; i < imgObj.data.length; i += 3, j += 4) {
                            rgbaData[j] = imgObj.data[i]; rgbaData[j+1] = imgObj.data[i+1];
                            rgbaData[j+2] = imgObj.data[i+2]; rgbaData[j+3] = 255;
                        }
                    }
                    scratchCtx.putImageData(new ImageData(rgbaData, imgObj.width, imgObj.height), 0, 0);
                }

                ctx.save();
                if (isFlipped) {
                    ctx.translate(0, canvas.height);
                    ctx.scale(1, -1);
                    console.log(`%c🦜 [Polly] Corrected Quartz vertical inversion on asset #${index}`, "color: #4a9c5d; font-weight: bold;");
                }
                ctx.drawImage(scratchCanvas, 0, 0);
                ctx.restore();

                const dataUrl = canvas.toDataURL('image/jpeg', 0.8);
                const alreadyTagged = existingAlt.length > 0;
                const card = document.createElement('div');
                card.className = 'image-card';
                card.id = `img-card-${index}`;
                card.style.padding = '12px';
                card.style.background = '#fff';
                card.innerHTML = `
                    <img src="${dataUrl}">
                    <div class="image-info" style="margin-top: 10px; font-size: 0.8rem;">
                        <span class="badge ${alreadyTagged ? 'badge-remediated' : 'badge-untagged'}" id="badge-${index}" style="display: inline-block; padding: 2px 6px; border-radius: 4px; font-weight: bold; text-transform: uppercase; font-size: 0.65rem; margin-bottom: 8px;">${alreadyTagged ? 'Already Tagged' : 'Untagged'}</span>
                        <div><strong>Asset #${index}</strong> (Page ${pageNum})</div>
                        <div id="content-${index}">
                            <textarea
                                id="alt-text-${index}"
                                rows="3"
                                style="width:100%; margin-top:10px; font-size:0.8rem; padding:6px; box-sizing:border-box; border:1px solid #ccc; border-radius:4px;"
                                placeholder="Alt text will appear here — edit before downloading..."
                            >${existingAlt}</textarea>
                            <button class="button button-secondary" id="btn-${index}" style="width: 100%; margin-top: 8px; font-weight: bold;" onclick="window.addToQueue(${index}, '${dataUrl.split(',')[1]}', ${pageIdx}, '${imgName}')">
                                ${alreadyTagged ? 'Preview & Refine' : 'Preview & Generate'}
                            </button>
                            ${alreadyTagged ? `<div style="margin-top:6px; font-size:0.7rem; color:#666;">Alt text found in document. Edit above or regenerate.</div>` : ''}
                        </div>
                    </div>
                `;
                imageList.appendChild(card);
                
                remediationResults[index] = {
                    pageIdx: pageIdx,
                    imgIdx: pageImgIndex,
                    imgName: imgName,
                    alt: existingAlt
                };

                const textarea = document.getElementById(`alt-text-${index}`);
                if (textarea) {
                    textarea.addEventListener('input', updateDownloadButton);
                }
            } catch(e) { console.error('renderImageToGallery error:', e); }
        }

        function trapFocus(modal) {
            const focusable = modal.querySelectorAll(
                'a[href], button:not([disabled]), textarea, input, select, [tabindex]:not([tabindex="-1"])'
            );
            const first = focusable[0];
            const last = focusable[focusable.length - 1];
            modal.addEventListener('keydown', function(e) {
                if (e.key === 'Escape') {
                    modal.dispatchEvent(new Event('polly-close'));
                    return;
                }
                if (e.key !== 'Tab') return;
                if (e.shiftKey) {
                    if (document.activeElement === first) {
                        e.preventDefault();
                        last.focus();
                    }
                } else {
                    if (document.activeElement === last) {
                        e.preventDefault();
                        first.focus();
                    }
                }
            });
        }

        function buildAltModalShell(imgSrc) {
            document.getElementById('polly-pdf-modal-overlay')?.remove();
            document.getElementById('polly-pdf-modal')?.remove();

            const overlay = document.createElement('div');
            overlay.id = 'polly-pdf-modal-overlay';

            const modal = document.createElement('div');
            modal.id = 'polly-pdf-modal';
            modal.setAttribute('role', 'dialog');
            modal.setAttribute('aria-modal', 'true');

            modal.innerHTML = `
                <div class="polly-pdf-modal-image-container" tabindex="0" aria-label="Widescreen asset view">
                    <img src="${imgSrc}" alt="">
                </div>
                <div class="polly-pdf-modal-header"><h3></h3></div>
                <div class="polly-pdf-modal-body"></div>
            `;

            const imgContainer = modal.querySelector('.polly-pdf-modal-image-container');
            const img = imgContainer.querySelector('img');
            img.onload = () => {
                imgContainer.scrollTop = (imgContainer.scrollHeight - imgContainer.clientHeight) / 2;
            };
            if (img.complete) {
                imgContainer.scrollTop = (imgContainer.scrollHeight - imgContainer.clientHeight) / 2;
            }

            return {
                overlay,
                modal,
                body: modal.querySelector('.polly-pdf-modal-body'),
                headerEl: modal.querySelector('.polly-pdf-modal-header h3'),
            };
        }

        // UPGRADED INTERFACE: Instantly draws widescreen dialog container with advice loops
        function showGeneratingModal(imgSrc, triggerBtn) {
            const { overlay, modal, body, headerEl } = buildAltModalShell(imgSrc);
            modal.setAttribute('aria-label', 'Generating Alt Text Suggestions');
            headerEl.textContent = '🦜 Hang tight, analysis running…';

            body.innerHTML = `
                <p class="polly-modal-intro" style="font-size:14px; line-height:1.5; margin:0 0 14px 0; color:#444;">
                    Get a close visual look at your document asset framing while Polly scans structure balances and constructs your alt suggestions:
                </p>
                <div class="polly-tip-rotator">
                    <span class="polly-tip-text"></span>
                </div>
                <div class="polly-pdf-modal-footer-btn-container" style="margin-top: 15px;">
                    <button type="button" id="polly-generating-cancel-btn" class="polly-pdf-modal-footer-btn">Cancel Request</button>
                </div>
            `;

            const tipText = body.querySelector('.polly-tip-text');
            let tipIndex = Math.floor(Math.random() * ALT_TEXT_TIPS.length);
            tipText.textContent = ALT_TEXT_TIPS[tipIndex];
            
            const tipInterval = setInterval(() => {
                tipIndex = (tipIndex + 1) % ALT_TEXT_TIPS.length;
                tipText.textContent = ALT_TEXT_TIPS[tipIndex];
            }, TIP_ROTATION_MS);

            const trigger = triggerBtn || document.activeElement;
            let dismissed = false;

            function dismiss() {
                if (dismissed) return;
                dismissed = true;
                clearInterval(tipInterval);
                overlay.remove();
                modal.remove();
                if (trigger) trigger.focus();
            }

            modal.addEventListener('polly-close', dismiss);
            overlay.onclick = dismiss;

            document.body.appendChild(overlay);
            document.body.appendChild(modal);
            
            modal.setAttribute('tabindex', '-1');
            modal.focus();

            body.querySelector('#polly-generating-cancel-btn').onclick = dismiss;
            trapFocus(modal);

            return {
                isDismissed: () => dismissed,
                dismiss: dismiss,
                
                updateStatus(text) {
                    if (dismissed) return;
                    headerEl.textContent = `🦜 ${text}`;
                },

                showError(message, onRetry) {
                    if (dismissed) return;
                    clearInterval(tipInterval);
                    headerEl.textContent = '🦜 Error processing image asset.';
                    body.innerHTML = `
                        <p style="font-size:14px; line-height:1.6; color:#d63638;">${message}</p>
                        <div style="display:flex; gap:10px; margin-top:20px;">
                            <button type="button" id="polly-error-retry-btn" class="polly-pdf-modal-footer-btn" style="background:#0073aa; color:#fff; border-color:#0073aa;">Try Again</button>
                            <button type="button" id="polly-error-close-btn" class="polly-pdf-modal-footer-btn">Close</button>
                        </div>
                    `;
                    body.querySelector('#polly-error-close-btn').onclick = dismiss;
                    body.querySelector('#polly-error-retry-btn').onclick = () => {
                        dismiss();
                        if (onRetry) onRetry();
                    };
                    body.querySelector('#polly-error-retry-btn').focus();
                },

                populate(choices, existingAlt, onSelect) {
                    if (dismissed) return;
                    clearInterval(tipInterval);
                    headerEl.textContent = '🦜 Choose Alt Text';
                    body.innerHTML = '';

                    const options = [];
                    if (existingAlt) {
                        options.push({ alt: existingAlt, focus: 'Current text', explanation: null, isOriginal: true });
                    }
                    choices.forEach(c => options.push({ ...c, isOriginal: false }));

                    options.forEach(opt => {
                        const item = document.createElement('div');
                        item.className = 'polly-pdf-choice-item';

                        const selectBtn = document.createElement('button');
                        selectBtn.type = 'button';
                        selectBtn.className = 'polly-pdf-choice-select-btn';
                        selectBtn.setAttribute('aria-label', `Select: ${opt.alt}`);

                        const tag = document.createElement('span');
                        tag.className = 'polly-pdf-choice-tag' + (opt.isOriginal ? ' original' : '');
                        tag.textContent = opt.isOriginal ? 'CURRENT TEXT' : (opt.focus || 'AI OPTION');

                        const content = document.createElement('div');
                        content.className = 'polly-pdf-choice-content';
                        content.textContent = opt.alt;

                        const charCount = document.createElement('span');
                        charCount.className = 'polly-pdf-choice-char-count' + (opt.alt.length > 125 ? ' over-limit' : '');
                        charCount.textContent = `${opt.alt.length} / 125 characters`;

                        selectBtn.appendChild(tag);
                        selectBtn.appendChild(content);
                        selectBtn.appendChild(charCount);

                        if (opt.explanation) {
                            const expl = document.createElement('div');
                            expl.className = 'polly-pdf-choice-explanation';
                            expl.textContent = opt.explanation;
                            selectBtn.appendChild(expl);
                        }

                        item.appendChild(selectBtn);

                        const editBtn = document.createElement('button');
                        editBtn.type = 'button';
                        editBtn.className = 'polly-pdf-modal-edit-btn';
                        editBtn.textContent = 'Edit';
                        editBtn.dataset.state = 'edit';
                        item.appendChild(editBtn);

                        selectBtn.onclick = () => {
                            const textarea = item.querySelector('.polly-pdf-choice-textarea');
                            const finalVal = textarea ? textarea.value : content.textContent;
                            onSelect(finalVal);
                            dismiss();
                        };

                        editBtn.onclick = (e) => {
                            e.stopPropagation();
                            if (editBtn.dataset.state === 'edit') {
                                const ta = document.createElement('textarea');
                                ta.className = 'polly-pdf-choice-textarea';
                                ta.value = content.textContent;
                                item.appendChild(ta);
                                editBtn.textContent = 'Apply';
                                editBtn.dataset.state = 'apply';
                                ta.addEventListener('input', () => {
                                    charCount.textContent = `${ta.value.length} / 125 characters`;
                                    charCount.className = 'polly-pdf-choice-char-count' + (ta.value.length > 125 ? ' over-limit' : '');
                                });
                                ta.addEventListener('click', e => e.stopPropagation());
                                ta.focus();
                            } else {
                                selectBtn.click();
                            }
                        };

                        body.appendChild(item);
                    });

                    const decoWrap = document.createElement('div');
                    decoWrap.style.cssText = 'margin-top:8px; padding:12px; background:#f9f9f9; border:1px solid #ddd; border-radius:6px;';
                    const decoId = `polly-pdf-deco-${Date.now()}`;
                    decoWrap.innerHTML = `
                        <label for="${decoId}" style="display:flex; align-items:flex-start; gap:10px; cursor:pointer; font-size:0.85rem; font-weight:600; color:#333;">
                            <input type="checkbox" id="${decoId}" style="margin-top:2px; transform:scale(1.1);">
                            Decorative Artifact
                        </label>
                        <span style="display:block; margin-top:6px; font-size:0.75rem; color:#666; font-style:italic; line-height:1.4;">Instruct screen readers to bypass this structural element entirely.</span>
                    `;
                    body.appendChild(decoWrap);

                    const decoCheck = decoWrap.querySelector('input[type="checkbox"]');
                    decoCheck.addEventListener('change', () => {
                        if (decoCheck.checked) {
                            onSelect('', true);
                            dismiss();
                        }
                    });

                    const cancelBtn = document.createElement('button');
                    cancelBtn.type = 'button';
                    cancelBtn.className = 'polly-pdf-modal-footer-btn';
                    cancelBtn.textContent = 'Keep Current & Close';
                    cancelBtn.onclick = dismiss;
                    body.appendChild(cancelBtn);

                    const firstChoice = body.querySelector('.polly-pdf-choice-select-btn');
                    if (firstChoice) firstChoice.focus({ focusVisible: true });
                    trapFocus(modal);
                }
            };
        }

        window.addToQueue = function(index, base64, pageIdx, imgName) {
            const badge = document.getElementById(`badge-${index}`);
            const btn = document.getElementById(`btn-${index}`);
            const container = document.getElementById(`content-${index}`);
            const imgDataUrl = `data:image/jpeg;base64,${base64}`;
            const currentAlt = document.getElementById(`alt-text-${index}`)?.value || '';

            badge.className = "badge badge-queued";
            badge.innerText = "Queued";
            if (btn) { btn.disabled = true; btn.innerText = "Queued..."; }

            // UPGRADE LOGIC: Launch widescreen display box right away!
            const modalCtl = showGeneratingModal(imgDataUrl, btn);

            remediationQueue.push({ index, base64, container, badge, pageIdx, imgName, modalCtl, currentAlt, imgDataUrl });
            processQueue();
        };

        async function processQueue() {
            if (isProcessingQueue || remediationQueue.length === 0) return;
            isProcessingQueue = true;
            
            const task = remediationQueue.shift();
            
            if (task.modalCtl.isDismissed()) {
                isProcessingQueue = false;
                setTimeout(processQueue, 50);
                return;
            }

            task.modalCtl.updateStatus("Pacing network request streams...");
            await new Promise(r => setTimeout(r, 2000)); 

            if (task.modalCtl.isDismissed()) {
                isProcessingQueue = false;
                setTimeout(processQueue, 50);
                return;
            }

            task.modalCtl.updateStatus("Analyzing visual compositions...");
            try {
                await runRemediation(task);
            } catch (err) {
                console.error(err);
                task.modalCtl.showError(`Analysis failed: ${err.message}`, () => {
                    window.addToQueue(task.index, task.base64, task.pageIdx, task.imgName);
                });
                
                // Reset card triggers if error drops out
                const triggerBtn = document.getElementById(`btn-${task.index}`);
                if (triggerBtn) {
                    triggerBtn.disabled = false;
                    triggerBtn.innerText = "Preview & Generate";
                }
            }
            isProcessingQueue = false;
            setTimeout(processQueue, 400);
        }

        async function runRemediation(task, retryCount = 0) {
            const prompt = `Task: Generate exactly ${config.choiceCount} distinct alt text variations for this image. Each must foreground a DIFFERENT visible subject or element as its opening focus. HARD CONSTRAINT: Each 'alt' value MUST NOT exceed 125 characters. Format: JSON array ONLY — no markdown, no preamble. Schema: [{"alt": "...", "focus": "short noun phrase naming the visual element", "explanation": "one sentence max"}, ...]`;

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
                    task.modalCtl.updateStatus(`Rate limited. Cooldown active (${waitTime/1000}s)...`);
                    await new Promise(r => setTimeout(r, waitTime));
                    return runRemediation(task, retryCount + 1);
                }
                throw new Error(resultData.data?.message || "Secure proxy engine error.");
            }

            const responsePayload = resultData.data;
            const rawText = responsePayload.candidates[0].content.parts[0].text
                .replace(/```json/g, '').replace(/```/g, '').trim();
            const choices = JSON.parse(rawText);

            // Re-render baseline text inputs back to sidebar container frame
            task.container.innerHTML = `
                <textarea
                    id="alt-text-${task.index}"
                    rows="3"
                    style="width:100%; margin-top:10px; font-size:0.8rem; padding:6px; box-sizing:border-box; border:1px solid #ccc; border-radius:4px;"
                >${task.currentAlt}</textarea>
                <button class="button button-secondary" id="btn-${task.index}" style="width: 100%; margin-top: 8px; font-weight: bold;" onclick="window.addToQueue(${task.index}, '${task.base64}', ${task.pageIdx}, '${task.imgName}')">
                    Preview & Refine
                </button>
            `;
            
            const freshTextarea = document.getElementById(`alt-text-${task.index}`);
            if (freshTextarea) {
                freshTextarea.addEventListener('input', updateDownloadButton);
            }

            // Populate choices inside active working display frame template
            task.modalCtl.populate(choices, task.currentAlt, (selectedText, isDecorative = false) => {
                const ta = document.getElementById(`alt-text-${task.index}`);
                if (ta) {
                    ta.value = selectedText;
                    ta.dispatchEvent(new Event('input'));
                }
                
                task.badge.className = `badge ${isDecorative ? 'badge-artifact' : 'badge-remediated'}`;
                task.badge.innerText = isDecorative ? 'Decorative Artifact' : 'Remediated';
                
                remediationResults[task.index] = {
                    ...remediationResults[task.index],
                    alt: selectedText,
                    decorative: isDecorative
                };
                
                if (!isDecorative) remediatedCount++;
                updateDownloadButton();
            });
        }

        function updateDownloadButton() {
            const filledCount = Array.from(
                document.querySelectorAll('textarea[id^="alt-text-"]')
            ).filter(t => t.value.trim().length > 0).length;

            const totalArtifacts = Object.values(remediationResults).filter(r => r.decorative).length;
            const absoluteActive = filledCount + totalArtifacts;

            readySub.innerText = `${absoluteActive} element asset description profiles ready for document compilation.`;
            downloadBtn.disabled = absoluteActive === 0;
        }

        downloadBtn.onclick = async () => {
            statusRight.innerText = "Transmitting compilation arrays...";
            downloadBtn.disabled = true;

            try {
                const formattedMetadata = {};
                document.querySelectorAll('textarea[id^="alt-text-"]').forEach(textarea => {
                    const idx = textarea.id.replace('alt-text-', '');
                    const altText = textarea.value.trim();
                    const isDecorative = remediationResults[idx]?.decorative || false;
                    
                    if (!altText && !isDecorative) return;
                    if (remediationResults[idx]) {
                        formattedMetadata[idx] = {
                            alt: altText,
                            pageIdx: remediationResults[idx].pageIdx,
                            imgName: remediationResults[idx].imgName,
                            imgIdx: remediationResults[idx].imgIdx,
                            decorative: isDecorative
                        };
                    }
                });

                const formPayload = new FormData();
                formPayload.append('pdf', currentPdfFile);
                formPayload.append('metadata', JSON.stringify(formattedMetadata));

                const remoteResponse = await fetch(`${config.serverUrl}/remediate`, {
                    method: 'POST',
                    body: formPayload
                });

                if (!remoteResponse.ok) {
                    const errorJson = await remoteResponse.json();
                    throw new Error(errorJson.error || "Remediation compiler engine failure.");
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

                statusRight.innerText = "Remediated file downloaded!";
            } catch(err) {
                console.error(err);
                alert("Compilation transmission failed: " + err.message);
                statusRight.innerText = "Compile Error";
            } finally {
                downloadBtn.disabled = false;
            }
        };
    }

    initPolly();
})();