/**
 * CSV Converter page - Upload, map columns, convert, import.
 *
 * The converter maps an arbitrary CSV onto ListMonk's three import columns
 * (email, name, attributes). Because an import that silently stores empty
 * attributes looks exactly like a successful one, every step here states what
 * will be stored: the mapping step counts the attribute columns, the preview
 * shows the real converted rows, and the import dialog refuses to start when
 * the mapping would throw the file's data away.
 */
const Converter = {
    fileContent: null,
    fileName: '',
    columns: [],
    sampleRows: [],
    suggested: {},
    warnings: [],
    _mapping: null,
    _autoAttributes: true,
    _previewStats: null,
    _importing: false,
    _lists: [],
    step: 1,

    render() {
        this.step = 1;
        this.fileContent = null;
        this.fileName = '';
        this.columns = [];
        this.sampleRows = [];
        this.suggested = {};
        this.warnings = [];
        this._mapping = null;
        this._autoAttributes = true;
        this._previewStats = null;
        this._importing = false;
        this._lists = [];
        this.renderStep();
    },

    renderStep() {
        // Upload/detect/preview handlers re-render after awaits; never paint
        // over a page the user navigated to in the meantime.
        if (App.currentPage !== 'converter') return;
        let html = `
            <div class="steps">
                <div class="step ${this.step >= 1 ? (this.step > 1 ? 'completed' : 'active') : ''}">1. Upload CSV</div>
                <div class="step ${this.step >= 2 ? (this.step > 2 ? 'completed' : 'active') : ''}">2. Map Columns</div>
                <div class="step ${this.step >= 3 ? (this.step > 3 ? 'completed' : 'active') : ''}">3. Preview & Convert</div>
            </div>
        `;

        switch (this.step) {
            case 1: html += this.renderUpload(); break;
            case 2: html += this.renderMapping(); break;
            case 3: html += this.renderPreview(); break;
        }

        App.setContent(html);
        this.bindEvents();
    },

    // ── Step 1: Upload ───────────────────────────────────
    renderUpload() {
        return `
            <div class="card">
                <div class="card-header"><h2 class="card-title">Upload CSV File</h2></div>
                <div class="file-drop-zone" id="dropZone" role="button" tabindex="0"
                     aria-label="Choose a CSV file — press Enter or Space to browse">
                    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                        <polyline points="16 16 12 12 8 16"/><line x1="12" y1="12" x2="12" y2="21"/>
                        <path d="M20.39 18.39A5 5 0 0 0 18 9h-1.26A8 8 0 1 0 3 16.3"/>
                    </svg>
                    <p>Drag & drop a CSV file here, or click to browse</p>
                    <p class="file-name" id="fileName">${App.escapeHtml(this.fileName || '')}</p>
                    <input type="file" id="csvFile" accept=".csv,.tsv,.txt" aria-label="Choose a CSV file" style="display:none">
                </div>
                <p style="margin-top:12px;font-size:0.85rem;color:var(--text-muted)">
                    Supports CSV, TSV, and text files. The converter will automatically detect the delimiter,
                    encoding, and columns, then map them onto ListMonk's three import columns:
                    <strong>email</strong>, <strong>name</strong>, and <strong>attributes</strong> (JSON).
                    Any column you do not map explicitly is preserved inside <strong>attributes</strong>.
                </p>
            </div>
        `;
    },

    // ── Step 2: Column Mapping ───────────────────────────
    renderMapping() {
        // Pre-select the guessed email/name columns so obvious headers
        // ("Email Address", "Full Name") need no manual mapping. Choices
        // already made win over the guess so a re-render never resets them
        // (including an explicit "-- None --" for the name column).
        if (!this._mapping || !this._mapping._initialized) {
            this._mapping = {
                emailCol: this.suggested.email || '',
                nameCol: this.suggested.name || '',
                attrCols: [],
                auto: this._autoAttributes,
                _initialized: true,
            };
        }
        const emailSel = this._mapping.emailCol;
        const nameSel = this._mapping.nameCol;
        const checkedAttrs = new Set(this._mapping.attrCols || []);
        const colOptions = this.columns.map(c =>
            `<option value="${App.escapeHtml(c)}"${emailSel === c ? ' selected' : ''}>${App.escapeHtml(c)}</option>`
        ).join('');
        const nameOptions = this.columns.map(c =>
            `<option value="${App.escapeHtml(c)}"${nameSel === c ? ' selected' : ''}>${App.escapeHtml(c)}</option>`
        ).join('');

        let attrCheckboxes = this.columns.map(c =>
            `<label class="checkbox-label"><input type="checkbox" name="attrCols" value="${App.escapeHtml(c)}"${checkedAttrs.has(c) ? ' checked' : ''}><span>${App.escapeHtml(c)}</span></label>`
        ).join('');

        // Sample data table: wide lead files (40+ columns) are unreadable as a
        // full grid, so the preview shows the first columns and reports how
        // many more exist. Cells are truncated instead of wrapped.
        const SAMPLE_COLS = 8;
        const shownCols = this.columns.slice(0, SAMPLE_COLS);
        const hiddenCols = this.columns.length - shownCols.length;
        let sampleHtml = '';
        if (this.sampleRows.length) {
            sampleHtml = `
                <div class="form-group" style="margin-top:4px">
                    <label>Sample data <span style="font-weight:400;color:var(--text-muted)">(first ${this.sampleRows.length} rows${hiddenCols > 0 ? `, ${shownCols.length} of ${this.columns.length} columns` : ''})</span></label>
                    <div class="table-wrapper sample-table-wrap"><table class="sample-table"><thead><tr>`;
            shownCols.forEach(c => { sampleHtml += `<th>${App.escapeHtml(c)}</th>`; });
            if (hiddenCols > 0) sampleHtml += `<th class="more-cols">+${hiddenCols} more</th>`;
            sampleHtml += '</tr></thead><tbody>';
            this.sampleRows.forEach(row => {
                sampleHtml += '<tr>';
                shownCols.forEach(c => { sampleHtml += `<td title="${App.escapeHtml(row[c] || '')}">${App.escapeHtml(row[c] || '')}</td>`; });
                if (hiddenCols > 0) sampleHtml += '<td class="more-cols">…</td>';
                sampleHtml += '</tr>';
            });
            sampleHtml += '</tbody></table></div></div>';
        }

        // In auto mode ListMonk receives one attribute per unmapped column, so
        // say exactly which columns that will be - as a count plus a scrollable
        // list, not a wall of text.
        const auto = this._autoAttributes;
        const skipped = new Set([emailSel, nameSel].filter(Boolean));
        const autoCols = auto ? this.columns.filter(c => !skipped.has(c)) : [];
        const pickedCount = checkedAttrs.size;
        const attrCount = auto ? autoCols.length : pickedCount;
        const nothingPicked = !auto && pickedCount === 0 && this.columns.length > skipped.size;

        const autoHint = auto
            ? (autoCols.length
                ? `Every column except <strong>${App.escapeHtml(emailSel || 'the email column')}${nameSel ? ` and ${App.escapeHtml(nameSel)}` : ''}</strong> is stored as a JSON attribute.`
                : 'No extra columns found - every column is already mapped to email/name.')
            : (pickedCount
                ? `Only the ${pickedCount} checked column${pickedCount === 1 ? '' : 's'} will be stored; the other ${Math.max(0, this.columns.length - skipped.size - pickedCount)} are dropped.`
                : 'No attribute columns are checked - every column except email/name would be dropped.');

        return `
            <div class="card">
                <div class="card-header">
                    <h2 class="card-title">Map Columns</h2>
                    <span style="color:var(--text-muted);font-size:0.85rem">${App.escapeHtml(this.fileName)} - ${this.columns.length} columns detected</span>
                </div>
                ${this.renderWarnings()}
                <div class="form-grid">
                    <div class="form-group">
                        <label>Email Column * (required)</label>
                        <select id="emailCol" onchange="Converter.preserveMapping()">${colOptions}</select>
                    </div>
                    <div class="form-group">
                        <label>Name Column (optional)</label>
                        <select id="nameCol" onchange="Converter.preserveMapping()"><option value="">-- None --</option>${nameOptions}</select>
                    </div>
                </div>

                <label class="switch-row ${auto ? 'on' : ''}" id="autoRow">
                    <span class="switch">
                        <input type="checkbox" id="autoAttrs" ${auto ? 'checked' : ''} onchange="Converter.showAutoAttrs(this.checked)">
                        <span class="slider"></span>
                    </span>
                    <span class="switch-text">
                        <span class="switch-title">Convert all other columns to attributes
                            <span class="chip ${auto ? 'ok' : ''}">${attrCount} attribute${attrCount === 1 ? '' : 's'}</span>
                        </span>
                        <span class="switch-desc">Recommended. Keeps every column of your file inside ListMonk's attributes JSON, so no lead data is lost. Turn it off to hand-pick the columns to keep.</span>
                    </span>
                </label>

                <div class="form-group" id="attrPicker" style="display:${auto ? 'none' : 'block'};margin-top:14px">
                    <label>Attribute Columns (stored as JSON in ListMonk)
                        <span style="float:right;font-weight:400">
                            <button type="button" class="btn btn-sm btn-secondary" onclick="Converter.toggleAllAttrs(true)">Select all</button>
                            <button type="button" class="btn btn-sm btn-secondary" onclick="Converter.toggleAllAttrs(false)">Clear</button>
                        </span>
                    </label>
                    <div class="attr-picker"><div class="checkbox-group">${attrCheckboxes}</div></div>
                    ${nothingPicked ? `
                    <div class="notice notice-danger" style="margin-top:10px">
                        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"/><line x1="12" y1="8" x2="12" y2="12"/><line x1="12" y1="16" x2="12.01" y2="16"/></svg>
                        <div>No attribute columns selected. Continuing would import e-mail and name only and <strong>drop every other column</strong>. Check the columns to keep, or turn the switch above back on.</div>
                    </div>` : ''}
                </div>
                <p style="font-size:0.83rem;color:var(--text-muted);margin:10px 0 0">${autoHint}</p>
                ${sampleHtml}
                <div class="form-actions">
                    <button class="btn btn-primary" onclick="Converter.processMapping()">Next: Preview</button>
                    <button class="btn btn-secondary" onclick="Converter.step=1;Converter.renderStep()">Back</button>
                </div>
            </div>
        `;
    },

    // Snapshot the live mapping widgets so a re-render keeps them, then re-render.
    preserveMapping() {
        const emailEl = document.getElementById('emailCol');
        const nameEl = document.getElementById('nameCol');
        const picker = document.getElementById('attrPicker');
        const pickerVisible = !!picker && picker.style.display !== 'none';
        if (!this._mapping) {
            this._mapping = { emailCol: '', nameCol: '', attrCols: [], auto: this._autoAttributes, _initialized: true };
        }
        if (emailEl) this._mapping.emailCol = emailEl.value;
        if (nameEl) this._mapping.nameCol = nameEl.value;
        // The attribute picker is hidden in auto mode, so its checkboxes would
        // report "nothing checked" and wipe a hand-picked selection. Only read
        // them when the picker is visible.
        if (pickerVisible) {
            this._mapping.attrCols = Array.from(document.querySelectorAll('input[name="attrCols"]:checked')).map(cb => cb.value);
        }
        this.renderStep();
    },

    // Toggle between "keep every column" and hand-picked attributes.
    showAutoAttrs(checked) {
        if (!this._mapping) {
            this._mapping = { emailCol: '', nameCol: '', attrCols: [], auto: checked, _initialized: true };
        }
        // When the picker is visible the user may have just changed it; snapshot
        // the checked columns before the mode switch hides them again.
        const picker = document.getElementById('attrPicker');
        if (picker && picker.style.display !== 'none') {
            this._mapping.attrCols = Array.from(document.querySelectorAll('input[name="attrCols"]:checked')).map(cb => cb.value);
        }
        this._autoAttributes = checked;
        this._mapping.auto = checked;
        this.renderStep();
    },

    // Select / clear every attribute checkbox in one go (lead files easily
    // have 40+ columns, so ticking them by hand is not an option).
    toggleAllAttrs(on) {
        const boxes = Array.from(document.querySelectorAll('input[name="attrCols"]'));
        boxes.forEach(cb => { cb.checked = on; });
        this._mapping.attrCols = on ? boxes.map(cb => cb.value) : [];
        this.renderStep();
    },

    // ── Warnings ─────────────────────────────────────────
    renderWarnings() {
        const warnings = this.warnings || [];
        if (!warnings.length) return '';
        return `<div class="notice notice-warning" style="margin-bottom:16px">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M10.29 3.86L1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z"/><line x1="12" y1="9" x2="12" y2="13"/><line x1="12" y1="17" x2="12.01" y2="17"/></svg>
            <div>${warnings.map(w => App.escapeHtml(w)).join('<br>')}</div>
        </div>`;
    },

    // ── Step 3: Preview & Actions ────────────────────────
    renderPreview() {
        return `
            <div class="card" style="margin-bottom:20px">
                <div class="card-header">
                    <h2 class="card-title">Conversion Preview</h2>
                    <span style="color:var(--text-muted);font-size:0.85rem">${App.escapeHtml(this.fileName)}</span>
                </div>
                <div id="previewChips" class="chip-row" style="margin-bottom:12px"></div>
                <p id="previewStats" role="status" aria-live="polite" style="color:var(--text-secondary);font-size:0.9rem;margin-bottom:12px"></p>
                <div id="previewNotice"></div>
                <div id="previewTable"><p style="color:var(--text-muted)">Converting a sample…</p></div>
            </div>
            <div class="card">
                <div class="card-header">
                    <h2 class="card-title">What would you like to do?</h2>
                </div>
                <div style="display:flex;gap:12px;flex-wrap:wrap">
                    <button class="btn btn-primary" onclick="Converter.downloadConverted()">Download Converted CSV</button>
                    <button class="btn btn-success" onclick="Converter.showImportDialog()">Import to ListMonk</button>
                    <button class="btn btn-secondary" onclick="Converter.step=2;Converter.renderStep()">Back to Mapping</button>
                </div>
                <div id="importDialog" style="display:none;margin-top:20px"></div>
            </div>
        `;
    },

    // ── Event Bindings ───────────────────────────────────
    bindEvents() {
        if (this.step === 1) {
            const dropZone = document.getElementById('dropZone');
            const fileInput = document.getElementById('csvFile');

            if (dropZone && fileInput) {
                dropZone.addEventListener('click', () => fileInput.click());

                // The dropzone is the file picker's only visible control; a
                // keyboard user reaches it via Tab and opens it with Enter.
                dropZone.addEventListener('keydown', (e) => {
                    if (e.key === 'Enter' || e.key === ' ') {
                        e.preventDefault();
                        fileInput.click();
                    }
                });

                dropZone.addEventListener('dragover', (e) => {
                    e.preventDefault();
                    dropZone.classList.add('dragover');
                });

                dropZone.addEventListener('dragleave', () => {
                    dropZone.classList.remove('dragover');
                });

                dropZone.addEventListener('drop', (e) => {
                    e.preventDefault();
                    dropZone.classList.remove('dragover');
                    if (e.dataTransfer.files.length) {
                        this.handleFile(e.dataTransfer.files[0]);
                    }
                });

                fileInput.addEventListener('change', (e) => {
                    if (e.target.files.length) {
                        this.handleFile(e.target.files[0]);
                    }
                });
            }
        }
    },

    // ── Handle File Upload ───────────────────────────────
    async handleFile(file) {
        this.fileName = file.name;
        document.getElementById('fileName').textContent = file.name;

        // A new file invalidates every mapping choice made for the previous one.
        this._mapping = null;
        this._previewStats = null;

        const formData = new FormData();
        formData.append('file', file);

        // Store file for later use
        this.fileContent = file;

        try {
            const result = await API.upload('/api/converter/detect-columns', formData);
            this.columns = result.columns || [];
            this.sampleRows = result.sample_rows || [];
            this.suggested = result.suggested || {};
            this.warnings = result.warnings || [];

            if (!this.columns.length) {
                App.toast('No columns detected in file', 'error');
                return;
            }

            App.toast(`Detected ${this.columns.length} columns`, 'success');
            this.step = 2;
            this.renderStep();
        } catch {
            // API.request already surfaced the server message.
        }
    },

    // ── Process Mapping & Generate Preview ───────────────
    async processMapping() {
        const emailEl = document.getElementById('emailCol');
        const nameEl = document.getElementById('nameCol');
        const emailCol = emailEl ? emailEl.value : (this._mapping?.emailCol || '');
        const nameCol = nameEl ? nameEl.value : (this._mapping?.nameCol || '');
        const auto = document.getElementById('autoAttrs')?.checked ?? this._autoAttributes;
        const attrCols = Array.from(document.querySelectorAll('input[name="attrCols"]:checked'))
            .map(cb => cb.value);

        if (!emailCol) { App.toast('Email column is required', 'error'); return; }

        // Stop an import that would throw the whole file away before it even
        // reaches the server: selected mode with nothing checked.
        if (!auto && !attrCols.length && this.columns.length > [emailCol, nameCol].filter(Boolean).length) {
            App.toast('Select the columns to store as attributes, or turn auto-attributes back on', 'error');
            this.step = 2;
            this.renderStep();
            return;
        }

        // Store mapping for later
        this._mapping = { emailCol, nameCol, attrCols, auto, _initialized: true };
        this._autoAttributes = auto;
        this._previewStats = null;

        this.step = 3;
        this.renderStep();

        // Show exactly what ListMonk will receive, converted by the same
        // backend code that runs the real import.
        const formData = this.buildMappingFormData();
        try {
            const result = await API.upload('/api/converter/preview', formData);
            this._previewStats = result?.stats || null;
            this.renderPreviewResult(result?.rows || [], this._previewStats);
        } catch {
            // API.request already surfaced the server message; leave a hint in
            // the preview area instead of a stuck "Converting a sample…".
            this.renderPreviewError();
        }
    },

    renderPreviewError() {
        const statEl = document.getElementById('previewStats');
        const chipsEl = document.getElementById('previewChips');
        const tableEl = document.getElementById('previewTable');
        if (statEl) statEl.textContent = '';
        if (chipsEl) chipsEl.innerHTML = '';
        if (tableEl) {
            tableEl.innerHTML = '<p style="color:var(--text-muted)">Preview failed - adjust the mapping and try again.</p>';
        }
    },

    // The attribute mapping shared by preview, download and import.
    buildMappingFormData() {
        const formData = new FormData();
        // `auto` may be missing from mappings created before this field
        // existed; fall back to the live switch state so the request never
        // silently downgrades to "selected" mode with no columns.
        const auto = this._mapping.auto ?? this._autoAttributes;
        formData.append('file', this.fileContent);
        formData.append('email_column', this._mapping.emailCol);
        if (this._mapping.nameCol) formData.append('name_column', this._mapping.nameCol);
        formData.append('attribute_columns', JSON.stringify(this._mapping.attrCols || []));
        formData.append('attribute_mode', auto ? 'auto' : 'selected');
        return formData;
    },

    // Renders the converted rows returned by /api/converter/preview.
    renderPreviewResult(rows, stats) {
        const statEl = document.getElementById('previewStats');
        const chipsEl = document.getElementById('previewChips');
        const noticeEl = document.getElementById('previewNotice');
        const tableEl = document.getElementById('previewTable');
        if (!tableEl) return;

        if (chipsEl) {
            const attrCount = stats?.attribute_count ?? 0;
            const chips = [
                `<span class="chip"><strong>${stats?.converted ?? 0}</strong> rows converted</span>`,
                stats?.skipped ? `<span class="chip warn"><strong>${stats.skipped}</strong> skipped</span>` : '',
                `<span class="chip ${attrCount ? 'ok' : 'danger'}"><strong>${attrCount}</strong> attribute${attrCount === 1 ? '' : 's'} per row</span>`,
                stats?.delimiter ? `<span class="chip">delimiter: ${App.escapeHtml(stats.delimiter)}</span>` : '',
                stats?.encoding_detected ? `<span class="chip">${App.escapeHtml(stats.encoding_detected)}</span>` : '',
            ].filter(Boolean);
            chipsEl.innerHTML = chips.join('');
        }

        if (statEl) {
            const skipped = stats?.skipped || 0;
            statEl.textContent = stats
                ? `${stats.converted} of ${stats.total} rows converted${skipped ? ` - ${skipped} skipped` : ''}`
                : '';
        }

        if (noticeEl) {
            const notices = [];
            if (stats?.attributes_empty) {
                notices.push({
                    kind: 'danger',
                    text: 'This mapping stores NO attributes: ListMonk will receive e-mail and name only, and every other column of your file is dropped. Go back and enable attributes (or pick columns) unless that is exactly what you want.',
                });
            }
            if (stats?.empty_attributes && !stats?.attributes_empty) {
                notices.push({
                    kind: 'warning',
                    text: `${stats.empty_attributes} of ${stats.converted} rows have no values in any attribute column - they will be imported with empty attributes.`,
                });
            }
            if (stats?.invalid_emails) {
                notices.push({ kind: 'warning', text: `${stats.invalid_emails} row(s) have a malformed e-mail address and will be skipped (ListMonk would reject them too).` });
            }
            if (stats?.empty_emails) {
                notices.push({ kind: 'warning', text: `${stats.empty_emails} row(s) have no e-mail address and will be skipped.` });
            }
            if (stats?.ragged_rows) {
                notices.push({ kind: 'warning', text: `${stats.ragged_rows} row(s) have a field-count mismatch with the header - verify the preview below.` });
            }
            if (stats?.unresolved_columns?.length) {
                notices.push({ kind: 'warning', text: `These selected attribute columns were not found in the file and are ignored: ${stats.unresolved_columns.join(', ')}.` });
            }
            noticeEl.innerHTML = notices.length
                ? notices.map(n => `<div class="notice notice-${n.kind}" style="margin-bottom:10px">
                    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M10.29 3.86L1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z"/><line x1="12" y1="9" x2="12" y2="13"/><line x1="12" y1="17" x2="12.01" y2="17"/></svg>
                    <div>${App.escapeHtml(n.text)}</div>
                  </div>`).join('')
                : '';
        }

        if (!rows.length) {
            tableEl.innerHTML = '<p style="color:var(--text-muted)">Nothing to preview - no valid rows to convert.</p>';
            return;
        }

        let tableHtml = '<div class="table-wrapper sample-table-wrap"><table class="sample-table"><thead><tr><th>email</th><th>name</th><th>attributes</th></tr></thead><tbody>';
        rows.forEach((r, i) => {
            let attrCell;
            try {
                const obj = JSON.parse(r.attributes || '{}');
                const keys = Object.keys(obj);
                attrCell = keys.length
                    ? `<span class="chip ok" style="margin-right:6px">${keys.length} keys</span><details style="display:inline"><summary style="display:inline;cursor:pointer;color:var(--primary-soft);font-size:0.8rem">view</summary><pre class="attr-json">${App.escapeHtml(JSON.stringify(obj, null, 2))}</pre></details>`
                    : '<span class="chip danger">empty</span>';
            } catch {
                attrCell = `<span title="${App.escapeHtml(r.attributes)}">${App.escapeHtml(r.attributes)}</span>`;
            }
            tableHtml += `<tr><td>${App.escapeHtml(r.email)}</td><td>${App.escapeHtml(r.name)}</td><td>${attrCell}</td></tr>`;
        });
        tableHtml += '</tbody></table></div>';
        tableEl.innerHTML = tableHtml;
    },

    // Human-readable conversion stats for toasts.
    _statsSummary(stats) {
        if (!stats) return '';
        const parts = [`${stats.converted} converted`];
        if (stats.skipped) {
            parts.push(`${stats.skipped} skipped${stats.invalid_emails ? ` (${stats.invalid_emails} invalid e-mail)` : ''}`);
        }
        if (stats.ragged_rows) parts.push(`${stats.ragged_rows} ragged row(s)`);
        if (stats.attribute_count) parts.push(`${stats.attribute_count} attributes/row`);
        return parts.join(' - ');
    },

    // ── Download Converted CSV ───────────────────────────
    async downloadConverted() {
        if (!this.fileContent || !this._mapping) return;

        try {
            const result = await API.upload('/api/converter/convert', this.buildMappingFormData());
            if (result.blob) {
                API.downloadBlob(result.blob, 'listmonk_import.csv');
                if (result.stats) {
                    let stats = null;
                    try { stats = JSON.parse(result.stats); } catch {}
                    if (stats) App.toast(`Converted: ${this._statsSummary(stats)}`, 'success');
                }
            }
        } catch {
            // API.request already surfaced the server message.
        }
    },

    // ── Import Dialog ────────────────────────────────────
    async showImportDialog() {
        const dialog = document.getElementById('importDialog');

        // Fetch lists (with their opt-in mode: double-opt-in lists only mail
        // confirmed subscriptions, so the status choice below matters).
        let lists = [];
        try {
            const res = await API.get('/api/lists?per_page=100&minimal=true');
            lists = res?.data?.results || [];
        } catch {}
        this._lists = lists;

        const stats = this._previewStats || {};
        const attrsEmpty = !!stats.attributes_empty;
        const optinLabel = { double: 'double opt-in', single: 'single opt-in', none: 'no opt-in' };
        let listOptions = lists.map(l =>
            `<label class="checkbox-label"><input type="checkbox" name="importLists" value="${l.id}" onchange="Converter.updateImportButton()"><span>${App.escapeHtml(l.name)}${l.optin ? ` <em style="opacity:.65;font-style:normal;font-size:.75rem">(${App.escapeHtml(optinLabel[l.optin] || l.optin)})</em>` : ''}</span></label>`
        ).join('');

        dialog.style.display = 'block';
        dialog.innerHTML = `
            <div style="border-top:1px solid var(--glass-border);padding-top:16px">
                <h3 style="margin-bottom:12px">Import to ListMonk</h3>
                <div class="import-summary">
                    <div class="cell"><div class="k">Rows</div><div class="v">${stats.converted ?? 0}</div></div>
                    <div class="cell"><div class="k">Skipped</div><div class="v">${stats.skipped ?? 0}</div></div>
                    <div class="cell"><div class="k">Attributes / row</div><div class="v">${stats.attribute_count ?? 0}</div></div>
                    <div class="cell"><div class="k">Email column</div><div class="v">${App.escapeHtml(this._mapping?.emailCol || '-')}</div></div>
                    <div class="cell"><div class="k">Name column</div><div class="v">${App.escapeHtml(this._mapping?.nameCol || '-')}</div></div>
                </div>
                ${attrsEmpty ? `
                <div class="notice notice-danger" style="margin-bottom:14px">
                    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"/><line x1="12" y1="8" x2="12" y2="12"/><line x1="12" y1="16" x2="12.01" y2="16"/></svg>
                    <div>This mapping stores <strong>no attributes</strong> - only e-mail and name will be imported.
                        <label class="checkbox-label" style="margin-top:8px;display:inline-flex"><input type="checkbox" id="ackEmptyAttrs" onchange="Converter.updateImportButton()"><span>I understand the other columns will not be imported</span></label>
                    </div>
                </div>` : ''}
                <div class="form-group">
                    <label>Select Lists *</label>
                    <div class="checkbox-group">${listOptions || '<span style="color:var(--text-muted)">No lists available</span>'}</div>
                </div>
                <div class="form-grid">
                    <div class="form-group">
                        <label>Mode</label>
                        <select id="importMode">
                            <option value="subscribe">Subscribe (add to lists)</option>
                            <option value="blocklist">Blocklist</option>
                        </select>
                    </div>
                    <div class="form-group">
                        <label>Subscription status</label>
                        <select id="importStatus">
                            <option value="confirmed" selected>Confirmed (mailable)</option>
                            <option value="unconfirmed">Unconfirmed (needs opt-in)</option>
                            <option value="unsubscribed">Unsubscribed</option>
                        </select>
                        <p style="font-size:0.78rem;color:var(--text-muted);margin-top:6px">
                            Double opt-in lists only send to <strong>confirmed</strong> subscriptions. Use "Confirmed" for lists whose contacts already opted in.
                        </p>
                    </div>
                </div>
                <div class="form-group">
                    <label class="checkbox-label" style="display:inline-flex;margin-right:8px"><input type="checkbox" id="owUserInfo" checked><span>Overwrite name &amp; attributes of existing subscribers</span></label>
                    <label class="checkbox-label" style="display:inline-flex"><input type="checkbox" id="owSubStatus" checked><span>Overwrite subscription status</span></label>
                </div>
                <button class="btn btn-success" id="startImportBtn" onclick="Converter.executeImport()" disabled>Select a list to continue</button>
            </div>
        `;
        this.updateImportButton();
    },

    // Keep the import button honest about why it is (not) clickable.
    updateImportButton() {
        const btn = document.getElementById('startImportBtn');
        if (!btn) return;
        const selected = document.querySelectorAll('input[name="importLists"]:checked').length;
        const ack = document.getElementById('ackEmptyAttrs');
        const needsAck = !!ack && !ack.checked;
        if (!selected) {
            btn.disabled = true;
            btn.textContent = 'Select a list to continue';
        } else if (needsAck) {
            btn.disabled = true;
            btn.textContent = 'Confirm the empty-attributes warning to continue';
        } else {
            btn.disabled = false;
            btn.textContent = 'Start Import';
        }
    },

    async executeImport() {
        if (this._importing) return;

        if (!this.fileContent || !this._mapping) {
            App.toast('Upload a file and map its columns first', 'error');
            return;
        }

        const selectedLists = Array.from(document.querySelectorAll('input[name="importLists"]:checked'))
            .map(cb => parseInt(cb.value, 10))
            .filter(id => Number.isFinite(id));

        if (!selectedLists.length) { App.toast('Select at least one list', 'error'); return; }

        const modeEl = document.getElementById('importMode');
        const mode = modeEl ? modeEl.value : 'subscribe';
        const statusEl = document.getElementById('importStatus');
        const subStatus = statusEl ? statusEl.value : 'confirmed';
        const owUser = document.getElementById('owUserInfo')?.checked ?? true;
        const owStatus = document.getElementById('owSubStatus')?.checked ?? true;

        const formData = this.buildMappingFormData();
        formData.append('list_ids', JSON.stringify(selectedLists));
        formData.append('mode', mode);
        formData.append('subscription_status', subStatus);
        formData.append('overwrite_userinfo', String(owUser));
        formData.append('overwrite_subscription_status', String(owStatus));

        this._importing = true;
        try {
            const result = await API.upload('/api/converter/convert-and-import', formData);
            const stats = result?.conversion_stats || {};
            App.toast(`Import started! ${this._statsSummary(stats)}.`, 'success');

            // Show import status
            this.pollImportStatus();
        } catch {
            // API.request already surfaced the server message.
            this._importing = false;
        }
    },

    async pollImportStatus() {
        const dialog = document.getElementById('importDialog');
        if (!dialog) return;
        dialog.innerHTML = `
            <div style="border-top:1px solid var(--glass-border);padding-top:16px" role="status" aria-live="polite">
                <h3>Import Progress</h3>
                <div class="progress-bar"><div class="progress-fill" id="importProgress" style="width:0%"></div></div>
                <p id="importStatusText" style="font-size:0.85rem;color:var(--text-secondary);margin-top:8px">Starting import…</p>
                <pre id="importLogs" style="max-height:200px;overflow-y:auto;font-size:0.8rem;color:var(--text-muted);margin-top:12px;background:var(--bg-input);padding:12px;border-radius:var(--radius-md);border:1px solid var(--glass-border)"></pre>
            </div>
        `;

        const poll = async () => {
            // The user may have navigated away from the converter page.
            if (!document.getElementById('importStatusText')) {
                this._importing = false;
                return;
            }
            try {
                // Silent mode: a flapping status endpoint must not stack an
                // error toast every 2 seconds — failures are counted and
                // reported in the dialog instead.
                const [statusRes, logsRes] = await Promise.allSettled([
                    API.get('/api/subscribers/import/status', true),
                    API.get('/api/subscribers/import/logs', true),
                ]);

                // The awaits above can outlive a navigation; re-check before
                // touching the dialog elements.
                const statusEl = document.getElementById('importStatusText');
                if (!statusEl) {
                    this._importing = false;
                    return;
                }

                if (statusRes.status === 'fulfilled') {
                    failures = 0;
                    const status = statusRes.value?.data || {};
                    const total = status.total || 1;
                    const imported = status.imported || 0;
                    const pct = Math.round((imported / total) * 100);

                    const progressEl = document.getElementById('importProgress');
                    if (progressEl) progressEl.style.width = `${pct}%`;
                    statusEl.textContent =
                        `${imported} / ${total} imported (${pct}%) - Status: ${status.status || 'unknown'}`;

                    if (status.status === 'finished' || status.status === 'stopped') {
                        this._importing = false;
                        App.toast(`Import complete! ${imported} subscribers imported.`, 'success');
                        return;
                    }
                } else {
                    failures++;
                    if (failures >= 5) {
                        this._importing = false;
                        statusEl.textContent =
                            'Lost contact with the import status endpoint. The import may still be running — reload to check.';
                        App.toast('Stopped polling import status after repeated failures', 'error');
                        return;
                    }
                    statusEl.textContent = `Status check failed (${failures}/5), retrying…`;
                }

                if (logsRes.status === 'fulfilled') {
                    const logs = logsRes.value?.data || '';
                    const logsEl = document.getElementById('importLogs');
                    if (logsEl) logsEl.textContent = logs;
                }

                setTimeout(poll, 2000);
            } catch (err) {
                this._importing = false;
                const el = document.getElementById('importStatusText');
                if (el) el.textContent = 'Failed to check import status.';
                if (!err?.handled) App.toast('Failed to check import status', 'error');
            }
        };

        let failures = 0;
        setTimeout(poll, 1000);
    },
};
