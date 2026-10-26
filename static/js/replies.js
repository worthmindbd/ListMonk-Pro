/**
 * Replies & Prospect Feed for ListMonk Pro Cold Outreach.
 * Real-time IMAP reply monitoring, sentiment filtering, and lead tracking.
 */
const Replies = {
    page: 1,
    perPage: 20,
    sentiment: '',
    status: '',
    campaignId: 0,
    query: '',
    stats: null,
    repliesData: null,
    isScanning: false,
    // Auto-blocklist toggles (reply settings); loaded with the page data.
    settings: { auto_blocklist_machine: false, auto_blocklist_ooo: false },

    async render() {
        // An action callback (status update, bulk op) can fire after the user
        // navigated away; never repaint over another page.
        if (App.currentPage !== 'replies') return;
        this._seq = App.seq();
        // Deep-linkable state: #/replies?page=2&sentiment=positive&campaign=6
        const rq = App.routeQuery();
        if (rq.get('page')) this.page = Math.max(1, parseInt(rq.get('page'), 10) || 1);
        if (rq.get('q') !== null) this.query = rq.get('q') || '';
        if (rq.get('sentiment') !== null) this.sentiment = rq.get('sentiment') || '';
        if (rq.get('status') !== null) this.status = rq.get('status') || '';
        if (rq.get('campaign') !== null) this.campaignId = parseInt(rq.get('campaign'), 10) || 0;
        App.setContent('<div class="loading-spinner">Loading prospect replies…</div>');

        try {
            await Promise.all([
                this.loadStats(),
                this.loadReplies(),
                this.loadSettings(),
            ]);
            this.renderPage();
        } catch (err) {
            console.error('Replies load error:', err);
            if (App.stale(this._seq)) return;
            App.setContent(`<div class="empty-state"><h2>Failed to load replies</h2><p>${App.escapeHtml(err?.message || '')}</p></div>`);
        }
    },

    async loadSettings() {
        try {
            const s = await API.get('/api/replies/settings');
            this.settings = {
                auto_blocklist_machine: !!s.auto_blocklist_machine,
                auto_blocklist_ooo: !!s.auto_blocklist_ooo,
            };
        } catch {
            // Defaults keep the toggles renderable without a settings file.
        }
    },

    async toggleAutoBlocklist(key, enabled) {
        try {
            await API.put('/api/replies/settings', { [key]: enabled });
            this.settings[key] = enabled;
            App.toast(`Auto-blocklist ${enabled ? 'enabled' : 'disabled'}`, 'success');
        } catch {
            // API.request already surfaced the server message — toasting
            // again here showed the failure twice. Revert the checkbox.
            this.settings[key] = !enabled;
        }
        if (App.currentPage === 'replies') this.renderPage();
    },

    async loadStats() {
        try {
            const params = new URLSearchParams();
            if (this.campaignId) params.append('campaign_id', this.campaignId);
            this.stats = await API.get(`/api/replies/stats?${params.toString()}`);
        } catch (e) {
            this.stats = { total_replies: 0, positive: 0, auto_reply: 0, negative: 0, reply_rate: 0, campaign_options: [] };
        }
    },

    async loadReplies() {
        const params = new URLSearchParams({
            page: this.page,
            per_page: this.perPage,
        });
        if (this.sentiment) params.append('sentiment', this.sentiment);
        if (this.status) params.append('status', this.status);
        if (this.campaignId) params.append('campaign_id', this.campaignId);
        if (this.query) params.append('q', this.query);

        this.repliesData = await API.get(`/api/replies?${params.toString()}`);
    },

    renderPage() {
        if (this._seq !== undefined && App.stale(this._seq)) return;
        const s = this.stats || {};
        const replies = this.repliesData?.results || [];
        const total = this.repliesData?.total || 0;

        App.selection.register('replies', {
            noun: 'reply',
            pageValues: this.pageIds(),
            actions: [
                { label: 'Mark Hot Lead', onclick: 'Replies.bulkStatus(\'lead\')' },
                { label: 'Mark Replied', onclick: 'Replies.bulkStatus(\'replied\')' },
                { label: 'Archive', onclick: 'Replies.bulkStatus(\'archived\')' },
                { label: 'Mark Unread', onclick: 'Replies.bulkStatus(\'unread\')' },
                { label: 'Blocklist', onclick: 'Replies.bulkBlocklist()' },
                { label: 'Delete', onclick: 'Replies.bulkDelete()', danger: true },
            ],
        });

        let html = `
            <!-- Cold Email Reply KPI Cards -->
            <div class="stats-grid replies-stats-grid">
                <div class="stat-card accent">
                    <div class="stat-label">Total Replies</div>
                    <div class="stat-value">${App.formatNumber(s.total_replies || 0)}</div>
                    <div style="font-size:0.82rem;color:var(--text-muted)">Human Reply Rate: <strong style="color:var(--accent)">${s.reply_rate || 0}%</strong> <span style="opacity:0.7">(excl. ${App.formatNumber(s.auto_reply || 0)} automated)</span></div>
                </div>
                <div class="stat-card success">
                    <div class="stat-label"><span class="badge-dot badge-dot-positive"></span> Positive / Hot Leads</div>
                    <div class="stat-value" style="color:var(--emerald)">${App.formatNumber(s.positive || 0)}</div>
                    <div style="font-size:0.82rem;color:var(--text-muted)">Interested & call requests</div>
                </div>
                <div class="stat-card">
                    <div class="stat-label"><span class="badge-dot badge-dot-neutral"></span> Neutral / No Signal</div>
                    <div class="stat-value">${App.formatNumber(s.neutral || 0)}</div>
                    <div style="font-size:0.82rem;color:var(--text-muted)">Ambiguous replies, no clear intent</div>
                </div>
                <div class="stat-card">
                    <div class="stat-label"><span class="badge-dot badge-dot-auto-reply"></span> Automated / Auto-Reply</div>
                    <div class="stat-value">${App.formatNumber(s.auto_reply || 0)}</div>
                    <div style="font-size:0.82rem;color:var(--text-muted)">OOO, ticket confirmations, auto-responses</div>
                </div>
                <div class="stat-card danger">
                    <div class="stat-label"><span class="badge-dot badge-dot-negative"></span> Not Interested</div>
                    <div class="stat-value" style="color:var(--rose)">${App.formatNumber(s.negative || 0)}</div>
                    <div style="font-size:0.82rem;color:var(--text-muted)">Unsubscribe / rejections</div>
                </div>
            </div>

            <!-- Toolbar & Controls -->
            <div class="card" style="margin-top:20px;padding:16px 20px;">
                <div class="replies-toolbar">
                    <div class="search-input-wrap">
                        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="16" height="16"><circle cx="11" cy="11" r="8"/><line x1="21" y1="21" x2="16.65" y2="16.65"/></svg>
                        <input type="text" id="repliesSearchInput" class="form-control" placeholder="Search sender, email, subject, body…" value="${App.escapeHtml(this.query)}" onkeydown="if(event.key==='Enter')Replies.handleSearch(this.value)">
                    </div>

                    <div class="replies-filters">
                        <span class="reply-card-select" title="Select every reply on this page">
                            ${App.selection.headbox('replies', 'Select all replies on this page')}
                            <label for="selectAll_replies">Select page</label>
                        </span>

                        <select class="form-control filter-select" id="sentimentFilter" aria-label="Filter replies by sentiment" onchange="Replies.filterSentiment(this.value)">
                            <option value="" ${!this.sentiment ? 'selected' : ''}>All Sentiments</option>
                            <option value="positive" ${this.sentiment === 'positive' ? 'selected' : ''}>Positive / Interested</option>
                            <option value="neutral" ${this.sentiment === 'neutral' ? 'selected' : ''}>Neutral / No Signal</option>
                            <option value="auto_reply" ${this.sentiment === 'auto_reply' ? 'selected' : ''}>Automated / Auto-Reply</option>
                            <option value="negative" ${this.sentiment === 'negative' ? 'selected' : ''}>Not Interested</option>
                        </select>

                        <select class="form-control filter-select" id="statusFilter" aria-label="Filter replies by status" onchange="Replies.filterStatus(this.value)">
                            <option value="" ${!this.status ? 'selected' : ''}>All Statuses</option>
                            <option value="unread" ${this.status === 'unread' ? 'selected' : ''}>Unread</option>
                            <option value="lead" ${this.status === 'lead' ? 'selected' : ''}>Marked Lead</option>
                            <option value="replied" ${this.status === 'replied' ? 'selected' : ''}>Replied</option>
                            <option value="archived" ${this.status === 'archived' ? 'selected' : ''}>Archived</option>
                        </select>

                        <select class="form-control filter-select" id="campaignFilter" aria-label="Filter replies by campaign" onchange="Replies.filterCampaign(Number(this.value))">
                            <option value="0" ${!this.campaignId ? 'selected' : ''}>All Campaigns</option>
                            ${(s.campaign_options || []).map(c => `<option value="${c.id}" ${this.campaignId === c.id ? 'selected' : ''}>${App.escapeHtml(c.name)} (${c.count})</option>`).join('')}
                        </select>

                        <button class="btn btn-primary scan-btn" id="btnScanInbox" onclick="Replies.scanInbox()" ${this.isScanning ? 'disabled' : ''}>
                            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="15" height="15"><polyline points="23 4 23 10 17 10"/><path d="M20.49 15a9 9 0 1 1-2.12-9.36L23 10"/></svg>
                            <span>${this.isScanning ? 'Scanning Mailbox…' : 'Scan Mailbox Now'}</span>
                        </button>

                        <label class="auto-blocklist-toggle" title="Automatically blocklist senders of machine auto-replies (noreply@, notifications@, ticket systems) when a scan detects them">
                            <input type="checkbox" ${this.settings.auto_blocklist_machine ? 'checked' : ''} aria-label="Auto-blocklist machine auto-replies" onchange="Replies.toggleAutoBlocklist('auto_blocklist_machine', this.checked)">
                            Auto-blocklist no-reply
                        </label>
                        <label class="auto-blocklist-toggle" title="Also blocklist out-of-office senders. Careful: these are real people who are temporarily away — they may be worth following up with later.">
                            <input type="checkbox" ${this.settings.auto_blocklist_ooo ? 'checked' : ''} aria-label="Auto-blocklist out-of-office replies" onchange="Replies.toggleAutoBlocklist('auto_blocklist_ooo', this.checked)">
                            Auto-blocklist OOO
                        </label>
                    </div>
                </div>
            </div>

            <!-- Selection bar (appears only with a selection) -->
            ${App.selection.bar('replies')}

            <!-- Prospect Replies Feed -->
            <div class="replies-feed" id="repliesFeed" style="margin-top:20px;">
        `;

        if (!replies.length) {
            html += `
                <div class="empty-state card" style="padding:48px 24px;text-align:center;">
                    <div style="display:inline-flex;align-items:center;justify-content:center;width:64px;height:64px;border-radius:16px;background:rgba(99,102,241,0.12);color:var(--primary);margin-bottom:16px">
                        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" width="32" height="32"><path d="M4 4h16c1.1 0 2 .9 2 2v12c0 1.1-.9 2-2 2H4c-1.1 0-2-.9-2-2V6c0-1.1.9-2 2-2z"/><polyline points="22,6 12,13 2,6"/></svg>
                    </div>
                    <h2>No Prospect Replies Found</h2>
                    <p style="color:var(--text-secondary);max-width:500px;margin:0 auto 16px auto;">
                        ${this.query || this.sentiment || this.status || this.campaignId ? 'No replies match your current filters. Try resetting the filters.' : 'Your prospect replies will appear here once leads respond to your cold outreach. Click "Scan Mailbox Now" to check IMAP for new incoming messages.'}
                    </p>
                    <button class="btn btn-primary" onclick="Replies.scanInbox()">Scan Mailbox Now</button>
                </div>
            `;
        } else {
            replies.forEach(r => {
                html += this.renderReplyCard(r);
            });
        }

        html += `</div>`;

        // Pagination
        if (total > this.perPage) {
            html += App.renderPagination(this.page, total, this.perPage, 'Replies.goToPage');
        }

        // A bulk delete can strand the view past the last page; clamp and
        // re-fetch instead of painting a false "no replies" feed.
        const clamped = App.clampPage(this.page, total, this.perPage);
        if (total > 0 && clamped !== this.page) { this.page = clamped; this.render(); return; }

        App.syncUrl('replies', {
            page: this.page, q: this.query, sentiment: this.sentiment,
            status: this.status, campaign: this.campaignId,
        });
        App.setContent(html);
    },

    renderReplyCard(r) {
        const sentimentConfig = {
            'positive': { label: 'Interested / Lead', colorClass: 'badge-positive', dotClass: 'badge-dot-positive' },
            'neutral': { label: 'Neutral / No Signal', colorClass: 'badge-neutral', dotClass: 'badge-dot-neutral' },
            'auto_reply': { label: 'Automated', colorClass: 'badge-auto-reply', dotClass: 'badge-dot-auto-reply' },
            'negative': { label: 'Not Interested', colorClass: 'badge-negative', dotClass: 'badge-dot-negative' },
        };

        const sent = sentimentConfig[r.sentiment] || { label: r.sentiment || 'Reply', colorClass: 'badge-default', dotClass: 'badge-dot-auto-reply' };
        const initial = (r.from_name || r.from_email || '?').charAt(0).toUpperCase();
        // Attribution is defined by campaign_id, never by campaign_name alone:
        // records written by older versions carry a fabricated
        // "Cold Outreach Campaign" label for a reply that matched nothing, and
        // a test mail must not look like it belonged to a campaign.
        const campLabel = r.campaign_id
            ? (r.campaign_name || `Campaign #${r.campaign_id}`)
            : 'Unattributed';
        const campClass = r.campaign_id ? 'campaign-tag' : 'campaign-tag campaign-tag-unattributed';
        const campTitle = r.campaign_id
            ? `Campaign: ${campLabel}`
            : 'No campaign matched this reply — it was not sent from a tracked campaign.';

        return `
            <div class="card reply-card ${r.status === 'lead' ? 'lead-highlight' : ''} ${App.selection.has('replies', r.id) ? 'selected' : ''}" id="card_${r.id}">
                <div class="reply-header">
                    <div class="reply-sender-info">
                        <label class="reply-card-select" title="Select this reply">
                            <input type="checkbox" class="row-select" data-key="replies" data-value="${App.escapeHtml(String(r.id))}"
                                aria-label="Select reply from ${App.escapeHtml(r.from_email || 'sender')}"
                                ${App.selection.has('replies', r.id) ? 'checked' : ''}
                                onchange="Replies.toggleSelect(${App.escapeHtml(JSON.stringify(String(r.id)))}, this)"/>
                        </label>
                        <div class="reply-avatar">${initial}</div>
                        <div>
                            <div class="reply-sender-name">
                                <strong>${App.escapeHtml(r.from_name || r.from_email)}</strong>
                                <span class="reply-sender-email">&lt;${App.escapeHtml(r.from_email)}&gt;</span>
                            </div>
                            <div class="reply-meta">
                                <span class="${campClass}" title="${App.escapeHtml(campTitle)}">${App.escapeHtml(campLabel)}</span>
                                <span class="reply-date">${App.formatDateTime(r.date)}</span>
                            </div>
                        </div>
                    </div>

                    <div class="reply-header-actions">
                        ${r.auto_blocklisted ? `<span class="sentiment-badge badge-negative" title="Sender was automatically blocklisted in ListMonk when this automated reply was detected">⛔ Auto-Blocklisted</span>` : ''}
                        <span class="sentiment-badge ${sent.colorClass}">
                            <span class="badge-dot ${sent.dotClass}"></span>${App.escapeHtml(sent.label)}
                        </span>
                        <div class="status-select-wrap">
                            <select class="form-control form-control-sm" aria-label="Set status for ${App.escapeHtml(r.from_email || 'this reply')}" onchange="Replies.updateStatus(${App.escapeHtml(JSON.stringify(String(r.id)))}, this.value)">
                                <option value="unread" ${r.status === 'unread' ? 'selected' : ''}>Unread</option>
                                <option value="lead" ${r.status === 'lead' ? 'selected' : ''}>Hot Lead</option>
                                <option value="replied" ${r.status === 'replied' ? 'selected' : ''}>Replied</option>
                                <option value="archived" ${r.status === 'archived' ? 'selected' : ''}>Archived</option>
                            </select>
                        </div>
                    </div>
                </div>

                <div class="reply-subject">
                    <strong>Subject:</strong> ${App.escapeHtml(r.subject || '(No Subject)')}
                </div>

                <div class="reply-body-container">
                    <div class="reply-body-text" id="body_${r.id}">
                        ${App.escapeHtml(r.full_body || r.body_preview || '(Empty reply)')}
                    </div>
                </div>

                <div class="reply-actions">
                    <button class="btn btn-sm ${r.status === 'lead' ? 'btn-success' : 'btn-secondary'}" onclick="Replies.toggleLead(${App.escapeHtml(JSON.stringify(String(r.id)))}, ${App.escapeHtml(JSON.stringify(String(r.status || '')))})">
                        <svg viewBox="0 0 24 24" fill="${r.status === 'lead' ? 'currentColor' : 'none'}" stroke="currentColor" stroke-width="2" width="13" height="13" style="vertical-align:-1px;margin-right:4px;"><polygon points="12 2 15.09 8.26 22 9.27 17 14.14 18.18 21.02 12 17.77 5.82 21.02 7 14.14 2 9.27 8.91 8.26 12 2"/></svg>
                        ${r.status === 'lead' ? 'Hot Lead Flagged' : 'Mark as Hot Lead'}
                    </button>
                    <button class="btn btn-sm btn-secondary" onclick="Replies.copyEmail(${App.escapeHtml(JSON.stringify(r.from_email || ''))})">
                        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="13" height="13" style="vertical-align:-1px;margin-right:4px;"><rect x="9" y="9" width="13" height="13" rx="2" ry="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/></svg>
                        Copy Email
                    </button>
                    <button class="btn btn-sm btn-secondary" onclick="Replies.viewFullThread(${App.escapeHtml(JSON.stringify(String(r.id)))})">
                        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="13" height="13" style="vertical-align:-1px;margin-right:4px;"><circle cx="11" cy="11" r="8"/><line x1="21" y1="21" x2="16.65" y2="16.65"/></svg>
                        Full Details
                    </button>
                    <button class="btn btn-sm btn-danger-outline" onclick="Replies.unsubscribeLead(${App.escapeHtml(JSON.stringify(String(r.id)))})">
                        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="13" height="13" style="vertical-align:-1px;margin-right:4px;"><circle cx="12" cy="12" r="10"/><line x1="4.93" y1="4.93" x2="19.07" y2="19.07"/></svg>
                        Blocklist / Unsubscribe
                    </button>
                    <button class="btn btn-sm btn-danger" onclick="Replies.remove(${App.escapeHtml(JSON.stringify(String(r.id)))})">
                        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="13" height="13" style="vertical-align:-1px;margin-right:4px;"><polyline points="3 6 5 6 21 6"/><path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/></svg>
                        Delete
                    </button>
                </div>
            </div>
        `;
    },

    /** IDs (as strings) of the reply cards currently on screen. */
    pageIds() {
        return (this.repliesData?.results || []).map(r => String(r.id));
    },

    _selectedIds() {
        return App.selection.values('replies');
    },

    /** Card checkbox handler: keeps the card highlight in sync. */
    toggleSelect(id, checkbox) {
        App.selection.rowToggle('replies', id, checkbox);
        document.getElementById(`card_${id}`)?.classList.toggle('selected', checkbox.checked);
    },

    // ── Bulk actions ─────────────────────────────────────
    async bulkStatus(newStatus) {
        const ids = this._selectedIds();
        if (!ids.length) return;
        try {
            const result = await API.put('/api/replies/status', { ids, status: newStatus });
            App.toast(`${result?.updated ?? ids.length} reply(ies) marked ${newStatus}`, 'success');
            App.selection.clear('replies');
            await Promise.all([this.loadStats(), this.loadReplies()]);
            this.renderPage();
        } catch {
            // API.request already surfaced the server message.
        }
    },

    async bulkBlocklist() {
        const ids = this._selectedIds();
        if (!ids.length) return;
        if (!await App.confirm('Blocklist Senders',
                `Blocklist the senders of ${ids.length} selected repl(y/ies)? They will never receive further outreach.`)) return;
        try {
            const result = await API.post('/api/replies/blocklist', { ids }, API.LONG_TIMEOUT);
            const errors = result?.errors || 0;
            App.toast(`Blocklisted ${result?.blocklisted ?? ids.length} sender(s)${errors ? `, ${errors} failed` : ''}`,
                errors ? 'info' : 'success');
            App.selection.clear('replies');
            this.render();
        } catch {
            // API.request already surfaced the server message.
        }
    },

    async bulkDelete() {
        const ids = this._selectedIds();
        if (!ids.length) return;
        if (!await App.confirm('Delete Replies',
                `Delete ${ids.length} selected reply record(s) from the log?`)) return;
        try {
            const params = ids.map(id => `ids=${encodeURIComponent(id)}`).join('&');
            await API.del(`/api/replies?${params}`);
            App.toast(`${ids.length} reply(ies) deleted`, 'success');
            App.selection.clear('replies');
            await Promise.all([this.loadStats(), this.loadReplies()]);
            this.renderPage();
        } catch {
            // API.request already surfaced the server message.
        }
    },

    async remove(id) {
        if (!await App.confirm('Delete Reply', 'Delete this reply record from the log?')) return;
        try {
            await API.del(`/api/replies/${encodeURIComponent(id)}`);
            App.toast('Reply deleted', 'success');
            App.selection.clear('replies');
            await Promise.all([this.loadStats(), this.loadReplies()]);
            this.renderPage();
        } catch {
            // API.request already surfaced the server message.
        }
    },

    async scanInbox() {
        if (this.isScanning) return;
        this.isScanning = true;
        const btn = document.getElementById('btnScanInbox');
        const btnHtml = btn ? btn.innerHTML : '';
        if (btn) {
            btn.disabled = true;
            btn.innerHTML = `<span class="spinner-sm"></span> Scanning Mailbox…`;
        }

        try {
            const res = await API.post('/api/replies/scan', {}, API.LONG_TIMEOUT);
            const stats = res.stats || {};
            // Safety net: the endpoint now answers 502 on IMAP failure, but a
            // 200 carrying an error must never read as a successful scan.
            if (stats.error) {
                App.toast(`Scan failed: ${stats.error}`, 'error');
            } else {
                const count = stats.new_replies || 0;
                const repaired = stats.repaired || 0;
                let msg = `Scan complete! Found ${count} new prospect reply(ies).`;
                if (repaired) {
                    msg += ` Recovered the body of ${repaired} empty reply(ies).`;
                }
                App.toast(msg, (count > 0 || repaired > 0) ? 'success' : 'info');
            }
            await Promise.all([this.loadStats(), this.loadReplies()]);
            this.renderPage();
        } catch (err) {
            console.error('Scan error:', err);
            // API.request already surfaced the server message.
        } finally {
            this.isScanning = false;
            // Restore the button even when the scan failed, so it does not
            // stay stuck on "Scanning Mailbox…" forever.
            const b = document.getElementById('btnScanInbox');
            if (b && btnHtml) { b.disabled = false; b.innerHTML = btnHtml; }
        }
    },

    async updateStatus(id, newStatus) {
        try {
            await API.put(`/api/replies/${encodeURIComponent(id)}/status`, { status: newStatus });
            App.toast(`Status updated to ${newStatus}`, 'success');
            const card = document.getElementById(`card_${id}`);
            if (card) {
                card.classList.toggle('lead-highlight', newStatus === 'lead');
            }
        } catch {
            // API.request already surfaced the server message; re-render so the
            // dropdown reflects the state the server actually stored.
            await this.render();
        }
    },

    async toggleLead(id, currentStatus) {
        const next = currentStatus === 'lead' ? 'unread' : 'lead';
        try {
            await API.put(`/api/replies/${encodeURIComponent(id)}/status`, { status: next });
            App.toast(`Marked as ${next}`, 'success');
            // Reload data in parallel instead of full render() which chains 3 calls.
            await Promise.all([this.loadStats(), this.loadReplies()]);
            this.renderPage();
        } catch {
            // API.request already surfaced the server message.
        }
    },

    copyEmail(email) {
        const done = () => App.toast(`Copied ${email} to clipboard`, 'success');
        const fail = () => App.toast(`Could not copy email`, 'error');

        if (navigator.clipboard?.writeText) {
            navigator.clipboard.writeText(email).then(done).catch(() => {
                // Clipboard API exists but was refused (insecure context,
                // missing permission): fall back to the legacy path.
                this._legacyCopy(email) ? done() : fail();
            });
            return;
        }
        this._legacyCopy(email) ? done() : fail();
    },

    /** Pre-Clipboard-API copy path (works on plain-HTTP dev instances). */
    _legacyCopy(text) {
        const ta = document.createElement('textarea');
        ta.value = text;
        ta.setAttribute('readonly', '');
        ta.style.position = 'fixed';
        ta.style.top = '-1000px';
        document.body.appendChild(ta);
        try {
            ta.select();
            return document.execCommand('copy');
        } catch {
            return false;
        } finally {
            document.body.removeChild(ta);
        }
    },

    viewFullThread(id) {
        const reply = (this.repliesData?.results || []).find(r => r.id === id);
        if (!reply) return;

        // showResult sets the title via textContent — escaping here would
        // show the entities literally.
        App.showResult(`Reply Details: ${reply.from_name || reply.from_email}`, `
            <div style="font-size:0.92rem;line-height:1.6">
                <p><strong>From:</strong> ${App.escapeHtml(reply.from_name || '')} &lt;${App.escapeHtml(reply.from_email)}&gt;</p>
                <p><strong>Subject:</strong> ${App.escapeHtml(reply.subject || '')}</p>
                <p><strong>Campaign:</strong> ${App.escapeHtml(reply.campaign_id ? (reply.campaign_name || `Campaign #${reply.campaign_id}`) : 'Unattributed (no campaign match)')}</p>
                <p><strong>Sentiment:</strong> <span class="badge">${App.escapeHtml(reply.sentiment)}</span></p>
                <p><strong>Date:</strong> ${App.escapeHtml(reply.date || '')}</p>
                <hr style="border:none;border-top:1px solid var(--border-color);margin:16px 0;">
                <div style="background:var(--bg-input);padding:14px;border-radius:8px;font-family:monospace;white-space:pre-wrap;word-break:break-word;">
${App.escapeHtml(reply.full_body || reply.body_preview || '')}
                </div>
            </div>
        `);
    },

    async unsubscribeLead(id) {
        const reply = (this.repliesData?.results || []).find(r => r.id === id);
        const email = reply?.from_email || 'this sender';
        const ok = await App.confirm(
            'Blocklist & Unsubscribe',
            `Are you sure you want to blocklist and unsubscribe ${email}? They will never receive any further outreach.`
        );
        if (!ok) return;

        try {
            // Resolved server-side so the sender address never reaches a
            // client-built ListMonk query.
            const result = await API.post(`/api/replies/${encodeURIComponent(id)}/blocklist`, {});
            App.toast(`${result?.email || email} successfully blocklisted.`, 'success');
        } catch {
            // API.request already surfaced the server message.
        }
    },

    handleSearch(val) {
        this.query = val.trim();
        this.page = 1;
        App.bumpSeq();
        this.render();
    },

    filterSentiment(val) {
        this.sentiment = val;
        this.page = 1;
        App.bumpSeq();
        this.render();
    },

    filterStatus(val) {
        this.status = val;
        this.page = 1;
        App.bumpSeq();
        this.render();
    },

    filterCampaign(val) {
        this.campaignId = val || 0;
        this.page = 1;
        App.bumpSeq();
        this.render();
    },

    goToPage(p) {
        this.page = p;
        App.bumpSeq();
        this.render();
    }
};
