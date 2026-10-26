/**
 * Unsubscribes management page — campaign-grouped view.
 * Click a campaign card → navigate to detail view showing all records.
 */
const Unsubscribes = {
    stats: null,
    imapStatus: null,
    campaignGroups: [],
    settings: { blocklist_enabled: false },
    // Detail view state
    activeCampaign: null,
    campaignRecords: { results: [], total: 0 },
    campaignPage: 1,
    campaignPerPage: 50,
    selectedEmails: new Set(),

    /* ── Helpers ──────────────────────────────────────────── */
    /** Convert YYYY-MM key to readable label, e.g. "March 2026" */
    formatCampaignDate(key) {
        if (!key) return 'Unknown';
        const months = ['January','February','March','April','May','June',
                        'July','August','September','October','November','December'];
        const parts = key.split('-');
        if (parts.length === 2) {
            const year = parts[0];
            const monthIdx = parseInt(parts[1], 10) - 1;
            if (monthIdx >= 0 && monthIdx < 12) return `${months[monthIdx]} ${year}`;
        }
        return key;
    },

    /** Strip leading date prefix from raw Listmonk campaign name */
    cleanCampaignName(name) {
        if (!name || name === 'Unknown') return 'No matching campaign';
        // Strip leading YYYYMMDD or YYYY-MM-DD prefix
        let cleaned = name.replace(/^\d{4}-?\d{2}-?\d{2}\s*/, '').trim();
        return cleaned || name;
    },

    /** Render a source badge for a log record. */
    renderSourceBadge(record) {
        const source = record.source || 'email';
        if (source === 'link') {
            return '<span class="badge badge-primary" title="Clicked unsubscribe link">link click</span>';
        }
        const kw = App.escapeHtml(record.keyword || 'email reply');
        return `<span class="badge badge-warning" title="${kw}">email reply</span>`;
    },

    async render() {
        this._seq = App.seq();
        App.setContent('<div class="loading-spinner">Loading unsubscribes…</div>');

        try {
            const [statsRes, statusRes, groupsRes, settingsRes] = await Promise.allSettled([
                API.get('/api/unsubscribes/stats'),
                API.get('/api/unsubscribes/imap-status'),
                API.get('/api/unsubscribes/campaigns'),
                API.get('/api/unsubscribes/settings'),
            ]);

            this.stats = statsRes.status === 'fulfilled' ? statsRes.value : { total: 0, today: 0, this_week: 0 };
            this.imapStatus = statusRes.status === 'fulfilled' ? statusRes.value : { configured: false, connected: false };
            this.campaignGroups = groupsRes.status === 'fulfilled' ? (groupsRes.value.data || []) : [];
            this.settings = settingsRes.status === 'fulfilled' ? settingsRes.value : { blocklist_enabled: false };

            // Deep-linkable detail view: #/unsubscribes?campaign=6
            const rq = App.routeQuery();
            const wanted = parseInt(rq.get('campaign') || '', 10);
            if (!this.activeCampaign && Number.isFinite(wanted) && wanted) {
                const group = this.campaignGroups.find(g => g.campaign_id === wanted);
                if (group) this.activeCampaign = group;
            }

            // If we're in detail view, stay there
            if (this.activeCampaign) {
                await this.loadCampaignRecords(this.activeCampaign.campaign_id);
                this.renderDetailView();
            } else {
                this.renderListView();
            }
        } catch (err) {
            if (App.stale(this._seq)) return;
            App.setContent(`<div class="empty-state"><h2>Failed to load unsubscribes</h2><p>${App.escapeHtml(err.message || '')}</p></div>`);
        }
    },

    /* ── List View — shows campaign cards ──────────────────── */
    renderListView() {
        if (this._seq !== undefined && App.stale(this._seq)) return;
        this.activeCampaign = null;
        this.selectedEmails.clear();
        App.selection.clear('unsub_campaigns');

        const stats = this.stats;
        const imap = this.imapStatus;
        const groups = this.campaignGroups;

        // IMAP badge
        let imapBadge;
        if (!imap.configured) {
            imapBadge = '<span class="badge badge-default" style="font-size:0.85rem;padding:6px 14px">IMAP Not Configured</span>';
        } else if (imap.connected) {
            imapBadge = '<span class="badge badge-success" style="font-size:0.85rem;padding:6px 14px">IMAP Connected</span>';
        } else {
            imapBadge = `<span class="badge badge-danger" style="font-size:0.85rem;padding:6px 14px">IMAP Error: ${App.escapeHtml(imap.error || 'Unknown')}</span>`;
        }

        // Only campaigns with a real ListMonk id can be deleted as a group
        // (records without one have no campaign to match on).
        const selectable = groups.filter(g => Number.isInteger(g.campaign_id));
        App.selection.register('unsub_campaigns', {
            noun: 'campaign',
            pageValues: selectable.map(g => String(g.campaign_id)),
            actions: [
                { label: 'Delete Records', onclick: 'Unsubscribes.bulkDeleteCampaigns()', danger: true },
            ],
        });

        let html = `
            <!-- IMAP Status -->
            <div class="card unsub-monitor-card" style="margin-bottom:20px">
                <div class="card-header">
                    <h2 class="card-title">IMAP Unsubscribe Monitor</h2>
                    <div class="page-actions">
                        ${imapBadge}
                        <button class="btn" onclick="Unsubscribes.exportAllCSV()">
                            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="14" height="14"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="7 10 12 15 17 10"/><line x1="12" y1="15" x2="12" y2="3"/></svg>
                            Export All
                        </button>
                        <button class="btn" onclick="Unsubscribes.resetAll()" title="Undo all unsubscribes and restore list memberships">
                            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="14" height="14"><path d="M3 12a9 9 0 1 0 9-9 9.75 9.75 0 0 0-6.74 2.74L3 8"/><polyline points="3 3 3 8 8 8"/></svg>
                            Undo All
                        </button>
                        <button class="btn btn-primary" onclick="Unsubscribes.triggerScan()">
                            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="14" height="14"><polyline points="23 4 23 10 17 10"/><path d="M20.49 15a9 9 0 1 1-2.12-9.36L23 10"/></svg>
                            Scan Now
                        </button>
                    </div>
                </div>
                <div class="unsub-monitor-info">
                    <div>
                        <span style="color:var(--text-muted)">Scan interval:</span>
                        <strong>Every 1 hour</strong>
                    </div>
                    <div>
                        <span style="color:var(--text-muted)">Keywords:</span>
                        <code style="font-size:0.8rem">"Remove me"</code>,
                        <code style="font-size:0.8rem">"Unsubscribe me"</code>,
                        <code style="font-size:0.8rem">"Exclude me"</code>
                    </div>
                    <div>
                        <span style="color:var(--text-muted)">Scans:</span>
                        <strong>Reply text only</strong>
                        <span style="color:var(--text-muted);font-size:0.75rem">(excludes quoted content)</span>
                    </div>
                    <div class="unsub-action-toggle">
                        <span style="color:var(--text-muted)">Action:</span>
                        <strong>${this.settings.blocklist_enabled ? 'Unsubscribe + Blocklist' : 'Unsubscribe Only'}</strong>
                        <label class="toggle-switch" style="margin-left:4px" title="Toggle blocklist: when ON, leads are also blocklisted">
                            <input type="checkbox" ${this.settings.blocklist_enabled ? 'checked' : ''} onchange="Unsubscribes.toggleBlocklist(this.checked)">
                            <span class="toggle-slider"></span>
                        </label>
                    </div>
                    <div>
                        <span style="color:var(--text-muted)">Sources:</span>
                        <strong>${App.formatNumber(stats.email_count || 0)} email repl${(stats.email_count || 0) !== 1 ? 'ies' : 'y'}</strong>
                        <span style="color:var(--text-muted);margin:0 6px">·</span>
                        <strong>${App.formatNumber(stats.link_count || 0)} link click${(stats.link_count || 0) !== 1 ? 's' : ''}</strong>
                    </div>
                </div>
            </div>

            <!-- Stats -->
            <div class="stats-grid">
                <div class="stat-card accent">
                    <div class="stat-label">Total Unsubscribes</div>
                    <div class="stat-value">${App.formatNumber(stats.total)}</div>
                </div>
                <div class="stat-card warning">
                    <div class="stat-label">Today</div>
                    <div class="stat-value">${App.formatNumber(stats.today)}</div>
                </div>
                <div class="stat-card">
                    <div class="stat-label">This Week</div>
                    <div class="stat-value">${App.formatNumber(stats.this_week)}</div>
                </div>
            </div>

            <!-- Campaign Groups -->
            <div class="card" style="margin-top:20px">
                <div class="card-header">
                    <h2 class="card-title">Unsubscribes by Campaign</h2>
                    <span style="color:var(--text-muted);font-size:0.85rem">${groups.length} campaign(s)</span>
                </div>

                ${App.selection.bar('unsub_campaigns')}`;

        if (!groups.length) {
            html += `
                <div class="empty-state" style="padding:40px">
                    <h2>No unsubscribe campaigns detected</h2>
                    <p>The system monitors your inbox every hour. Unsubscribes will be grouped by campaign month.</p>
                </div>`;
        } else {
            html += '<div class="unsub-campaigns-list">';
            groups.forEach(g => {
                const readableDate = this.formatCampaignDate(g.campaign_key);
                const cleanName = App.escapeHtml(this.cleanCampaignName(g.campaign_name));
                const isClickable = Number.isInteger(g.campaign_id);
                const isSelected = App.selection.has('unsub_campaigns', g.campaign_id);
                html += `
                    <div class="unsub-campaign-card ${isSelected ? 'selected' : ''}" ${isClickable ? `role="button" tabindex="0" aria-label="Open unsubscribe records for ${cleanName}" onclick="Unsubscribes.openCampaign(${g.campaign_id})" onkeydown="if(event.key==='Enter'||event.key===' '){event.preventDefault();Unsubscribes.openCampaign(${g.campaign_id})}"` : 'style="cursor:default;opacity:0.75"'}>
                        <div class="unsub-campaign-header">
                            <div class="unsub-campaign-info">
                                ${isClickable ? `<span onclick="event.stopPropagation()" style="margin-right:10px">${App.selection.rowbox('unsub_campaigns', g.campaign_id, `Select ${this.cleanCampaignName(g.campaign_name)}`)}</span>` : ''}
                                <span class="badge badge-primary" style="font-size:0.8rem;padding:4px 10px;margin-right:8px">${App.escapeHtml(readableDate)}</span>
                                <strong>${cleanName}</strong>
                            </div>
                            <div class="unsub-campaign-meta">
                                <span class="badge badge-default" style="font-size:0.8rem;text-transform:uppercase">${g.count} unsubscribe${g.count !== 1 ? 's' : ''}</span>
                                ${isClickable ? `
                                    <button class="btn btn-sm btn-danger" style="margin-left:8px;padding:2px 8px;font-size:0.72rem"
                                        onclick="event.stopPropagation();Unsubscribes.removeCampaign(${g.campaign_id})">Remove</button>
                                    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="16" height="16" style="margin-left:8px;color:var(--text-muted)"><polyline points="9 18 15 12 9 6"/></svg>` : ''}
                            </div>
                        </div>
                    </div>`;
            });
            html += '</div>';
        }

        html += '</div>';
        App.setContent(html);
    },

    /* ── Detail View — shows records for a single campaign ── */
    async openCampaign(campaignId) {
        this.activeCampaign = this.campaignGroups.find(g => g.campaign_id === campaignId) || { campaign_id: campaignId, campaign_key: 'unknown', campaign_name: 'No matching campaign', count: 0 };
        this.campaignPage = 1;
        this.selectedEmails.clear();
        // The detail view is addressable so Back/reload return to it.
        App.syncUrl('unsubscribes', { campaign: campaignId });

        App.setContent('<div class="loading-spinner">Loading campaign records…</div>');

        await this.loadCampaignRecords(campaignId);
        this.renderDetailView();
    },

    async loadCampaignRecords(campaignId) {
        try {
            const res = await API.get(`/api/unsubscribes/campaign/${campaignId}?page=${this.campaignPage}&per_page=${this.campaignPerPage}`);
            this.campaignRecords = res.data || { results: [], total: 0 };
        } catch {
            this.campaignRecords = { results: [], total: 0 };
        }
    },

    renderDetailView() {
        if (this._seq !== undefined && App.stale(this._seq)) return;
        const camp = this.activeCampaign;
        const data = this.campaignRecords;
        const records = data.results || [];
        const total = data.total || 0;
        const linkCount = data.link_count || 0;
        const emailCount = data.email_count || 0;
        const hasSelected = this.selectedEmails.size > 0;
        const readableDate = this.formatCampaignDate(camp.campaign_key);
        const cleanName = App.escapeHtml(this.cleanCampaignName(camp.campaign_name));

        let html = `
            <!-- Back button + header -->
            <div style="margin-bottom:20px">
                <button class="btn btn-sm" onclick="Unsubscribes.backToList()" style="margin-bottom:12px">
                    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="14" height="14" style="margin-right:4px"><line x1="19" y1="12" x2="5" y2="12"/><polyline points="12 19 5 12 12 5"/></svg>
                    Back to Campaigns
                </button>
                <div class="card">
                    <div class="card-header" style="margin-bottom:0">
                        <div style="display:flex;align-items:center;gap:10px">
                            <span class="badge badge-primary" style="font-size:0.9rem;padding:6px 14px">${App.escapeHtml(readableDate)}</span>
                            <h3 class="card-title" style="margin:0">${cleanName}</h3>
                        </div>
                        <div class="page-actions">
                            <button id="unsubBulkDeleteBtn" class="btn ${hasSelected ? 'btn-danger' : ''}" onclick="Unsubscribes.bulkDeleteSelected()" ${!hasSelected ? 'disabled' : ''}>
                                <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="14" height="14"><polyline points="3 6 5 6 21 6"/><path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/></svg>
                                <span id="unsubBulkDeleteLabel">Delete Selected${hasSelected ? ` (${this.selectedEmails.size})` : ''}</span>
                            </button>
                            <button class="btn" onclick="Unsubscribes.exportCampaignCSV(${camp.campaign_id})">
                                <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="14" height="14"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="7 10 12 15 17 10"/><line x1="12" y1="15" x2="12" y2="3"/></svg>
                                Export CSV
                            </button>
                            <button class="btn btn-danger" onclick="Unsubscribes.removeCampaign(${camp.campaign_id})">
                                Remove List
                            </button>
                        </div>
                    </div>
                </div>
            </div>

            <!-- Campaign stats -->
            <div class="stats-grid" style="margin-bottom:20px">
                <div class="stat-card accent">
                    <div class="stat-label">Total Unsubscribes</div>
                    <div class="stat-value">${App.formatNumber(total)}</div>
                </div>
                <div class="stat-card warning">
                    <div class="stat-label">Email Replies</div>
                    <div class="stat-value">${App.formatNumber(emailCount)}</div>
                </div>
                <div class="stat-card">
                    <div class="stat-label">Link Clicks</div>
                    <div class="stat-value">${App.formatNumber(linkCount)}</div>
                </div>
            </div>

            <!-- Records table -->
            <div class="card">
                <div class="table-wrapper" tabindex="0" role="region" aria-label="Scrollable data table"><table>
                    <thead><tr>
                        <th style="width:30px"><input type="checkbox" id="unsubSelectAllBox" aria-label="Select all records on this page" onchange="Unsubscribes.toggleSelectAll(this)" ${this._isPageFullySelected() ? 'checked' : ''}/></th>
                        <th>#</th><th>Email</th><th>Name</th><th>Type</th><th>Date</th><th>Actions</th>
                    </tr></thead><tbody>`;

        if (!records.length) {
            html += '<tr><td colspan="7"><div class="empty-state"><p>No records</p></div></td></tr>';
        }

        records.forEach((r, i) => {
            const idx = (this.campaignPage - 1) * this.campaignPerPage + i + 1;
            const email = App.escapeHtml(r.email || '-');
            const name = App.escapeHtml(r.name || '-');
            const isChecked = this.selectedEmails.has(r.email);

            html += `<tr>
                <td><input type="checkbox" class="unsub-row-select" value="${email}" aria-label="Select ${email}" onchange="Unsubscribes.toggleSelect(${i}, this)" ${isChecked ? 'checked' : ''}/></td>
                <td>${idx}</td>
                <td><strong>${email}</strong></td>
                <td>${name}</td>
                <td>${Unsubscribes.renderSourceBadge(r)}</td>
                <td>${App.formatDateTime(r.timestamp)}</td>
                <td>
                    <button class="btn btn-sm btn-danger" style="padding:2px 8px;font-size:0.75rem" onclick="Unsubscribes.deleteRecord(${App.escapeHtml(JSON.stringify(r.email))})">
                        Remove
                    </button>
                </td>
            </tr>`;
        });

        html += '</tbody></table></div>';
        html += App.renderPagination(this.campaignPage, total, this.campaignPerPage, 'Unsubscribes.goToCampaignPage');
        html += '</div>';

        App.setContent(html);
    },

    /* ── Navigation ───────────────────────────────────────── */
    backToList() {
        this.activeCampaign = null;
        this.selectedEmails.clear();
        App.syncUrl('unsubscribes', {});
        this.renderListView();
    },

    /* ── Selection ────────────────────────────────────────── */
    /** Emails selected on the *current* page (the set also spans pages). */
    _pageEmails() {
        return (this.campaignRecords.results || []).map(r => r.email).filter(Boolean);
    },

    /** True only when every row on this page is selected. */
    _isPageFullySelected() {
        const emails = this._pageEmails();
        return emails.length > 0 && emails.every(e => this.selectedEmails.has(e));
    },

    toggleSelect(idx, checkbox) {
        const records = this.campaignRecords.results || [];
        const email = records[idx]?.email;
        if (!email) return;
        const checked = checkbox ? checkbox.checked : !this.selectedEmails.has(email);
        if (checked) {
            this.selectedEmails.add(email);
        } else {
            this.selectedEmails.delete(email);
        }
        if (checkbox) checkbox.closest('tr')?.classList.toggle('row-selected', checked);
        // Targeted control refresh: a full table re-render on every checkbox
        // click reset the scroll position and dropped keyboard focus.
        this._syncDetailSelection();
    },

    toggleSelectAll(checkbox) {
        // Scoped to the current page: selectedEmails can hold rows from other
        // pages, so comparing set size to this page's length is meaningless.
        const emails = this._pageEmails();
        if (checkbox.checked) {
            emails.forEach(e => this.selectedEmails.add(e));
        } else {
            emails.forEach(e => this.selectedEmails.delete(e));
        }
        document.querySelectorAll('.unsub-row-select').forEach(box => {
            box.checked = checkbox.checked;
            box.closest('tr')?.classList.toggle('row-selected', checkbox.checked);
        });
        this._syncDetailSelection();
    },

    /** Refresh the selection-dependent controls in place (no re-render). */
    _syncDetailSelection() {
        const count = this.selectedEmails.size;
        const btn = document.getElementById('unsubBulkDeleteBtn');
        const label = document.getElementById('unsubBulkDeleteLabel');
        if (btn && label) {
            btn.disabled = count === 0;
            btn.classList.toggle('btn-danger', count > 0);
            label.textContent = count ? `Delete Selected (${count})` : 'Delete Selected';
        }
        const head = document.getElementById('unsubSelectAllBox');
        if (head) {
            const emails = this._pageEmails();
            const onPage = emails.filter(e => this.selectedEmails.has(e)).length;
            head.checked = emails.length > 0 && onPage === emails.length;
            head.indeterminate = onPage > 0 && onPage < emails.length;
        }
    },

    /* ── Pagination ───────────────────────────────────────── */
    async goToCampaignPage(p) {
        // Bump the generation counter so a slower page fetch that resolves
        // after a newer click cannot paint over it.
        App.bumpSeq();
        const seq = App.seq();
        this.campaignPage = p;
        await this.loadCampaignRecords(this.activeCampaign.campaign_id);
        if (App.stale(seq)) return;
        this.renderDetailView();
    },

    /* ── CRUD Actions ─────────────────────────────────────── */
    /** Query suffix limiting a record delete to the campaign on screen: the
        dialog promises "this campaign's unsubscribe list", and an address
        that unsubscribed from several campaigns must keep the others. */
    _campaignScope() {
        const cid = this.activeCampaign?.campaign_id;
        return Number.isInteger(cid) ? `&campaign_id=${cid}` : '';
    },

    async deleteRecord(email) {
        if (!email) return;
        if (!await App.confirm('Remove Record',
                `Remove "${email}" from this campaign's unsubscribe list?`)) return;
        try {
            await API.del(`/api/unsubscribes/records?emails=${encodeURIComponent(email)}${this._campaignScope()}`);
            App.toast(`Removed ${email}`, 'success');
            this.selectedEmails.delete(email);
            await this.render();
        } catch {
            // API.request already surfaced the server message.
        }
    },

    async bulkDeleteSelected() {
        const count = this.selectedEmails.size;
        if (!count) return;
        if (!await App.confirm('Remove Records',
                `Remove ${count} selected record(s)? This cannot be undone.`)) return;
        try {
            const emailParams = [...this.selectedEmails].map(e => `emails=${encodeURIComponent(e)}`).join('&');
            await API.del(`/api/unsubscribes/records?${emailParams}${this._campaignScope()}`);
            App.toast(`Removed ${count} record(s)`, 'success');
            this.selectedEmails.clear();
            await this.render();
        } catch {
            // API.request already surfaced the server message.
        }
    },

    async removeCampaign(campaignId) {
        if (!await App.confirm('Remove Campaign Records',
                'Remove ALL unsubscribe records for this campaign? This cannot be undone.')) return;
        try {
            const res = await API.del(`/api/unsubscribes/campaign/${campaignId}`);
            App.toast(res.message || 'Campaign removed', 'success');
            this.activeCampaign = null;
            App.selection.clear('unsub_campaigns');
            await this.render();
        } catch {
            // API.request already surfaced the server message.
        }
    },

    /** Delete the log records of every selected campaign group. */
    async bulkDeleteCampaigns() {
        const ids = App.selection.values('unsub_campaigns').map(Number).filter(Boolean);
        if (!ids.length) return;
        if (!await App.confirm('Remove Campaign Records',
                `Remove ALL unsubscribe records for ${ids.length} selected campaign(s)? This cannot be undone.`)) return;
        try {
            const params = ids.map(id => `campaign_ids=${id}`).join('&');
            const res = await API.del(`/api/unsubscribes/campaigns?${params}`);
            App.toast(res.message || 'Campaign records removed', 'success');
            App.selection.clear('unsub_campaigns');
            await this.render();
        } catch {
            // API.request already surfaced the server message.
        }
    },

    /* ── Export ────────────────────────────────────────────── */
    async exportCampaignCSV(campaignId) {
        try {
            const result = await API.download(`/api/unsubscribes/campaign/${campaignId}/export`);
            if (result.blob) {
                API.downloadBlob(result.blob, `unsubscribes_campaign_${campaignId}.csv`);
                App.toast('Campaign exported', 'success');
            }
        } catch {
            // API.request already surfaced the server message.
        }
    },

    async exportAllCSV() {
        try {
            const result = await API.download('/api/unsubscribes/export');
            if (result.blob) {
                API.downloadBlob(result.blob, 'unsubscribes_all.csv');
                App.toast('All unsubscribes exported', 'success');
            }
        } catch {
            // API.request already surfaced the server message (404 when empty).
        }
    },

    /* ── Scan ─────────────────────────────────────────────── */
    async triggerScan() {
        // Disable button while scanning
        const scanBtn = document.querySelector('[onclick="Unsubscribes.triggerScan()"]');
        if (scanBtn) {
            scanBtn.disabled = true;
            scanBtn.innerHTML = `
                <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="14" height="14" style="margin-right:4px;animation:spin 1s linear infinite">
                    <polyline points="23 4 23 10 17 10"/><path d="M20.49 15a9 9 0 1 1-2.12-9.36L23 10"/>
                </svg>
                Scanning...`;
        }
        App.toast('Scanning inbox...', 'info');
        try {
            const result = await API.post('/api/unsubscribes/scan', {}, API.LONG_TIMEOUT);
            const imapData = result.imap || result;  // supports both old and new format
            const linkData = result.link || {};
            const totalProcessed = (imapData.processed || 0) + (linkData.processed || 0);
            const msg = `Scanned ${imapData.scanned || 0} emails + ${linkData.scanned_lists || 0} lists — ${totalProcessed} unsubscribed`;
            App.toast(msg, totalProcessed > 0 ? 'success' : 'info');
            await this.render();
        } catch (err) {
            // API.request already toasted the error; nothing more to do.
        } finally {
            if (scanBtn) {
                scanBtn.disabled = false;
                scanBtn.innerHTML = `
                    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="14" height="14" style="margin-right:4px">
                        <polyline points="23 4 23 10 17 10"/><path d="M20.49 15a9 9 0 1 1-2.12-9.36L23 10"/>
                    </svg>
                    Scan Now`;
            }
        }
    },

    /* ── Settings ─────────────────────────────────────────── */
    async toggleBlocklist(enabled) {
        try {
            await API.put('/api/unsubscribes/settings', { blocklist_enabled: enabled });
            this.settings.blocklist_enabled = enabled;
            App.toast(`Action set to: ${enabled ? 'Unsubscribe + Blocklist' : 'Unsubscribe Only'}`, 'success');
            // Re-render to update the label
            this.renderListView();
        } catch {
            // API.request already surfaced the server message — toasting
            // again here showed the failure twice.
            // Revert toggle
            this.settings.blocklist_enabled = !enabled;
            this.renderListView();
        }
    },

    async resetAll() {
        if (!await App.confirm('Undo All Unsubscribes',
            'This will UNDO all unsubscribes — re-enable and re-subscribe all processed leads in ListMonk, then remove restored records from the log. Continue?')) return;
        App.toast('Resetting all unsubscribes...', 'info');
        try {
            const result = await API.post('/api/unsubscribes/reset', null, API.LONG_TIMEOUT);
            App.toast(result.message || 'Reset complete', 'success');
            await this.render();
        } catch {
            // API.request already surfaced the server message.
        }
    },
};
