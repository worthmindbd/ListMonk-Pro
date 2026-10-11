/**
 * Campaigns page - CRUD, status control, analytics, preview.
 */
const Campaigns = {
    page: 1,
    query: '',
    statusFilter: '',
    analyticsChart: null,

    async render() {
        try {
            let params = `page=${this.page}&per_page=50`;
            if (this.query) params += `&query=${encodeURIComponent(this.query)}`;
            if (this.statusFilter) params += `&status=${this.statusFilter}`;

            const result = await API.get(`/api/campaigns?${params}`);
            const data = result?.data || {};
            const campaigns = data.results || [];
            const total = data.total || 0;

            App.setActions(`
                <button class="btn btn-sm" onclick="Campaigns.exportAll()">Export CSV</button>
                <a href="#/analytics" class="btn btn-sm">Analytics</a>
                <button class="btn btn-sm btn-primary" onclick="Campaigns.showCreate()">+ New Campaign</button>
            `);

            let html = `
                <div class="search-bar">
                    <div class="search-input">
                        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="11" cy="11" r="8"/><line x1="21" y1="21" x2="16.65" y2="16.65"/></svg>
                        <input type="search" id="campSearch" placeholder="Search campaigns..."
                            aria-label="Search campaigns"
                            value="${App.escapeHtml(this.query)}" onkeydown="if(event.key==='Enter')Campaigns.search()">
                    </div>
                    <select id="campStatusFilter" onchange="Campaigns.filterStatus(this.value)" style="width:auto">
                        <option value="" ${!this.statusFilter ? 'selected' : ''}>All Status</option>
                        <option value="draft" ${this.statusFilter === 'draft' ? 'selected' : ''}>Draft</option>
                        <option value="running" ${this.statusFilter === 'running' ? 'selected' : ''}>Running</option>
                        <option value="finished" ${this.statusFilter === 'finished' ? 'selected' : ''}>Finished</option>
                        <option value="paused" ${this.statusFilter === 'paused' ? 'selected' : ''}>Paused</option>
                        <option value="cancelled" ${this.statusFilter === 'cancelled' ? 'selected' : ''}>Cancelled</option>
                        <option value="scheduled" ${this.statusFilter === 'scheduled' ? 'selected' : ''}>Scheduled</option>
                    </select>
                    <button class="btn" onclick="Campaigns.search()">Search</button>
                </div>
                <div class="table-wrapper"><table>
                    <thead><tr>
                        <th>ID</th><th>Name</th><th>Subject</th><th>Status</th><th>Lists</th><th>Sent</th><th>Created</th><th class="cell-actions">Actions</th>
                    </tr></thead><tbody>`;

            if (!campaigns.length) {
                html += '<tr><td colspan="8"><div class="empty-state"><h3>No campaigns found</h3></div></td></tr>';
            }

            campaigns.forEach(c => {
                const listNames = (c.lists || []).map(l => `<span class="tag">${App.escapeHtml(l.name)}</span>`).join('') || '-';
                const sent = c.to_send || 0;
                html += `<tr>
                    <td>${c.id}</td>
                    <td><strong>${App.escapeHtml(c.name)}</strong></td>
                    <td style="max-width:200px;overflow:hidden;text-overflow:ellipsis">${App.escapeHtml(c.subject) || '-'}</td>
                    <td>${App.statusBadge(c.status)}</td>
                    <td>${listNames}</td>
                    <td>${App.formatNumber(sent)}</td>
                    <td>${App.formatDate(c.created_at)}</td>
                    <td class="cell-actions">
                        <div class="action-btns">
                            <button class="btn btn-sm" onclick="Campaigns.showDetail(${c.id})">View</button>
                            ${c.status === 'draft' ? `<button class="btn btn-sm btn-primary" onclick="Campaigns.changeStatus(${c.id},'running')">Start</button>` : ''}
                            ${c.status === 'running' ? `<button class="btn btn-sm" style="border-color:var(--warning);color:var(--warning)" onclick="Campaigns.changeStatus(${c.id},'paused')">Pause</button>` : ''}
                            ${c.status === 'paused' ? `<button class="btn btn-sm btn-primary" onclick="Campaigns.changeStatus(${c.id},'running')">Resume</button>` : ''}
                            <button class="btn btn-sm btn-danger" onclick="Campaigns.remove(${c.id})">Delete</button>
                        </div>
                    </td>
                </tr>`;
            });

            html += '</tbody></table></div>';
            html += App.renderPagination(this.page, total, 50, 'Campaigns.goToPage');
            App.setContent(html);
        } catch {
            App.setContent('<div class="empty-state"><h3>Failed to load campaigns</h3></div>');
        }
    },

    search() {
        this.query = document.getElementById('campSearch')?.value || '';
        this.page = 1;
        this.render();
    },

    filterStatus(status) {
        this.statusFilter = status;
        this.page = 1;
        this.render();
    },

    goToPage(p) { this.page = p; this.render(); },

    async showCreate() {
        // Fetch lists for selection
        let lists = [];
        try {
            const res = await API.get('/api/lists?per_page=100&minimal=true');
            lists = res?.data?.results || [];
        } catch {}

        // Fetch templates for selection
        let templates = [];
        try {
            const res = await API.get('/api/templates');
            templates = res?.data || [];
        } catch {}

        let listOptions = lists.map(l =>
            `<label class="checkbox-label"><input type="checkbox" name="campLists" value="${l.id}"><span>${App.escapeHtml(l.name)}</span></label>`
        ).join('');

        let tplOptions = templates.map(t =>
            `<option value="${t.id}" ${t.is_default ? 'selected' : ''}>${App.escapeHtml(t.name)}</option>`
        ).join('');

        App.setContent(`
            <div class="inline-form">
                <h3 style="margin-bottom:16px">Create Campaign</h3>
                <div class="form-grid">
                    <div class="form-group"><label>Name *</label><input type="text" id="campName" placeholder="Campaign name"></div>
                    <div class="form-group"><label>Subject *</label><input type="text" id="campSubject" placeholder="Email subject line"></div>
                </div>
                <div class="form-group">
                    <label>Lists *</label>
                    <div class="checkbox-group">${listOptions || '<span style="color:var(--text-muted)">No lists available</span>'}</div>
                </div>
                <div class="form-grid">
                    <div class="form-group">
                        <label>Template</label>
                        <select id="campTemplate">${tplOptions}</select>
                    </div>
                    <div class="form-group">
                        <label>Content Type</label>
                        <select id="campContentType">
                            <option value="richtext">Rich Text</option>
                            <option value="html">HTML</option>
                            <option value="markdown">Markdown</option>
                            <option value="plain">Plain Text</option>
                        </select>
                    </div>
                </div>
                <div class="form-group">
                    <label>Body</label>
                    <textarea id="campBody" rows="10" placeholder="Email body content"></textarea>
                </div>
                <div class="form-group"><label>Tags (comma-separated)</label><input type="text" id="campTags" placeholder="newsletter, promo"></div>
                <div class="form-actions">
                    <button class="btn btn-primary" onclick="Campaigns.create()">Create Campaign</button>
                    <button class="btn btn-secondary" onclick="Campaigns.render()">Cancel</button>
                </div>
            </div>
        `);
    },

    async create() {
        const name = document.getElementById('campName').value.trim();
        const subject = document.getElementById('campSubject').value.trim();
        const templateId = parseInt(document.getElementById('campTemplate').value);
        const contentType = document.getElementById('campContentType').value;
        const body = document.getElementById('campBody').value;
        const tags = document.getElementById('campTags').value.split(',').map(t => t.trim()).filter(Boolean);

        const selectedLists = Array.from(document.querySelectorAll('input[name="campLists"]:checked'))
            .map(cb => parseInt(cb.value));

        if (!name || !subject) { App.toast('Name and subject are required', 'error'); return; }
        if (!selectedLists.length) { App.toast('Select at least one list', 'error'); return; }

        try {
            await API.post('/api/campaigns', {
                name, subject, body, content_type: contentType,
                type: 'regular', lists: selectedLists,
                template_id: templateId, tags,
            });
            App.toast('Campaign created', 'success');
            this.render();
        } catch {
            // API.request already surfaced the server message.
        }
    },

    async showDetail(id) {
        try {
            const [result, unsubRes] = await Promise.allSettled([
                API.get(`/api/campaigns/${id}`),
                API.get(`/api/unsubscribes/campaign/${id}`),
            ]);

            const c = result.status === 'fulfilled' ? (result.value?.data || {}) : {};
            if (!c.id) throw new Error('Campaign not found');

            const unsubData = unsubRes.status === 'fulfilled' ? (unsubRes.value?.data || {}) : {};
            const unsubTotal = unsubData.total || 0;
            const unsubEmail = unsubData.email_count || 0;
            const unsubLink = unsubData.link_count || 0;

            const lists = (c.lists || []).map(l => `<span class="tag">${App.escapeHtml(l.name)}</span>`).join('') || '-';
            const tags = (c.tags || []).map(t => `<span class="tag">${App.escapeHtml(t)}</span>`).join('') || '-';

            const openRate = c.sent ? ((c.views || 0) / c.sent * 100).toFixed(1) : '0';
            const ctr = c.sent ? ((c.clicks || 0) / c.sent * 100).toFixed(1) : '0';
            const bounceRate = c.sent ? ((c.bounces || 0) / c.sent * 100).toFixed(1) : '0';

            App.setContent(`
                <div class="inline-form">
                    <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:20px">
                        <h3>${App.escapeHtml(c.name)}</h3>
                        <div class="action-btns">
                            <button class="btn btn-sm" onclick="Campaigns.viewAnalytics(${c.id})">View Analytics</button>
                            <button class="btn btn-sm" onclick="Campaigns.exportCampaign(${c.id})">Export CSV</button>
                            <button class="btn btn-sm" onclick="Campaigns.preview(${c.id})">Preview</button>
                            <button class="btn btn-sm" onclick="Campaigns.showEdit(${c.id})">Edit</button>
                            <button class="btn btn-sm btn-secondary" onclick="Campaigns.render()">Back</button>
                        </div>
                    </div>
                    <div class="stats-grid">
                        <div class="stat-card"><div class="stat-label">Status</div><div style="margin-top:4px">${App.statusBadge(c.status)}</div></div>
                        <div class="stat-card accent"><div class="stat-label">To Send</div><div class="stat-value">${App.formatNumber(c.to_send || 0)}</div></div>
                        <div class="stat-card success"><div class="stat-label">Sent</div><div class="stat-value">${App.formatNumber(c.sent || 0)}</div></div>
                        <div class="stat-card" style="border-left:3px solid var(--success)">
                            <div class="stat-label">Views</div>
                            <div class="stat-value">${App.formatNumber(c.views || 0)}</div>
                            <div style="font-size:0.8rem;color:var(--text-muted)">${openRate}% open rate</div>
                        </div>
                        <div class="stat-card" style="border-left:3px solid var(--warning)">
                            <div class="stat-label">Clicks</div>
                            <div class="stat-value">${App.formatNumber(c.clicks || 0)}</div>
                            <div style="font-size:0.8rem;color:var(--text-muted)">${ctr}% CTR</div>
                        </div>
                        <div class="stat-card" style="border-left:3px solid var(--danger)">
                            <div class="stat-label">Bounces</div>
                            <div class="stat-value">${App.formatNumber(c.bounces || 0)}</div>
                            <div style="font-size:0.8rem;color:var(--text-muted)">${bounceRate}% bounce rate</div>
                        </div>
                        <div class="stat-card" style="border-left:3px solid var(--accent)">
                            <div class="stat-label">Unsubscribes</div>
                            <div class="stat-value">${App.formatNumber(unsubTotal)}</div>
                            <div style="font-size:0.8rem;color:var(--text-muted)">${unsubEmail} email · ${unsubLink} link</div>
                        </div>
                    </div>
                    <table style="width:auto">
                        <tr><td style="color:var(--text-muted);padding:8px 20px 8px 0">Subject</td><td>${App.escapeHtml(c.subject) || '-'}</td></tr>
                        <tr><td style="color:var(--text-muted);padding:8px 20px 8px 0">Lists</td><td>${lists}</td></tr>
                        <tr><td style="color:var(--text-muted);padding:8px 20px 8px 0">Tags</td><td>${tags}</td></tr>
                        <tr><td style="color:var(--text-muted);padding:8px 20px 8px 0">Content Type</td><td>${c.content_type || '-'}</td></tr>
                        <tr><td style="color:var(--text-muted);padding:8px 20px 8px 0">Created</td><td>${App.formatDateTime(c.created_at)}</td></tr>
                        <tr><td style="color:var(--text-muted);padding:8px 20px 8px 0">Updated</td><td>${App.formatDateTime(c.updated_at)}</td></tr>
                        <tr><td style="color:var(--text-muted);padding:8px 20px 8px 0">Started</td><td>${App.formatDateTime(c.started_at)}</td></tr>
                    </table>
                </div>
            `);
        } catch {
            // API.request already surfaced the server message.
        }
    },

    viewAnalytics(id) {
        // Jump to analytics page with this campaign pre-selected
        Analytics.selectedCampaignId = id;
        Analytics.fromDate = '';
        Analytics.toDate = '';
        window.location.hash = '#/analytics';
    },

    async exportCampaign(id) {
        const types = ['views', 'clicks', 'bounces'];
        let exported = 0;

        for (const type of types) {
            try {
                const result = await API.get(`/api/campaigns/${id}/subscribers/${type}/export`);
                if (result.blob) {
                    API.downloadBlob(result.blob, `campaign_${id}_${type}.csv`);
                    exported++;
                }
            } catch {}
        }

        if (exported > 0) {
            App.toast(`Exported ${exported} subscriber list(s)`, 'success');
        } else {
            App.toast('No data to export', 'error');
        }
    },

    async showEdit(id) {
        try {
            const [campRes, listsRes] = await Promise.all([
                API.get(`/api/campaigns/${id}`),
                API.get('/api/lists?per_page=100&minimal=true'),
            ]);

            const c = campRes?.data || {};
            const allLists = listsRes?.data?.results || [];
            const selectedIds = (c.lists || []).map(l => l.id);

            let listOptions = allLists.map(l =>
                `<label class="checkbox-label"><input type="checkbox" name="editCampLists" value="${l.id}" ${selectedIds.includes(l.id) ? 'checked' : ''}><span>${App.escapeHtml(l.name)}</span></label>`
            ).join('');

            App.setContent(`
                <div class="inline-form">
                    <h3 style="margin-bottom:16px">Edit Campaign #${c.id}</h3>
                    <div class="form-grid">
                        <div class="form-group"><label>Name</label><input type="text" id="editCampName" value="${App.escapeHtml(c.name || '')}"></div>
                        <div class="form-group"><label>Subject</label><input type="text" id="editCampSubject" value="${App.escapeHtml(c.subject || '')}"></div>
                    </div>
                    <div class="form-group">
                        <label>Lists</label>
                        <div class="checkbox-group">${listOptions}</div>
                    </div>
                    <div class="form-group">
                        <label>Body</label>
                        <textarea id="editCampBody" rows="10">${App.escapeHtml(c.body || '')}</textarea>
                    </div>
                    <div class="form-group"><label>Tags</label><input type="text" id="editCampTags" value="${App.escapeHtml((c.tags || []).join(', '))}"></div>
                    <div class="form-actions">
                        <button class="btn btn-primary" onclick="Campaigns.update(${c.id})">Update</button>
                        <button class="btn btn-secondary" onclick="Campaigns.showDetail(${c.id})">Cancel</button>
                    </div>
                </div>
            `);
        } catch {
            // API.request already surfaced the server message.
        }
    },

    async update(id) {
        const name = document.getElementById('editCampName').value.trim();
        const subject = document.getElementById('editCampSubject').value.trim();
        const body = document.getElementById('editCampBody').value;
        const tags = document.getElementById('editCampTags').value.split(',').map(t => t.trim()).filter(Boolean);
        const lists = Array.from(document.querySelectorAll('input[name="editCampLists"]:checked'))
            .map(cb => parseInt(cb.value));

        try {
            await API.put(`/api/campaigns/${id}`, { name, subject, body, lists, tags });
            App.toast('Campaign updated', 'success');
            this.showDetail(id);
        } catch {
            // API.request already surfaced the server message.
        }
    },

    async changeStatus(id, status) {
        const action = status === 'running' ? 'start' : status;
        if (await App.confirm('Change Status', `Are you sure you want to ${action} this campaign?`)) {
            try {
                await API.put(`/api/campaigns/${id}/status`, { status });
                App.toast(`Campaign ${action}ed`, 'success');
                this.render();
            } catch {
                // API.request already surfaced the server message.
            }
        }
    },

    async preview(id) {
        try {
            const result = await API.get(`/api/campaigns/${id}/preview`);
            App.setContent(`
                <div style="margin-bottom:12px">
                    <button class="btn btn-secondary" onclick="Campaigns.showDetail(${id})">Back to Campaign</button>
                </div>
                <iframe class="preview-frame" sandbox="" srcdoc="${App.escapeHtml(result.html)}"></iframe>
            `);
        } catch {
            // API.request already surfaced the server message.
        }
    },

    async remove(id) {
        if (await App.confirm('Delete Campaign', 'Are you sure you want to delete this campaign?')) {
            try {
                await API.del(`/api/campaigns/${id}`);
                App.toast('Campaign deleted', 'success');
                this.render();
            } catch {
                // API.request already surfaced the server message.
            }
        }
    },

    // ── Export ────────────────────────────────────────────
    async exportAll() {
        try {
            const result = await API.get('/api/campaigns/export-all');
            if (result.blob) {
                API.downloadBlob(result.blob, 'campaigns_export.csv');
                App.toast('Campaigns exported', 'success');
            }
        } catch {
            // API.request already surfaced the server message.
        }
    },
};
