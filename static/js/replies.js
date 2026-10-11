/**
 * Replies & Prospect Feed for ListMonk Pro Cold Outreach.
 * Real-time IMAP reply monitoring, sentiment filtering, and lead tracking.
 */
const Replies = {
    page: 1,
    perPage: 20,
    sentiment: '',
    status: '',
    query: '',
    stats: null,
    repliesData: null,
    isScanning: false,

    async render() {
        App.setContent('<div class="loading-spinner">Loading prospect replies...</div>');

        try {
            await Promise.all([
                this.loadStats(),
                this.loadReplies(),
            ]);
            this.renderPage();
        } catch (err) {
            console.error('Replies load error:', err);
            App.setContent(`<div class="empty-state"><h3>Failed to load replies</h3><p>${App.escapeHtml(err?.message || '')}</p></div>`);
        }
    },

    async loadStats() {
        try {
            this.stats = await API.get('/api/replies/stats');
        } catch (e) {
            this.stats = { total_replies: 0, positive: 0, question: 0, auto_reply: 0, negative: 0, reply_rate: 0 };
        }
    },

    async loadReplies() {
        const params = new URLSearchParams({
            page: this.page,
            per_page: this.perPage,
        });
        if (this.sentiment) params.append('sentiment', this.sentiment);
        if (this.status) params.append('status', this.status);
        if (this.query) params.append('q', this.query);

        this.repliesData = await API.get(`/api/replies?${params.toString()}`);
    },

    renderPage() {
        const s = this.stats || {};
        const replies = this.repliesData?.results || [];
        const total = this.repliesData?.total || 0;

        let html = `
            <!-- Cold Email Reply KPI Cards -->
            <div class="stats-grid replies-stats-grid">
                <div class="stat-card accent">
                    <div class="stat-label">Total Replies</div>
                    <div class="stat-value">${App.formatNumber(s.total_replies || 0)}</div>
                    <div style="font-size:0.82rem;color:var(--text-muted)">Overall Reply Rate: <strong style="color:var(--accent)">${s.reply_rate || 0}%</strong></div>
                </div>
                <div class="stat-card success">
                    <div class="stat-label">🟢 Positive / Hot Leads</div>
                    <div class="stat-value" style="color:var(--emerald)">${App.formatNumber(s.positive || 0)}</div>
                    <div style="font-size:0.82rem;color:var(--text-muted)">Interested & call requests</div>
                </div>
                <div class="stat-card warning">
                    <div class="stat-label">🟡 Questions & Inquiries</div>
                    <div class="stat-value" style="color:var(--amber)">${App.formatNumber(s.question || 0)}</div>
                    <div style="font-size:0.82rem;color:var(--text-muted)">Clarifications & pricing</div>
                </div>
                <div class="stat-card">
                    <div class="stat-label">⚪ Out of Office / Auto-Reply</div>
                    <div class="stat-value">${App.formatNumber(s.auto_reply || 0)}</div>
                    <div style="font-size:0.82rem;color:var(--text-muted)">Automated OOO notices</div>
                </div>
                <div class="stat-card danger">
                    <div class="stat-label">🔴 Not Interested</div>
                    <div class="stat-value" style="color:var(--rose)">${App.formatNumber(s.negative || 0)}</div>
                    <div style="font-size:0.82rem;color:var(--text-muted)">Unsubscribe / rejections</div>
                </div>
            </div>

            <!-- Toolbar & Controls -->
            <div class="card" style="margin-top:20px;padding:16px 20px;">
                <div class="replies-toolbar">
                    <div class="search-input-wrap">
                        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="16" height="16"><circle cx="11" cy="11" r="8"/><line x1="21" y1="21" x2="16.65" y2="16.65"/></svg>
                        <input type="text" id="repliesSearchInput" class="form-control" placeholder="Search sender, email, subject, body..." value="${App.escapeHtml(this.query)}" onkeydown="if(event.key==='Enter')Replies.handleSearch(this.value)">
                    </div>

                    <div class="replies-filters">
                        <select class="form-control filter-select" id="sentimentFilter" onchange="Replies.filterSentiment(this.value)">
                            <option value="" ${!this.sentiment ? 'selected' : ''}>All Sentiments</option>
                            <option value="positive" ${this.sentiment === 'positive' ? 'selected' : ''}>🟢 Positive / Interested</option>
                            <option value="question" ${this.sentiment === 'question' ? 'selected' : ''}>🟡 Question</option>
                            <option value="auto_reply" ${this.sentiment === 'auto_reply' ? 'selected' : ''}>⚪ Out of Office</option>
                            <option value="negative" ${this.sentiment === 'negative' ? 'selected' : ''}>🔴 Not Interested</option>
                        </select>

                        <select class="form-control filter-select" id="statusFilter" onchange="Replies.filterStatus(this.value)">
                            <option value="" ${!this.status ? 'selected' : ''}>All Statuses</option>
                            <option value="unread" ${this.status === 'unread' ? 'selected' : ''}>Unread</option>
                            <option value="lead" ${this.status === 'lead' ? 'selected' : ''}>Marked Lead</option>
                            <option value="replied" ${this.status === 'replied' ? 'selected' : ''}>Replied</option>
                            <option value="archived" ${this.status === 'archived' ? 'selected' : ''}>Archived</option>
                        </select>

                        <button class="btn btn-primary scan-btn" id="btnScanInbox" onclick="Replies.scanInbox()" ${this.isScanning ? 'disabled' : ''}>
                            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="15" height="15"><polyline points="23 4 23 10 17 10"/><path d="M20.49 15a9 9 0 1 1-2.12-9.36L23 10"/></svg>
                            <span>${this.isScanning ? 'Scanning Mailbox...' : 'Scan Mailbox Now'}</span>
                        </button>
                    </div>
                </div>
            </div>

            <!-- Prospect Replies Feed -->
            <div class="replies-feed" id="repliesFeed" style="margin-top:20px;">
        `;

        if (!replies.length) {
            html += `
                <div class="empty-state card" style="padding:48px 24px;text-align:center;">
                    <div style="font-size:2.8rem;margin-bottom:12px">📬</div>
                    <h3>No Prospect Replies Found</h3>
                    <p style="color:var(--text-secondary);max-width:500px;margin:0 auto 16px auto;">
                        ${this.query || this.sentiment || this.status ? 'No replies match your current filters. Try resetting the filters.' : 'Your prospect replies will appear here once leads respond to your cold outreach. Click "Scan Mailbox Now" to check IMAP for new incoming messages.'}
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

        App.setContent(html);
    },

    renderReplyCard(r) {
        const sentimentConfig = {
            'positive': { label: 'Interested / Lead', colorClass: 'badge-positive', dot: '🟢' },
            'question': { label: 'Question', colorClass: 'badge-question', dot: '🟡' },
            'auto_reply': { label: 'Out of Office', colorClass: 'badge-auto-reply', dot: '⚪' },
            'negative': { label: 'Not Interested', colorClass: 'badge-negative', dot: '🔴' },
        };

        const sent = sentimentConfig[r.sentiment] || { label: r.sentiment || 'Reply', colorClass: 'badge-default', dot: '✉️' };
        const initial = (r.from_name || r.from_email || '?').charAt(0).toUpperCase();

        return `
            <div class="card reply-card ${r.status === 'lead' ? 'lead-highlight' : ''}" id="card_${r.id}">
                <div class="reply-header">
                    <div class="reply-sender-info">
                        <div class="reply-avatar">${initial}</div>
                        <div>
                            <div class="reply-sender-name">
                                <strong>${App.escapeHtml(r.from_name || r.from_email)}</strong>
                                <span class="reply-sender-email">&lt;${App.escapeHtml(r.from_email)}&gt;</span>
                            </div>
                            <div class="reply-meta">
                                <span class="campaign-tag">${App.escapeHtml(r.campaign_name || 'Cold Outreach')}</span>
                                <span class="reply-date">${App.escapeHtml(r.date || '')}</span>
                            </div>
                        </div>
                    </div>

                    <div class="reply-header-actions">
                        <span class="sentiment-badge ${sent.colorClass}">
                            ${sent.dot} ${sent.label}
                        </span>
                        <div class="status-select-wrap">
                            <select class="form-control form-control-sm" onchange="Replies.updateStatus('${r.id}', this.value)">
                                <option value="unread" ${r.status === 'unread' ? 'selected' : ''}>Unread</option>
                                <option value="lead" ${r.status === 'lead' ? 'selected' : ''}>⭐ Hot Lead</option>
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
                    <button class="btn btn-sm ${r.status === 'lead' ? 'btn-success' : 'btn-secondary'}" onclick="Replies.toggleLead('${r.id}', '${r.status}')">
                        ${r.status === 'lead' ? '★ Hot Lead Flagged' : '☆ Mark as Hot Lead'}
                    </button>
                    <button class="btn btn-sm btn-secondary" onclick="Replies.copyEmail('${App.escapeHtml(r.from_email)}')">
                        📋 Copy Email
                    </button>
                    <button class="btn btn-sm btn-secondary" onclick="Replies.viewFullThread('${r.id}')">
                        🔍 Full Details
                    </button>
                    <button class="btn btn-sm btn-danger-outline" onclick="Replies.unsubscribeLead('${App.escapeHtml(r.from_email)}')">
                        🚫 Blocklist / Unsubscribe
                    </button>
                </div>
            </div>
        `;
    },

    async scanInbox() {
        if (this.isScanning) return;
        this.isScanning = true;
        const btn = document.getElementById('btnScanInbox');
        if (btn) {
            btn.disabled = true;
            btn.innerHTML = `<span class="spinner-sm"></span> Scanning Mailbox...`;
        }

        try {
            const res = await API.post('/api/replies/scan', {});
            const stats = res.stats || {};
            const count = stats.new_replies || 0;
            App.toast(`Scan complete! Found ${count} new prospect reply(ies).`, count > 0 ? 'success' : 'info');
            await Promise.all([this.loadStats(), this.loadReplies()]);
            this.renderPage();
        } catch (err) {
            console.error('Scan error:', err);
            App.toast(`Scan failed: ${err?.message || 'Check IMAP settings in .env'}`, 'danger');
        } finally {
            this.isScanning = false;
        }
    },

    async updateStatus(id, newStatus) {
        try {
            await API.put(`/api/replies/${id}/status`, { status: newStatus });
            App.toast(`Status updated to ${newStatus}`, 'success');
            const card = document.getElementById(`card_${id}`);
            if (card) {
                card.classList.toggle('lead-highlight', newStatus === 'lead');
            }
        } catch (err) {
            App.toast(`Failed to update status: ${err?.message}`, 'danger');
        }
    },

    async toggleLead(id, currentStatus) {
        const next = currentStatus === 'lead' ? 'unread' : 'lead';
        await this.updateStatus(id, next);
        await this.render();
    },

    copyEmail(email) {
        navigator.clipboard.writeText(email).then(() => {
            App.toast(`Copied ${email} to clipboard`, 'success');
        }).catch(() => {
            App.toast(`Could not copy email`, 'warning');
        });
    },

    viewFullThread(id) {
        const reply = (this.repliesData?.results || []).find(r => r.id === id);
        if (!reply) return;

        App.showResult(`Reply Details: ${App.escapeHtml(reply.from_name || reply.from_email)}`, `
            <div style="font-size:0.92rem;line-height:1.6">
                <p><strong>From:</strong> ${App.escapeHtml(reply.from_name || '')} &lt;${App.escapeHtml(reply.from_email)}&gt;</p>
                <p><strong>Subject:</strong> ${App.escapeHtml(reply.subject || '')}</p>
                <p><strong>Campaign:</strong> ${App.escapeHtml(reply.campaign_name || 'N/A')}</p>
                <p><strong>Sentiment:</strong> <span class="badge">${App.escapeHtml(reply.sentiment)}</span></p>
                <p><strong>Date:</strong> ${App.escapeHtml(reply.date || '')}</p>
                <hr style="border:none;border-top:1px solid var(--border-color);margin:16px 0;">
                <div style="background:var(--bg-input);padding:14px;border-radius:8px;font-family:monospace;white-space:pre-wrap;word-break:break-word;">
${App.escapeHtml(reply.full_body || reply.body_preview || '')}
                </div>
            </div>
        `);
    },

    async unsubscribeLead(email) {
        const ok = await App.confirm(
            'Blocklist & Unsubscribe',
            `Are you sure you want to blocklist and unsubscribe ${email}? They will never receive any further outreach.`
        );
        if (!ok) return;

        try {
            // Unsubscribe from listmonk via existing subscriber search or blocklist
            const subsRes = await API.get(`/api/subscribers?query=${encodeURIComponent("subscribers.email = '" + email + "'")}`);
            const subs = subsRes?.data?.results || [];
            if (subs.length > 0) {
                const subId = subs[0].id;
                await API.put(`/api/subscribers/${subId}/blocklist`, {});
            }
            App.toast(`${email} successfully blocklisted.`, 'success');
        } catch (err) {
            App.toast(`Failed to blocklist: ${err?.message}`, 'danger');
        }
    },

    handleSearch(val) {
        this.query = val.trim();
        this.page = 1;
        this.render();
    },

    filterSentiment(val) {
        this.sentiment = val;
        this.page = 1;
        this.render();
    },

    filterStatus(val) {
        this.status = val;
        this.page = 1;
        this.render();
    },

    goToPage(p) {
        this.page = p;
        this.render();
    }
};
