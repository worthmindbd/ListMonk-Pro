/**
 * CSV Converter page - Upload, map columns, convert, import.
 */
const Converter = {
    fileContent: null,
    fileName: '',
    columns: [],
    sampleRows: [],
    step: 1,

    render() {
        this.step = 1;
        this.fileContent = null;
        this.fileName = '';
        this.columns = [];
        this.sampleRows = [];
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
                    <input type="file" id="csvFile" accept=".csv,.tsv,.txt" style="display:none">
                </div>
                <p style="margin-top:12px;font-size:0.85rem;color:var(--text-muted)">
                    Supports CSV, TSV, and text files. The converter will automatically detect the delimiter,
                    encoding, and columns. ListMonk requires: <strong>email</strong>, <strong>name</strong>, and
                    <strong>attributes</strong> (JSON) columns.
                </p>
            </div>
        `;
    },

    // ── Step 2: Column Mapping ───────────────────────────
    renderMapping() {
        let colOptions = this.columns.map(c => `<option value="${App.escapeHtml(c)}">${App.escapeHtml(c)}</option>`).join('');

        let attrCheckboxes = this.columns.map(c =>
            `<label class="checkbox-label"><input type="checkbox" name="attrCols" value="${App.escapeHtml(c)}"><span>${App.escapeHtml(c)}</span></label>`
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

        return `
            <div class="card">
                <div class="card-header">
                    <h3 class="card-title">Map Columns</h3>
                    <span style="color:var(--text-muted);font-size:0.85rem">${this.fileName} - ${this.columns.length} columns detected</span>
                </div>
                <div class="form-grid">
                    <div class="form-group">
                        <label>Email Column * (required)</label>
                        <select id="emailCol">${colOptions}</select>
                    </div>
                    <div class="form-group">
                        <label>Name Column (optional)</label>
                        <select id="nameCol"><option value="">-- None --</option>${colOptions}</select>
                    </div>
                </div>
                <div class="form-group">
                    <label>Attribute Columns (will be stored as JSON in ListMonk)</label>
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

    // ── Step 3: Preview & Actions ────────────────────────
    renderPreview() {
        return `
            <div class="card" style="margin-bottom:20px">
                <div class="card-header">
                    <h3 class="card-title">Conversion Preview</h3>
                </div>
                <div id="previewStats" style="margin-bottom:16px"></div>
                <div id="previewTable"></div>
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

        const formData = new FormData();
        formData.append('file', file);

        // Store file for later use
        this.fileContent = file;

        try {
            const result = await API.upload('/api/converter/detect-columns', formData);
            this.columns = result.columns || [];
            this.sampleRows = result.sample_rows || [];

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
        const emailCol = document.getElementById('emailCol').value;
        const nameCol = document.getElementById('nameCol').value;
        const attrCols = Array.from(document.querySelectorAll('input[name="attrCols"]:checked'))
            .map(cb => cb.value);

        if (!emailCol) { App.toast('Email column is required', 'error'); return; }

        // Store mapping for later
        this._mapping = { emailCol, nameCol, attrCols };

        this.step = 3;
        this.renderStep();

        // Show preview of converted data
        const previewRows = this.sampleRows.map(row => {
            const attribs = {};
            attrCols.forEach(col => {
                if (row[col]) attribs[col] = row[col];
            });
            return {
                email: row[emailCol] || '',
                name: nameCol ? (row[nameCol] || '') : '',
                attributes: JSON.stringify(attribs),
            };
        });

        let tableHtml = '<div class="table-wrapper"><table><thead><tr><th>email</th><th>name</th><th>attributes</th></tr></thead><tbody>';
        previewRows.forEach(r => {
            tableHtml += `<tr><td>${App.escapeHtml(r.email)}</td><td>${App.escapeHtml(r.name)}</td><td style="max-width:300px;overflow:hidden;text-overflow:ellipsis;font-size:0.8rem">${App.escapeHtml(r.attributes)}</td></tr>`;
        });
        tableHtml += '</tbody></table></div>';

        document.getElementById('previewTable').innerHTML = tableHtml;
        document.getElementById('previewStats').innerHTML = `
            <p style="color:var(--text-secondary)">
                Email: <strong>${App.escapeHtml(emailCol)}</strong> |
                Name: <strong>${App.escapeHtml(nameCol || '(none)')}</strong> |
                Attributes: <strong>${App.escapeHtml(attrCols.join(', ') || '(none)')}</strong>
            </p>
        `;
    },

    // ── Download Converted CSV ───────────────────────────
    async downloadConverted() {
        if (!this.fileContent || !this._mapping) return;

        const formData = new FormData();
        formData.append('file', this.fileContent);
        formData.append('email_column', this._mapping.emailCol);
        if (this._mapping.nameCol) formData.append('name_column', this._mapping.nameCol);
        formData.append('attribute_columns', JSON.stringify(this._mapping.attrCols));

        try {
            const result = await API.upload('/api/converter/convert', formData);
            if (result.blob) {
                API.downloadBlob(result.blob, 'listmonk_import.csv');
                if (result.stats) {
                    const stats = JSON.parse(result.stats);
                    App.toast(`Converted ${stats.converted} rows (${stats.skipped} skipped)`, 'success');
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
        const selectedLists = Array.from(document.querySelectorAll('input[name="importLists"]:checked'))
            .map(cb => parseInt(cb.value));

        if (!selectedLists.length) { App.toast('Select at least one list', 'error'); return; }

        const mode = document.getElementById('importMode').value;

        const formData = new FormData();
        formData.append('file', this.fileContent);
        formData.append('email_column', this._mapping.emailCol);
        if (this._mapping.nameCol) formData.append('name_column', this._mapping.nameCol);
        formData.append('attribute_columns', JSON.stringify(this._mapping.attrCols));
        formData.append('list_ids', JSON.stringify(selectedLists));
        formData.append('mode', mode);

        try {
            const result = await API.upload('/api/converter/convert-and-import', formData);
            const stats = result?.conversion_stats || {};
            App.toast(`Import started! ${stats.converted} subscribers converted.`, 'success');

            // Show import status
            this.pollImportStatus();
        } catch {
            // API.request already surfaced the server message.
        }
    },

    async pollImportStatus() {
        const dialog = document.getElementById('importDialog');
        dialog.innerHTML = `
            <div style="border-top:1px solid var(--glass-border);padding-top:16px">
                <h4>Import Progress</h4>
                <div class="progress-bar"><div class="progress-fill" id="importProgress" style="width:0%"></div></div>
                <p id="importStatusText" style="font-size:0.85rem;color:var(--text-secondary);margin-top:8px">Starting import...</p>
                <pre id="importLogs" style="max-height:200px;overflow-y:auto;font-size:0.8rem;color:var(--text-muted);margin-top:12px;background:var(--bg-input);padding:12px;border-radius:var(--radius-md);border:1px solid var(--glass-border)"></pre>
            </div>
        `;

        const poll = async () => {
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

                    document.getElementById('importProgress').style.width = `${pct}%`;
                    document.getElementById('importStatusText').textContent =
                        `${imported} / ${total} imported (${pct}%) - Status: ${status.status || 'unknown'}`;

                    if (status.status === 'finished' || status.status === 'stopped') {
                        App.toast(`Import complete! ${imported} subscribers imported.`, 'success');
                        return;
                    }
                }

                if (logsRes.status === 'fulfilled') {
                    const logs = logsRes.value?.data || '';
                    document.getElementById('importLogs').textContent = logs;
                }

                setTimeout(poll, 2000);
            } catch {
                App.toast('Failed to check import status', 'error');
            }
        };

        setTimeout(poll, 1000);
    },
};
