/**
 * Main SPA router and shared UI components.
 */
const App = {
    currentPage: null,
    currentMode: 'newsletter',

    pages: {
        'dashboard': { title: 'Dashboard', render: () => Dashboard.render() },
        'subscribers': { title: 'Subscribers', render: () => Subscribers.render() },
        'lists': { title: 'Lists', render: () => Lists.render() },
        'campaigns': { title: 'Campaigns', render: () => Campaigns.render() },
        'analytics': { title: 'Analytics', render: () => Analytics.render() },
        'settings': { title: 'Settings', render: () => Settings.render() },
        'templates': { title: 'Templates', render: () => Templates.render() },
        'bounces': { title: 'Bounces', render: () => Bounces.render() },
        'converter': { title: 'CSV Converter', render: () => Converter.render() },
        'unsubscribes': { title: 'Unsubscribes', render: () => Unsubscribes.render() },
        'replies': { title: 'Replies & Leads', render: () => Replies.render() },
        'deliverability': { title: 'Deliverability & Safety', render: () => Deliverability.render() },
    },

    init() {
        window.addEventListener('hashchange', () => this.route());
        document.getElementById('menuToggle').addEventListener('click', () => {
            document.getElementById('sidebar').classList.toggle('open');
        });

        // Close sidebar on content click (mobile)
        document.querySelector('.content-area').addEventListener('click', () => {
            document.getElementById('sidebar').classList.remove('open');
        });

        // Modal handlers
        document.getElementById('modalClose').addEventListener('click', () => this.closeModal());
        document.getElementById('modalCancel').addEventListener('click', () => this.closeModal());
        document.getElementById('modalOverlay').addEventListener('click', (e) => {
            if (e.target === e.currentTarget) this.closeModal();
        });

        // Initialize mode
        const savedMode = localStorage.getItem('listmonk_pro_mode');
        if (!savedMode) {
            this.setMode('newsletter', false);
            setTimeout(() => this.showModeOnboarding(), 200);
        } else {
            this.setMode(savedMode, false);
        }

        this.checkConnection();
        this.route();
    },

    showModeOnboarding() {
        const modal = document.getElementById('modeOnboardingModal');
        if (modal) modal.style.display = 'flex';
    },

    selectInitialMode(mode) {
        const modal = document.getElementById('modeOnboardingModal');
        if (modal) modal.style.display = 'none';
        this.setMode(mode, true);
        this.toast(`Welcome to ListMonk Pro! Activated ${mode === 'cold_email' ? 'Cold Email' : 'Newsletter'} mode.`, 'success');
    },

    setMode(mode, save = true) {
        this.currentMode = mode;
        if (save) localStorage.setItem('listmonk_pro_mode', mode);
        document.documentElement.setAttribute('data-mode', mode);

        // Update mode switcher buttons
        document.querySelectorAll('.mode-pill-btn').forEach(btn => {
            btn.classList.toggle('active', btn.dataset.mode === mode);
        });

        const isCold = mode === 'cold_email';

        // Dynamic sidebar labels
        const subsLabel = document.querySelector('.nav-link[data-page="subscribers"] span');
        if (subsLabel) subsLabel.textContent = isCold ? 'Leads' : 'Subscribers';

        const listsLabel = document.querySelector('.nav-link[data-page="lists"] span');
        if (listsLabel) listsLabel.textContent = isCold ? 'Lead Lists' : 'Lists';

        const campLabel = document.querySelector('.nav-link[data-page="campaigns"] span');
        if (campLabel) campLabel.textContent = isCold ? 'Cold Outreach' : 'Campaigns';

        // Dynamic visibility for mode-specific nav items
        document.querySelectorAll('.nav-item-cold').forEach(el => {
            el.style.display = isCold ? '' : 'none';
        });
        document.querySelectorAll('.nav-item-newsletter').forEach(el => {
            el.style.display = isCold ? 'none' : '';
        });

        // A category heading only makes sense while it still has visible items
        // (Newsletter mode has no Replies/Deliverability page, so that group
        // collapses to a single Bounces entry and keeps its heading).
        document.querySelectorAll('.nav-section-title').forEach(title => {
            const group = title.dataset.navGroup;
            if (!group) return;
            const siblings = document.querySelectorAll(
                `.nav-menu li[data-nav-group="${group}"]:not(.nav-section-title)`
            );
            const anyVisible = Array.from(siblings).some(li => li.style.display !== 'none');
            title.style.display = anyVisible ? '' : 'none';
        });

        // Re-render current page
        if (this.currentPage) {
            this.route();
        }
        window.dispatchEvent(new CustomEvent('modeChanged', { detail: { mode } }));
    },

    route() {
        const hash = window.location.hash.slice(2) || 'dashboard';
        // Own-property check: a plain lookup also resolves inherited keys, so
        // "#/constructor" or "#/toString" would return a function and blow up
        // with a confusing TypeError instead of redirecting to the dashboard.
        const page = Object.hasOwn(this.pages, hash) ? this.pages[hash] : null;

        if (!page || typeof page.render !== 'function') {
            window.location.hash = '#/dashboard';
            return;
        }

        this.currentPage = hash;

        // Clear per-page state when navigating away
        if (hash !== 'bounces' && typeof Bounces !== 'undefined') {
            Bounces.campaigns = [];
            Bounces.campaignFilter = 0;
            Bounces.page = 1;
        }

        // Update nav
        document.querySelectorAll('.nav-link').forEach(link => {
            link.classList.toggle('active', link.dataset.page === hash);
        });

        // Update title
        document.getElementById('pageTitle').textContent = page.title;

        // Render page
        const content = document.getElementById('contentArea');
        content.innerHTML = '<div class="loading-spinner">Loading...</div>';
        try {
            const res = page.render();
            if (res && typeof res.catch === 'function') {
                res.catch(err => {
                    console.error(`Error rendering page ${hash}:`, err);
                    content.innerHTML = `<div class="empty-state"><h3>Failed to load ${App.escapeHtml(page.title)}</h3><p>${App.escapeHtml(err?.message || '')}</p></div>`;
                });
            }
        } catch (err) {
            console.error(`Error rendering page ${hash}:`, err);
            content.innerHTML = `<div class="empty-state"><h3>Failed to load ${App.escapeHtml(page.title)}</h3><p>${App.escapeHtml(err?.message || '')}</p></div>`;
        }
    },

    async checkConnection() {
        const dot = document.querySelector('.status-dot');
        const text = document.querySelector('.status-text');
        try {
            await API.get('/api/lists?per_page=1&minimal=true');
            dot.className = 'status-dot connected';
            text.textContent = 'Connected';
        } catch {
            dot.className = 'status-dot error';
            text.textContent = 'Disconnected';
        }
    },

    // ── Toast ────────────────────────────────────────────
    toast(message, type = 'info') {
        const container = document.getElementById('toastContainer');
        const toast = document.createElement('div');
        toast.className = `toast toast-${type}`;
        toast.textContent = message;
        container.appendChild(toast);
        setTimeout(() => {
            toast.style.opacity = '0';
            toast.style.transform = 'translateX(100%)';
            setTimeout(() => toast.remove(), 300);
        }, 4000);
    },

    // ── Modal / Confirm ──────────────────────────────────
    _modalResolve: null,

    escapeHtml(s) {
        if (s == null) return '';
        return String(s)
            .replace(/&/g, '&amp;')
            .replace(/</g, '&lt;')
            .replace(/>/g, '&gt;')
            .replace(/"/g, '&quot;')
            .replace(/'/g, '&#39;');
    },

    _resetModalButtons() {
        const cancelBtn = document.getElementById('modalCancel');
        const confirmBtn = document.getElementById('modalConfirm');
        if (cancelBtn) cancelBtn.style.display = '';
        if (confirmBtn) {
            confirmBtn.style.display = '';
            confirmBtn.textContent = 'Confirm';
            confirmBtn.className = 'btn btn-danger';
            confirmBtn.onclick = null;
        }
    },

    confirm(title, message) {
        return new Promise((resolve) => {
            this._modalResolve = resolve;
            document.getElementById('modalTitle').textContent = title;
            document.getElementById('modalBody').innerHTML = `<p>${this.escapeHtml(message)}</p>`;
            this._resetModalButtons();
            document.getElementById('modalOverlay').style.display = 'flex';
            document.getElementById('modalConfirm').onclick = () => {
                this._modalResolve = null;
                this.closeModal();
                resolve(true);
            };
        });
    },

    closeModal() {
        document.getElementById('modalOverlay').style.display = 'none';
        this._resetModalButtons();
        if (this._modalResolve) {
            this._modalResolve(false);
            this._modalResolve = null;
        }
    },

    showProgress(title, message) {
        this._modalResolve = null;
        document.getElementById('modalTitle').textContent = title;
        document.getElementById('modalBody').innerHTML = `
            <p>${this.escapeHtml(message)}</p>
            <div id="progressPhase" style="margin-top:14px;font-weight:600;color:var(--text-primary)">Starting...</div>
            <div style="margin-top:10px;background:var(--bg-input);border-radius:9999px;height:12px;overflow:hidden;border:1px solid var(--glass-border);box-shadow:inset 0 1px 3px rgba(0,0,0,0.2)">
                <div id="progressBar" style="height:100%;width:0%;background:linear-gradient(90deg,var(--primary),var(--emerald));border-radius:9999px;box-shadow:0 0 12px var(--primary-glow);transition:width 0.3s cubic-bezier(0.16,1,0.3,1)"></div>
            </div>
            <div id="progressStats" style="margin-top:10px;font-size:0.88rem;color:var(--text-secondary);text-align:center;font-feature-settings:'tnum'">0 / 0</div>
            <div id="progressMessage" style="margin-top:8px;font-size:0.82rem;color:var(--text-muted);font-family:monospace;word-break:break-word;text-align:center"></div>
        `;
        document.getElementById('modalCancel').style.display = 'none';
        document.getElementById('modalConfirm').style.display = 'none';
        document.getElementById('modalOverlay').style.display = 'flex';
    },

    updateProgress(progress) {
        const phaseEl = document.getElementById('progressPhase');
        const barEl = document.getElementById('progressBar');
        const statsEl = document.getElementById('progressStats');
        const msgEl = document.getElementById('progressMessage');
        if (!phaseEl || !barEl) return;
        const phaseLabels = {
            starting: 'Starting...',
            fetching: 'Fetching hard bounces',
            identifying: 'Identifying blacklist bounces',
            reclassifying: 'Reclassifying hard → soft',
            unblocking: 'Unblocking subscribers',
            done: 'Complete',
            error: 'Error',
        };
        phaseEl.textContent = phaseLabels[progress.phase] || progress.phase || '';
        const pct = progress.total > 0
            ? Math.min(100, Math.round((progress.current / progress.total) * 100))
            : (progress.status === 'done' ? 100 : 0);
        barEl.style.width = pct + '%';
        statsEl.textContent = progress.total > 0
            ? `${progress.current} / ${progress.total} (${pct}%)`
            : `${progress.current}`;
        if (msgEl) msgEl.textContent = progress.message || '';
    },

    showResult(title, bodyHtml) {
        this._modalResolve = null;
        document.getElementById('modalTitle').textContent = title;
        document.getElementById('modalBody').innerHTML = bodyHtml;
        document.getElementById('modalCancel').style.display = 'none';
        const confirmBtn = document.getElementById('modalConfirm');
        confirmBtn.style.display = '';
        confirmBtn.textContent = 'OK';
        confirmBtn.className = 'btn btn-primary';
        confirmBtn.onclick = () => this.closeModal();
        document.getElementById('modalOverlay').style.display = 'flex';
    },

    // ── Pagination ───────────────────────────────────────
    renderPagination(currentPage, total, perPage, onPageChange) {
        const totalPages = Math.ceil(total / perPage);
        if (totalPages <= 1) return '';

        // Build the list of page numbers to show (windowed with ellipsis)
        const pages = [];
        if (totalPages <= 7) {
            for (let i = 1; i <= totalPages; i++) pages.push(i);
        } else {
            pages.push(1);
            if (currentPage > 4) pages.push('...');
            const start = Math.max(2, currentPage - 1);
            const end = Math.min(totalPages - 1, currentPage + 1);
            for (let i = start; i <= end; i++) pages.push(i);
            if (currentPage < totalPages - 3) pages.push('...');
            pages.push(totalPages);
        }

        let html = '<div class="pagination">';
        html += `<button class="btn btn-sm" ${currentPage <= 1 ? 'disabled' : ''} onclick="${onPageChange}(${currentPage - 1})">&#8592;</button>`;
        for (const p of pages) {
            if (p === '...') {
                html += '<span class="pagination-ellipsis">&hellip;</span>';
            } else {
                const active = p === currentPage ? ' active' : '';
                html += `<button class="btn btn-sm${active}" onclick="${onPageChange}(${p})">${p}</button>`;
            }
        }
        html += `<button class="btn btn-sm" ${currentPage >= totalPages ? 'disabled' : ''} onclick="${onPageChange}(${currentPage + 1})">&#8594;</button>`;
        html += `<span class="pagination-info">${App.formatNumber(total)} records</span>`;
        html += '</div>';
        return html;
    },

    // ── Status Badge ─────────────────────────────────────
    statusBadge(status) {
        const map = {
            'enabled': 'success', 'active': 'success', 'running': 'success', 'finished': 'success',
            'blocklisted': 'danger', 'cancelled': 'danger', 'archived': 'default',
            'draft': 'info', 'scheduled': 'warning', 'paused': 'warning',
        };
        const cls = map[status] || 'default';
        return `<span class="badge badge-${cls}">${this.escapeHtml(status)}</span>`;
    },

    // ── Format Date ──────────────────────────────────────
    formatDate(dateStr) {
        if (!dateStr) return '-';
        const d = new Date(dateStr);
        return d.toLocaleDateString('en-US', { year: 'numeric', month: 'short', day: 'numeric' });
    },

    formatDateTime(dateStr) {
        if (!dateStr) return '-';
        const d = new Date(dateStr);
        return d.toLocaleString('en-US', {
            year: 'numeric', month: 'short', day: 'numeric',
            hour: '2-digit', minute: '2-digit'
        });
    },

    // ── Number Format ────────────────────────────────────
    formatNumber(n) {
        if (n === undefined || n === null) return '0';
        return n.toLocaleString();
    },

    // ── Set Content ──────────────────────────────────────
    setContent(html) {
        document.getElementById('contentArea').innerHTML = html;
    },
};

// ── Templates Page (simple) ──────────────────────────────
const Templates = {
    _data: [],

    async render() {
        try {
            const result = await API.get('/api/templates');
            this._data = result?.data || [];
            const templates = this._data;

            let html = `
                <div class="search-bar">
                    <span style="color:var(--text-secondary);font-size:0.9rem">${templates.length} template(s)</span>
                    <div class="page-actions">
                        <button class="btn btn-primary" onclick="Templates.showCreate()">+ New Template</button>
                    </div>
                </div>
                <div class="table-wrapper"><table>
                    <thead><tr>
                        <th>ID</th><th>Name</th><th>Type</th><th>Default</th><th>Created</th><th>Updated</th><th class="cell-actions">Actions</th>
                    </tr></thead><tbody>`;

            if (!templates.length) {
                html += '<tr><td colspan="7"><div class="empty-state"><h3>No templates found</h3></div></td></tr>';
            }
            templates.forEach(t => {
                const name = App.escapeHtml(t.name || '');
                html += `<tr>
                    <td>${t.id}</td>
                    <td><strong>${name}</strong></td>
                    <td><span class="badge badge-info">${t.type || '-'}</span></td>
                    <td>${t.is_default ? '<span class="badge badge-success">Default</span>' : '-'}</td>
                    <td>${App.formatDate(t.created_at)}</td>
                    <td>${App.formatDate(t.updated_at)}</td>
                    <td class="cell-actions">
                        <div class="action-btns">
                            <button class="btn btn-sm" onclick="Templates.preview(${t.id})">Preview</button>
                            <button class="btn btn-sm" onclick="Templates.showEdit(${t.id})">Edit</button>
                            ${!t.is_default ? `<button class="btn btn-sm" onclick="Templates.setDefault(${t.id})">Set Default</button>` : ''}
                            <button class="btn btn-sm btn-danger" onclick="Templates.remove(${t.id})">Delete</button>
                        </div>
                    </td>
                </tr>`;
            });

            html += '</tbody></table></div>';
            App.setContent(html);
        } catch (err) {
            console.error('Templates load error:', err);
            App.setContent(`<div class="empty-state"><h3>Failed to load templates</h3><p>${App.escapeHtml(err.message || '')}</p></div>`);
        }
    },

    preview(id) {
        const t = this._data.find(tpl => tpl.id === id);
        if (!t) return;
        App.setContent(`
            <div style="margin-bottom:12px">
                <button class="btn btn-secondary" onclick="Templates.render()">Back to Templates</button>
                <span style="margin-left:12px;font-size:1.1rem;font-weight:600">${App.escapeHtml(t.name || '')}</span>
            </div>
            <iframe class="preview-frame" sandbox="" srcdoc="${App.escapeHtml(t.body || '')}"></iframe>
        `);
    },

    showCreate() {
        App.setContent(`
            <div class="inline-form">
                <h3 style="margin-bottom:16px">Create Template</h3>
                <div class="form-group"><label>Name</label><input type="text" id="tplName" placeholder="Template name"></div>
                <div class="form-group">
                    <label>Type</label>
                    <select id="tplType"><option value="campaign">Campaign</option></select>
                </div>
                <div class="form-group"><label>Body (HTML)</label><textarea id="tplBody" rows="15" placeholder="HTML content"></textarea></div>
                <div class="form-actions">
                    <button class="btn btn-primary" onclick="Templates.create()">Create</button>
                    <button class="btn btn-secondary" onclick="Templates.render()">Cancel</button>
                </div>
            </div>
        `);
    },

    async showEdit(id) {
        const t = this._data.find(tpl => tpl.id === id);
        if (!t) { App.toast('Template not found', 'error'); return; }
        App.setContent(`
            <div class="inline-form">
                <h3 style="margin-bottom:16px">Edit Template #${t.id}</h3>
                <div class="form-group"><label>Name</label><input type="text" id="editTplName" value="${App.escapeHtml(t.name || '')}"></div>
                <div class="form-group"><label>Body (HTML)</label><textarea id="editTplBody" rows="15">${App.escapeHtml(t.body || '')}</textarea></div>
                <div class="form-actions">
                    <button class="btn btn-primary" onclick="Templates.update(${t.id})">Update</button>
                    <button class="btn btn-secondary" onclick="Templates.render()">Cancel</button>
                </div>
            </div>
        `);
    },

    async create() {
        const name = document.getElementById('tplName').value.trim();
        const body = document.getElementById('tplBody').value;
        if (!name) { App.toast('Name is required', 'error'); return; }
        try {
            await API.post('/api/templates', { name, body, type: 'campaign' });
            App.toast('Template created', 'success');
            this.render();
        } catch { /* API.request already surfaced the server message */ }
    },

    async update(id) {
        const name = document.getElementById('editTplName').value.trim();
        const body = document.getElementById('editTplBody').value;
        if (!name) { App.toast('Name is required', 'error'); return; }
        try {
            await API.put(`/api/templates/${id}`, { name, body });
            App.toast('Template updated', 'success');
            this.render();
        } catch { /* API.request already surfaced the server message */ }
    },

    async setDefault(id) {
        try {
            await API.put(`/api/templates/${id}/default`);
            App.toast('Default template updated', 'success');
            this.render();
        } catch { /* API.request already surfaced the server message */ }
    },

    async remove(id) {
        if (await App.confirm('Delete Template', 'Are you sure you want to delete this template?')) {
            try {
                await API.del(`/api/templates/${id}`);
                App.toast('Template deleted', 'success');
                this.render();
            } catch { /* API.request already surfaced the server message */ }
        }
    },
};

// ── Bounces Page ─────────────────────────────────────────
const Bounces = {
    page: 1,
    campaignFilter: 0,
    typeFilter: 'hard',
    campaigns: [],
    total: 0,
    perPage: 25,

    _queryParams() {
        const params = new URLSearchParams({
            page: String(this.page),
            per_page: String(this.perPage),
        });
        if (this.typeFilter) params.set('bounce_type', this.typeFilter);
        if (this.campaignFilter) params.set('campaign_id', String(this.campaignFilter));
        return params.toString();
    },

    _typeLabel() {
        if (this.typeFilter === 'hard') return 'hard bounces';
        if (this.typeFilter === 'soft') return 'soft bounces';
        return 'bounces';
    },

    async render() {
        try {
            const fetchCampaigns = !this.campaigns.length
                ? API.get('/api/campaigns?per_page=500&order_by=created_at&order=DESC')
                : Promise.resolve(null);

            const [campRes, bounceRes] = await Promise.all([
                fetchCampaigns,
                API.get(`/api/bounces?${this._queryParams()}`),
            ]);
            if (campRes) this.campaigns = campRes?.data?.results || [];

            const data = bounceRes?.data || {};
            const pageBounces = data.results || [];
            const total = data.total || 0;
            this.total = total;

            // Build campaign filter options
            const campOptions = this.campaigns.map(c =>
                `<option value="${c.id}" ${c.id === this.campaignFilter ? 'selected' : ''}>${App.escapeHtml(c.name)} (${c.status})</option>`
            ).join('');

            const filterLabel = this.campaignFilter
                ? this.campaigns.find(c => c.id === this.campaignFilter)?.name || `#${this.campaignFilter}`
                : 'All campaigns';

            const deleteActionLabel = this.campaignFilter
                ? `Delete ${this._typeLabel()}`
                : `Delete All ${this.typeFilter ? (this.typeFilter === 'hard' ? 'Hard ' : 'Soft ') : ''}Bounces`;

            // The page's actions belong in the filter row, next to the filters
            // they act on - the shared top bar keeps only the page title and
            // the global mode/theme controls.
            let html = `
                <div class="search-bar">
                    <select id="bounceCampFilter" onchange="Bounces.filterCampaign(this.value)" style="width:auto;min-width:240px">
                        <option value="0" ${!this.campaignFilter ? 'selected' : ''}>All Campaigns</option>
                        ${campOptions}
                    </select>
                    <select id="bounceTypeFilter" onchange="Bounces.filterType(this.value)" style="width:auto">
                        <option value="hard" ${this.typeFilter === 'hard' ? 'selected' : ''}>Hard Bounces</option>
                        <option value="soft" ${this.typeFilter === 'soft' ? 'selected' : ''}>Soft Bounces</option>
                        <option value="" ${!this.typeFilter ? 'selected' : ''}>All Bounce Types</option>
                    </select>
                    <span style="color:var(--text-secondary);font-size:0.9rem">
                        ${App.formatNumber(total)} ${this._typeLabel()}
                        ${this.campaignFilter ? ' for ' + filterLabel : ''}
                    </span>
                    <div class="page-actions">
                        <button class="btn btn-primary" onclick="Bounces.ingestBounces()">Ingest New Bounces</button>
                        <button class="btn" onclick="Bounces.exportBounces()">Export CSV</button>
                        <button class="btn btn-danger" onclick="Bounces.deleteAll()">${deleteActionLabel}</button>
                    </div>
                </div>
                <div class="table-wrapper"><table>
                    <thead><tr>
                        <th>ID</th><th>Email</th><th>Campaign</th><th>Source</th><th>Type</th><th>Date</th><th class="cell-actions">Actions</th>
                    </tr></thead><tbody>`;

            if (!pageBounces.length) {
                html += `<tr><td colspan="7"><div class="empty-state"><h3>No ${this._typeLabel()} found</h3></div></td></tr>`;
            }
            pageBounces.forEach(b => {
                const campName = App.escapeHtml(b.campaign?.name || '-');
                html += `<tr>
                    <td>${b.id}</td>
                    <td><strong>${App.escapeHtml(b.email || '-')}</strong></td>
                    <td style="max-width:250px;overflow:hidden;text-overflow:ellipsis">${campName}</td>
                    <td>${b.source || '-'}</td>
                    <td><span class="badge badge-${b.type === 'hard' ? 'danger' : 'warning'}">${b.type || 'unknown'}</span></td>
                    <td>${App.formatDate(b.created_at)}</td>
                    <td class="cell-actions"><button class="btn btn-sm btn-danger" onclick="Bounces.remove(${b.id})">Delete</button></td>
                </tr>`;
            });

            html += '</tbody></table></div>';
            html += App.renderPagination(this.page, total, this.perPage, 'Bounces.goToPage');
            App.setContent(html);
        } catch (err) {
            App.setContent(`<div class="empty-state"><h3>Failed to load bounces</h3><p>${App.escapeHtml(err.message || '')}</p></div>`);
        }
    },

    filterCampaign(val) {
        this.campaignFilter = parseInt(val) || 0;
        this.page = 1;
        this.render();
    },

    filterType(val) {
        this.typeFilter = val || '';
        this.page = 1;
        this.render();
    },

    goToPage(p) { this.page = p; this.render(); },

    async remove(id) {
        try {
            await API.del(`/api/bounces/${id}`);
            App.toast('Bounce deleted', 'success');
            this.render();
        } catch {
            // API.request already surfaced the server message.
        }
    },

    _campaignLabel() {
        if (this.campaignFilter) {
            return this.campaigns.find(c => c.id === this.campaignFilter)?.name
                || `#${this.campaignFilter}`;
        }
        return 'All Campaigns';
    },

    _campaignQuery() {
        const params = [];
        if (this.campaignFilter) params.push(`campaign_id=${this.campaignFilter}`);
        return params.length ? `?${params.join('&')}` : '';
    },

    async deleteAll() {
        const typeDesc = this.typeFilter ? `${this.typeFilter} ` : '';
        const label = this.campaignFilter
            ? `${typeDesc}bounces for "${this._campaignLabel()}"`
            : `ALL ${typeDesc}bounce records`;
        if (!await App.confirm('Delete Bounces', `This will permanently delete ${label}. Continue?`)) return;
        App.showProgress('Delete Bounces', `Deleting ${label}. Please wait...`);
        try {
            const params = new URLSearchParams();
            if (this.typeFilter) params.set('bounce_type', this.typeFilter);
            if (this.campaignFilter) params.set('campaign_id', String(this.campaignFilter));
            const result = await API.del(`/api/bounces?${params}`);
            const deleted = result?.deleted ?? 0;
            const errors = result?.errors ?? 0;
            App.showResult('Delete Bounces — Done', `
                <ul style="line-height:1.8">
                    <li>Deleted: <strong>${deleted}</strong></li>
                    ${errors ? `<li>Errors: <strong>${errors}</strong></li>` : ''}
                </ul>
            `);
            this.render();
        } catch (err) {
            App.showResult('Delete Bounces — Failed',
                `<p style="color:#e74c3c">${App.escapeHtml(err.message || 'Unknown error')}</p>`);
        }
    },

    async exportBounces() {
        App.showProgress('Export Bounces', 'Preparing export...');
        try {
            const params = new URLSearchParams();
            if (this.typeFilter) params.set('bounce_type', this.typeFilter);
            if (this.campaignFilter) params.set('campaign_id', String(this.campaignFilter));
            const result = await API.get(`/api/bounces/export?${params}`);
            if (!result.blob) {
                App.showResult('Export Bounces', `<p>No ${this._typeLabel()} to export.</p>`);
                return;
            }
            const typeSuffix = this.typeFilter ? `_${this.typeFilter}` : '';
            const suffix = this.campaignFilter
                ? `_${this.campaigns.find(c => c.id === this.campaignFilter)?.name?.replace(/[^a-zA-Z0-9]/g, '_').substring(0, 30) || this.campaignFilter}${typeSuffix}`
                : (typeSuffix || '_all');
            API.downloadBlob(result.blob, `bounces${suffix}.csv`);
            App.toast('Bounces exported', 'success');
            App.closeModal();
        } catch (err) {
            App.showResult('Export Bounces — Failed',
                `<p style="color:#e74c3c">${App.escapeHtml(err.message || 'Unknown error')}</p>`);
        }
    },

    async ingestBounces() {
        App.showProgress('Ingest New Bounces',
            'Scanning unseen bounce emails, classifying each, and creating matching records in ListMonk. Only truly invalid addresses become hard bounces. Please wait...');
        try {
            const result = await API.post('/api/bounces/ingest');
            if (result && result.error) {
                App.showResult('Ingest Bounces — Error',
                    `<p style="color:#e74c3c">${App.escapeHtml(result.error)}</p>`);
                return;
            }
            const r = result || {};
            const reasons = r.skipped_reasons || {};
            const reasonList = Object.entries(reasons)
                .map(([k, v]) => `<li>${App.escapeHtml(k)}: <strong>${v}</strong></li>`)
                .join('');
            App.showResult('Ingest Bounces — Done', `
                <ul style="line-height:1.8">
                    <li>Emails scanned: <strong>${r.scanned || 0}</strong></li>
                    <li>Ingested: <strong>${r.ingested || 0}</strong>
                        (hard: <strong>${r.hard || 0}</strong>, soft: <strong>${r.soft || 0}</strong>)</li>
                    <li>Skipped: <strong>${r.skipped || 0}</strong></li>
                    ${reasonList ? `<li>Skip reasons:<ul>${reasonList}</ul></li>` : ''}
                    <li>Errors: <strong>${r.errors || 0}</strong></li>
                    ${r.message ? `<li>${App.escapeHtml(r.message)}</li>` : ''}
                </ul>
            `);
            if (r.ingested > 0) {
                this.render();
            }
        } catch (err) {
            App.showResult('Ingest Bounces — Failed',
                `<p style="color:#e74c3c">${App.escapeHtml(err.message || 'Unknown error')}</p>`);
        }
    },

};

// Initialize on DOM ready
document.addEventListener('DOMContentLoaded', () => App.init());
