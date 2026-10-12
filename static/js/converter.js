/**
 * CSV Converter page - Upload, map columns, convert, import.
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
        this.renderStep();
    },

    renderStep() {
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
                <div class="card-header"><h3 class="card-title">Upload CSV File</h3></div>
                <div class="file-drop-zone" id="dropZone">
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

        // Sample data table
        let sampleHtml = '';
        if (this.sampleRows.length) {
            sampleHtml = '<div class="table-wrapper" style="margin-top:16px"><table><thead><tr>';
            this.columns.forEach(c => { sampleHtml += `<th>${App.escapeHtml(c)}</th>`; });
            sampleHtml += '</tr></thead><tbody>';
            this.sampleRows.forEach(row => {
                sampleHtml += '<tr>';
                this.columns.forEach(c => { sampleHtml += `<td>${App.escapeHtml(row[c] || '')}</td>`; });
                sampleHtml += '</tr>';
            });
            sampleHtml += '</tbody></table></div>';
        }

        // In auto mode ListMonk receives one attribute per unmapped column, so
        // say exactly which columns that will be before the preview.
        const auto = this._autoAttributes;
        const skipped = new Set([emailSel, nameSel].filter(Boolean));
        const autoCols = auto ? this.columns.filter(c => !skipped.has(c)) : [];
        const autoHint = auto
            ? (autoCols.length
                ? `All other columns will be stored as JSON attributes: <strong>${autoCols.map(c => App.escapeHtml(c)).join(', ')}</strong>`
                : 'No extra columns found - every column is already mapped.')
            : 'Only the columns checked below will be stored as JSON attributes; unchecked columns are dropped.';

        return `
            <div class="card">
                <div class="card-header">
                    <h3 class="card-title">Map Columns</h3>
                    <span style="color:var(--text-muted);font-size:0.85rem">${this.fileName} - ${this.columns.length} columns detected</span>
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
                <div class="form-group">
                    <label class="checkbox-label ${auto ? 'toggle-on' : ''}" style="font-size:0.95rem;padding:10px 14px">
                        <input type="checkbox" id="autoAttrs" ${auto ? 'checked' : ''} onchange="Converter.showAutoAttrs(this.checked)">
                        <span style="font-weight:600">Convert all other columns to attributes (recommended)</span>
                    </label>
                    <p style="font-size:0.85rem;color:var(--text-muted);margin-top:8px">${autoHint}</p>
                </div>
                <div class="form-group" id="attrPicker" style="display:${auto ? 'none' : 'block'}">
                    <label>Attribute Columns (stored as JSON in ListMonk)</label>
                    <div class="checkbox-group">${attrCheckboxes}</div>
                </div>
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
            this._mapping = { emailCol: '', nameCol: '', attrCols: [], _initialized: true };
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
            this._mapping = { emailCol: '', nameCol: '', attrCols: [], _initialized: true };
        }
        // When the picker is visible the user may have just changed it; snapshot
        // the checked columns before the mode switch hides them again.
        const picker = document.getElementById('attrPicker');
        if (picker && picker.style.display !== 'none') {
            this._mapping.attrCols = Array.from(document.querySelectorAll('input[name="attrCols"]:checked')).map(cb => cb.value);
        }
        this._autoAttributes = checked;
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
                    <h3 class="card-title">Conversion Preview</h3>
                </div>
                <p id="previewStats" style="color:var(--text-secondary);font-size:0.9rem;margin-bottom:12px"></p>
                <div id="previewNotice"></div>
                <div id="previewTable"><p style="color:var(--text-muted)">Converting a sample...</p></div>
            </div>
            <div class="card">
                <div class="card-header">
                    <h3 class="card-title">What would you like to do?</h3>
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
        this._attrCols = [];
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

        // Store mapping for later
        this._mapping = { emailCol, nameCol, attrCols, auto, _initialized: true };
        this._attrCols = attrCols;
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
            // the preview area instead of a stuck "Converting a sample...".
            this.renderPreviewError();
        }
    },

    renderPreviewError() {
        const statEl = document.getElementById('previewStats');
        const tableEl = document.getElementById('previewTable');
        if (statEl) statEl.textContent = '';
        if (tableEl) {
            tableEl.innerHTML = '<p style="color:var(--text-muted)">Preview failed - adjust the mapping and try again.</p>';
        }
    },

    // The attribute mapping shared by preview, download and import.
    buildMappingFormData() {
        const formData = new FormData();
        formData.append('file', this.fileContent);
        formData.append('email_column', this._mapping.emailCol);
        if (this._mapping.nameCol) formData.append('name_column', this._mapping.nameCol);
        formData.append('attribute_columns', JSON.stringify(this._mapping.attrCols));
        formData.append('attribute_mode', this._mapping.auto ? 'auto' : 'selected');
        return formData;
    },

    // Renders the converted rows returned by /api/converter/preview.
    renderPreviewResult(rows, stats) {
        const statEl = document.getElementById('previewStats');
        const noticeEl = document.getElementById('previewNotice');
        const tableEl = document.getElementById('previewTable');
        if (!tableEl) return;

        if (statEl) {
            const skipped = stats?.skipped || 0;
            statEl.textContent = stats
                ? `${stats.converted} of ${stats.total} rows converted${skipped ? ` - ${skipped} skipped` : ''}`
                : '';
        }

        if (noticeEl) {
            const notices = [];
            if (stats?.invalid_emails) {
                notices.push(`${stats.invalid_emails} row(s) have a malformed e-mail address and will be skipped (ListMonk would reject them too).`);
            }
            if (stats?.empty_emails) {
                notices.push(`${stats.empty_emails} row(s) have no e-mail address and will be skipped.`);
            }
            if (stats?.ragged_rows) {
                notices.push(`${stats.ragged_rows} row(s) have a field-count mismatch with the header - verify the preview below.`);
            }
            noticeEl.innerHTML = notices.length
                ? `<div class="notice notice-warning" style="margin-bottom:16px"><div>${notices.map(n => App.escapeHtml(n)).join('<br>')}</div></div>`
                : '';
        }

        if (!rows.length) {
            tableEl.innerHTML = '<p style="color:var(--text-muted)">Nothing to preview - no valid rows to convert.</p>';
            return;
        }

        let tableHtml = '<div class="table-wrapper"><table><thead><tr><th>email</th><th>name</th><th>attributes</th></tr></thead><tbody>';
        rows.forEach(r => {
            tableHtml += `<tr><td>${App.escapeHtml(r.email)}</td><td>${App.escapeHtml(r.name)}</td><td style="max-width:300px;overflow:hidden;text-overflow:ellipsis;font-size:0.8rem">${App.escapeHtml(r.attributes)}</td></tr>`;
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

        // Fetch lists
        let lists = [];
        try {
            const res = await API.get('/api/lists?per_page=100&minimal=true');
            lists = res?.data?.results || [];
        } catch {}

        let listOptions = lists.map(l =>
            `<label class="checkbox-label"><input type="checkbox" name="importLists" value="${l.id}"><span>${App.escapeHtml(l.name)}</span></label>`
        ).join('');

        dialog.style.display = 'block';
        dialog.innerHTML = `
            <div style="border-top:1px solid var(--glass-border);padding-top:16px">
                <h4 style="margin-bottom:12px">Import to ListMonk</h4>
                <div class="form-group">
                    <label>Select Lists *</label>
                    <div class="checkbox-group">${listOptions || '<span style="color:var(--text-muted)">No lists available</span>'}</div>
                </div>
                <div class="form-group">
                    <label>Mode</label>
                    <select id="importMode">
                        <option value="subscribe">Subscribe (add to lists)</option>
                        <option value="blocklist">Blocklist</option>
                    </select>
                </div>
                <button class="btn btn-success" onclick="Converter.executeImport()">Start Import</button>
            </div>
        `;
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

        const formData = this.buildMappingFormData();
        formData.append('list_ids', JSON.stringify(selectedLists));
        formData.append('mode', mode);

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
            <div style="border-top:1px solid var(--glass-border);padding-top:16px">
                <h4>Import Progress</h4>
                <div class="progress-bar"><div class="progress-fill" id="importProgress" style="width:0%"></div></div>
                <p id="importStatusText" style="font-size:0.85rem;color:var(--text-secondary);margin-top:8px">Starting import...</p>
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
                const [statusRes, logsRes] = await Promise.allSettled([
                    API.get('/api/subscribers/import/status'),
                    API.get('/api/subscribers/import/logs'),
                ]);

                if (statusRes.status === 'fulfilled') {
                    const status = statusRes.value?.data || {};
                    const total = status.total || 1;
                    const imported = status.imported || 0;
                    const pct = Math.round((imported / total) * 100);

                    const progressEl = document.getElementById('importProgress');
                    if (progressEl) progressEl.style.width = `${pct}%`;
                    document.getElementById('importStatusText').textContent =
                        `${imported} / ${total} imported (${pct}%) - Status: ${status.status || 'unknown'}`;

                    if (status.status === 'finished' || status.status === 'stopped') {
                        this._importing = false;
                        App.toast(`Import complete! ${imported} subscribers imported.`, 'success');
                        return;
                    }
                }

                if (logsRes.status === 'fulfilled') {
                    const logs = logsRes.value?.data || '';
                    const logsEl = document.getElementById('importLogs');
                    if (logsEl) logsEl.textContent = logs;
                }

                setTimeout(poll, 2000);
            } catch {
                this._importing = false;
                App.toast('Failed to check import status', 'error');
            }
        };

        setTimeout(poll, 1000);
    },
};
