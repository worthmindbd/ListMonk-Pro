/**
 * Dashboard home page with summary stats and analytics charts.
 */
const Dashboard = {
    charts: {},
    campaignsData: [],
    summary: null,

    getThemeColors() {
        const isDark = document.documentElement.getAttribute('data-theme') !== 'light';
        return {
            isDark,
            textColor: isDark ? '#94a3b8' : '#64748d',
            gridColor: isDark ? 'rgba(255, 255, 255, 0.07)' : 'rgba(0, 55, 112, 0.06)',
            tooltipBg: isDark ? 'rgba(15, 23, 42, 0.92)' : 'rgba(255, 255, 255, 0.95)',
            tooltipText: isDark ? '#f8fafc' : '#0d253d',
            tooltipBorder: isDark ? 'rgba(255, 255, 255, 0.12)' : 'rgba(227, 232, 238, 0.9)',
        };
    },

    /** Re-read everything from ListMonk now (the header's Refresh button). */
    refresh() {
        this._fresh = true;
        App.bumpSeq();
        this.render();
    },

    async render() {
        const seq = App.seq();
        const fresh = this._fresh === true;
        this._fresh = false;
        App.setContent('<div class="loading-spinner">Loading dashboard...</div>');

        const isCold = App.currentMode === 'cold_email';

        try {
            // Fetch all data in parallel including reply stats if in cold email mode
            const requests = [
                API.get('/api/lists?per_page=1&minimal=true'),
                API.get('/api/campaigns?per_page=10&order_by=created_at&order=DESC'),
                API.get('/api/subscribers?per_page=1'),
                API.get(`/api/campaigns/summary${fresh ? '?fresh=true' : ''}`),
                // Silent: a failed daily count only hides the "today" line.
                API.get('/api/sends/daily?days=1', true),
            ];

            if (isCold) {
                requests.push(API.get('/api/replies/stats'));
                requests.push(API.get('/api/replies?per_page=5'));
            }

            // "Needs attention" strip: fetched alongside, never blocks the page.
            const attentionPromise = this._attention(isCold);

            const results = await Promise.allSettled(requests);
            const [listsRes, campaignsRes, subscribersRes, summaryRes, sendsRes] = results;
            const replyStatsRes = isCold ? results[5] : null;
            const recentRepliesRes = isCold ? results[6] : null;
            const sends = sendsRes.status === 'fulfilled' ? (sendsRes.value || null) : null;
            const sentToday = sends ? (sends.sent_today ?? null) : null;
            // Sends by a campaign deleted since still happened today, but
            // ListMonk no longer lists it: say so, or the number looks invented.
            const byDeleted = sends?.sent_today_by_deleted || 0;
            const deletedNote = !byDeleted ? ''
                : byDeleted >= sentToday ? ' (by a deleted campaign)'
                : ` (${App.formatNumber(byDeleted)} by a deleted campaign)`;
            const sentTodayText = sentToday === null ? '' : `${App.formatNumber(sentToday)} sent today${deletedNote}`;
            this.loadedAt = new Date();

            this.summary = summaryRes.status === 'fulfilled' ? (summaryRes.value || null) : null;
            const totalLists = listsRes.status === 'fulfilled' ? (listsRes.value?.data?.total || 0) : 0;
            const totalCampaigns = this.summary?.total_campaigns
                ?? (campaignsRes.status === 'fulfilled' ? (campaignsRes.value?.data?.total || 0) : 0);
            const totalSubscribers = subscribersRes.status === 'fulfilled' ? (subscribersRes.value?.data?.total || 0) : 0;
            const activeCampaigns = this.summary?.status_counts?.running ?? null;

            let campaigns = [];
            if (campaignsRes.status === 'fulfilled') {
                campaigns = campaignsRes.value?.data?.results || [];
            }
            this.campaignsData = campaigns;

            const totalSent = this.summary?.sent ?? campaigns.reduce((sum, c) => sum + (c.sent || 0), 0);

            let html = '';

            if (isCold) {
                const repStats = replyStatsRes?.status === 'fulfilled' ? replyStatsRes.value : {};
                // Retained so the theme toggle can re-render the sentiment
                // chart without a full page reload.
                this.lastRepStats = repStats;
                const recentReplies = recentRepliesRes?.status === 'fulfilled' ? (recentRepliesRes.value?.results || []) : [];
                // Human replies, matching the reply rate beside it: the rate
                // excludes OOO/ticket auto-responses, so counting them here
                // made the two numbers disagree.
                const totalReplies = repStats.human_replies ?? repStats.total_replies ?? 0;
                const replyRate = repStats.reply_rate || 0;
                const positiveLeads = repStats.positive || 0;

                html += `
                    <!-- Cold Email KPI Cards -->
                    ${this._syncBar()}
                    <div id="dashAttention"></div>
                    <div class="stats-grid dash-kpis">
                        <a class="stat-card stat-card-link accent" href="#/subscribers">
                            <div class="stat-label">Total Leads</div>
                            <div class="stat-value">${App.formatNumber(totalSubscribers)}</div>
                            <div class="stat-sub">Across ${totalLists} lead list${totalLists === 1 ? '' : 's'}</div>
                        </a>
                        <a class="stat-card stat-card-link info" href="#/analytics">
                            <div class="stat-label">Outbound Sent</div>
                            <div class="stat-value">${App.formatNumber(totalSent)}</div>
                            <div class="stat-sub">${sentTodayText ? `<strong style="color:var(--primary-soft)">${sentTodayText}</strong> · ` : ''}${totalCampaigns} campaign${totalCampaigns === 1 ? '' : 's'}</div>
                        </a>
                        <a class="stat-card stat-card-link success" href="#/replies">
                            <div class="stat-label">Prospect Replies</div>
                            <div class="stat-value" style="color:var(--emerald)">${App.formatNumber(totalReplies)}</div>
                            <div class="stat-sub"><strong style="color:var(--emerald)">${replyRate}%</strong> reply rate</div>
                        </a>
                        <a class="stat-card stat-card-link warning" href="#/replies?status=lead">
                            <div class="stat-label"><span class="badge-dot badge-dot-positive"></span> Hot Leads / Interested</div>
                            <div class="stat-value" style="color:var(--amber)">${App.formatNumber(positiveLeads)}</div>
                            <div class="stat-sub">Positive meeting &amp; call intent →</div>
                        </a>
                    </div>

                    <!-- Charts Grid -->
                    <div class="charts-grid">
                        <div class="card">
                            <div class="card-header">
                                <h2 class="card-title">Outbound Volume per Campaign</h2>
                            </div>
                            <div class="chart-container">
                                <canvas id="campaignChart"></canvas>
                            </div>
                        </div>
                        <div class="card">
                            <div class="card-header">
                                <h2 class="card-title">Prospect Sentiment Distribution</h2>
                            </div>
                            <div class="chart-container">
                                <canvas id="statusChart"></canvas>
                            </div>
                        </div>
                    </div>

                    <!-- Bottom Grid: Recent Replies & Recent Campaigns -->
                    <div class="dashboard-bottom-grid" style="display:grid;grid-template-columns:repeat(auto-fit,minmax(380px,1fr));gap:24px;margin-top:24px;">
                        <div class="card">
                            <div class="card-header">
                                <h2 class="card-title">Recent Prospect Replies</h2>
                                <a href="#/replies" class="btn btn-sm">View Inbox Feed →</a>
                            </div>
                            <div class="replies-compact-list" style="padding:10px 0;">`;

                if (!recentReplies.length) {
                    html += `<div style="text-align:center;padding:32px 16px;color:var(--text-muted);font-size:0.9rem">No replies recorded yet. Click <a href="#/replies" style="color:var(--primary)">Scan Mailbox</a> to sync incoming replies.</div>`;
                } else {
                    recentReplies.slice(0, 4).forEach(r => {
                        const sentMap = {
                            positive: { label: 'Interested', cls: 'badge-success', dotCls: 'badge-dot-positive' },
                            neutral: { label: 'Neutral', cls: 'badge-secondary', dotCls: 'badge-dot-neutral' },
                            auto_reply: { label: 'Auto-Reply', cls: 'badge-secondary', dotCls: 'badge-dot-auto-reply' },
                            negative: { label: 'Not Interested', cls: 'badge-danger', dotCls: 'badge-dot-negative' },
                        };
                        const sent = sentMap[r.sentiment] || { label: App.escapeHtml(r.sentiment || 'Reply'), cls: 'badge-secondary', dotCls: 'badge-dot-auto-reply' };
                        html += `
                            <div style="padding:10px 16px;border-bottom:1px solid var(--border-color);display:flex;align-items:center;justify-content:space-between;gap:12px;">
                                <div style="overflow:hidden;">
                                    <div style="font-weight:600;font-size:0.88rem;color:var(--text-primary);white-space:nowrap;overflow:hidden;text-overflow:ellipsis;">
                                        ${App.escapeHtml(r.from_name || r.from_email)}
                                    </div>
                                    <div style="font-size:0.8rem;color:var(--text-secondary);white-space:nowrap;overflow:hidden;text-overflow:ellipsis;">
                                        ${App.escapeHtml(r.subject || '(No subject)')}
                                    </div>
                                </div>
                                <span class="badge ${sent.cls}" style="font-size:0.75rem;flex-shrink:0;"><span class="badge-dot ${sent.dotCls}"></span>${sent.label}</span>
                            </div>
                        `;
                    });
                }

                html += `
                            </div>
                        </div>

                        <div class="card">
                            <div class="card-header">
                                <h2 class="card-title">Recent Outreach Campaigns</h2>
                                <a href="#/campaigns" class="btn btn-sm">All Campaigns →</a>
                            </div>
                            <div class="table-wrapper" tabindex="0" role="region" aria-label="Scrollable data table" style="border:none;box-shadow:none;background:transparent">
                                <table>
                                    <thead><tr>
                                        <th>Campaign</th><th>Status</th><th>Sent</th>
                                    </tr></thead>
                                    <tbody>`;

                campaigns.slice(0, 4).forEach(c => {
                    // Keyboard-equivalent of the row click: the row is a link
                    // shortcut, so it needs a role and a focus stop of its own.
                    html += `<tr style="cursor:pointer" tabindex="0" role="link"
                        aria-label="View campaign ${App.escapeHtml(c.name)}"
                        onclick="Campaigns.showDetail(${c.id})"
                        onkeydown="if(event.key==='Enter'||event.key===' '){event.preventDefault();Campaigns.showDetail(${c.id})}">
                        <td><strong>${App.escapeHtml(c.name)}</strong></td>
                        <td>${App.statusBadge(c.status)}</td>
                        <td>${this._sentOf(c)}</td>
                    </tr>`;
                });

                if (!campaigns.length) {
                    html += '<tr><td colspan="3" style="text-align:center;color:var(--text-muted);padding:24px">No campaigns yet</td></tr>';
                }

                html += `</tbody></table></div></div></div>`;

                if (App.stale(seq)) return;
                App.setContent(html);

                // Render cold charts
                this.renderCampaignChart(campaigns);
                this.renderColdSentimentChart(repStats);
                this._paintAttention(seq, attentionPromise);
                this._scheduleRefresh(campaigns, activeCampaigns);

            } else {                // Newsletter Mode
                // Drop the retained cold-email stats so a later theme-toggle
                // re-render cannot paint a stale sentiment chart in newsletter
                // mode.
                this.lastRepStats = null;
                html += `
                    ${this._syncBar()}
                    <div id="dashAttention"></div>
                    <div class="stats-grid dash-kpis">
                        <a class="stat-card stat-card-link accent" href="#/subscribers">
                            <div class="stat-label">Total Subscribers</div>
                            <div class="stat-value">${App.formatNumber(totalSubscribers)}</div>
                        </a>
                        <a class="stat-card stat-card-link success" href="#/lists">
                            <div class="stat-label">Total Lists</div>
                            <div class="stat-value">${App.formatNumber(totalLists)}</div>
                        </a>
                        <a class="stat-card stat-card-link warning" href="#/campaigns">
                            <div class="stat-label">Total Campaigns</div>
                            <div class="stat-value">${App.formatNumber(totalCampaigns)}</div>
                        </a>
                        <a class="stat-card stat-card-link info" href="#/campaigns?status=running">
                            <div class="stat-label">Active Campaigns</div>
                            <div class="stat-value">${App.formatNumber(activeCampaigns ?? campaigns.filter(c => c.status === 'running').length)}</div>
                            ${sentTodayText ? `<div class="stat-sub"><strong style="color:var(--primary-soft)">${sentTodayText}</strong></div>` : ''}
                        </a>
                    </div>

                    <div class="charts-grid">
                        <div class="card">
                            <div class="card-header">
                                <h2 class="card-title">Campaign Performance</h2>
                            </div>
                            <div class="chart-container">
                                <canvas id="campaignChart"></canvas>
                            </div>
                        </div>
                        <div class="card">
                            <div class="card-header">
                                <h2 class="card-title">Campaign Status Distribution</h2>
                            </div>
                            <div class="chart-container">
                                <canvas id="statusChart"></canvas>
                            </div>
                        </div>
                    </div>

                    <div class="card" style="margin-top:24px">
                        <div class="card-header">
                            <h2 class="card-title">Recent Campaigns</h2>
                            <a href="#/campaigns" class="btn btn-sm">View All</a>
                        </div>
                        <div class="table-wrapper" tabindex="0" role="region" aria-label="Scrollable data table" style="border:none;box-shadow:none;background:transparent">
                            <table>
                                <thead><tr>
                                    <th>Name</th><th>Status</th><th>Lists</th><th>Created</th>
                                </tr></thead>
                                <tbody>`;

                campaigns.slice(0, 5).forEach(c => {
                    const listNames = (c.lists || []).map(l => App.escapeHtml(l.name)).join(', ') || '-';
                    html += `<tr style="cursor:pointer" onclick="Campaigns.showDetail(${c.id})">
                        <td><strong>${App.escapeHtml(c.name)}</strong></td>
                        <td>${App.statusBadge(c.status)}</td>
                        <td>${listNames}</td>
                        <td>${App.formatDate(c.created_at)}</td>
                    </tr>`;
                });

                if (!campaigns.length) {
                    html += '<tr><td colspan="4" style="text-align:center;color:var(--text-muted);padding:24px">No campaigns yet</td></tr>';
                }

                html += '</tbody></table></div></div>';
                if (App.stale(seq)) return;
                App.setContent(html);

                this.renderCampaignChart(campaigns);
                this.renderStatusChart(campaigns);
                this._paintAttention(seq, attentionPromise);
                this._scheduleRefresh(campaigns, activeCampaigns);
            }

        } catch (err) {
            if (App.stale(seq)) return;
            App.setContent(`<div class="empty-state"><h2>Failed to load dashboard</h2><p>${App.escapeHtml(err.message)}</p></div>`);
        }
    },

    /** "sent / audience": the audience alone used to stand in for the sent
        count while nothing had gone out, which read as "1,730 dispatched". */
    _sentOf(c) {
        const sent = App.formatNumber(c.sent || 0);
        return c.to_send ? `${sent} <span style="color:var(--text-muted)">/ ${App.formatNumber(c.to_send)}</span>` : sent;
    },

    /** Where the numbers come from and when they were read, plus Refresh. */
    _syncBar() {
        const at = (this.loadedAt || new Date()).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' });
        return `<div class="dash-sync" role="status">
            <span><span class="dash-sync-dot" aria-hidden="true"></span>Live from ListMonk · read at <strong>${at}</strong><span id="dashSyncPoll"></span></span>
            <button type="button" class="btn btn-sm" onclick="Dashboard.refresh()">Refresh</button>
        </div>`;
    },

    _fmtSpan(seconds) {
        const m = Math.floor(seconds / 60);
        if (m < 60) return `${Math.max(1, m)} min`;
        const h = Math.floor(m / 60), r = m % 60;
        return r ? `${h} h ${r} min` : `${h} h`;
    },

    /** Things that need a look, gathered from what other pages already
        expose: unread replies, campaigns held by the send window or the
        warm-up quota, a cleanup run held for review. Each request is best
        effort; nothing here may fail the dashboard. */
    async _attention(isCold) {
        const quiet = (p) => p.then(v => v).catch(() => null);
        const [unread, sched, warm, cleanup, running, progress] = await Promise.all([
            isCold ? quiet(API.get('/api/replies?status=unread&per_page=1', true)) : null,
            quiet(API.get('/api/scheduler', true)),
            quiet(API.get('/api/warmup', true)),
            quiet(API.get('/api/email-validation/cleanup', true)),
            quiet(API.get('/api/campaigns?status=running&per_page=1', true)),
            quiet(API.get('/api/sends/progress', true)),
        ]);
        this.lastProgress = progress;
        const items = [];
        // A "running" campaign that sends nothing is the one failure ListMonk
        // never reports (see services/send_stats.track_progress).
        for (const c of (progress?.campaigns || []).filter(c => c.stalled)) {
            items.push({
                tone: 'danger', href: '#/deliverability',
                text: `${c.name || `Campaign #${c.id}`} is running but has sent nothing for ${this._fmtSpan(c.idle_seconds)}: ListMonk's send queue looks stuck (a paused or deleted campaign can hold it; restarting ListMonk clears it)`,
            });
        }
        const unreadN = unread?.total || 0;
        if (unreadN) items.push({ tone: 'success', href: '#/replies?status=unread', text: `${App.formatNumber(unreadN)} unread repl${unreadN === 1 ? 'y' : 'ies'}` });
        const held = sched?.enabled ? (sched.auto_paused_campaigns || []).length : 0;
        if (warm?.enabled && warm.remaining <= 0) {
            items.push({ tone: 'warning', href: '#/settings?tab=warmup', text: `Warm-up quota reached (${App.formatNumber(warm.sent_today)} / ${App.formatNumber(warm.quota)}): sending resumes tomorrow` });
        } else if (held) {
            items.push({ tone: 'warning', href: '#/settings?tab=scheduler', text: `${held} campaign${held === 1 ? '' : 's'} paused until the send window opens` });
        }
        if (cleanup?.last_run?.status === 'held') {
            items.push({ tone: 'danger', href: '#/validation', text: 'Email cleanup held: too many addresses looked dead, review before anything is blocklisted' });
        } else if (cleanup?.last_run?.status === 'failed') {
            items.push({ tone: 'danger', href: '#/settings?tab=cleanup', text: 'The last email cleanup failed' });
        }
        const runningN = running?.data?.total || 0;
        const stalledN = (progress?.campaigns || []).filter(c => c.stalled).length;
        if (runningN > stalledN) {
            const n = runningN - stalledN;
            items.push({ tone: 'info', href: '#/campaigns?status=running', text: `${n} campaign${n === 1 ? '' : 's'} sending now` });
        }
        return items;
    },

    async _paintAttention(seq, promise) {
        let items = [];
        try { items = await promise; } catch { return; }
        if (App.stale(seq)) return;
        const poll = document.getElementById('dashSyncPoll');
        const last = this.lastProgress?.last_poll ? new Date(this.lastProgress.last_poll) : null;
        if (poll && last && !isNaN(last)) {
            const ago = Math.max(0, Math.round((Date.now() - last.getTime()) / 1000));
            poll.textContent = ` · send counters checked ${ago < 60 ? `${ago} s` : this._fmtSpan(ago)} ago`;
        }
        const el = document.getElementById('dashAttention');
        if (!el || !items.length) return;
        el.innerHTML = `<div class="attention-strip" role="status" aria-label="Needs attention">
            ${items.map(i => `<a class="attention-item tone-${i.tone}" href="${i.href}"><span class="attention-dot"></span>${App.escapeHtml(i.text)}<span aria-hidden="true">→</span></a>`).join('')}
        </div>`;
    },

    /** Keep the numbers live while a campaign sends. */
    _scheduleRefresh(campaigns, activeCampaigns) {
        const running = (activeCampaigns ?? 0) > 0 || (campaigns || []).some(c => c.status === 'running');
        if (running) App.autoRefresh('dashboard', () => { App.bumpSeq(); this.render(); }, 30000);
        else App.stopAutoRefresh();
    },

    /** Replace a chart with an explanation when it has nothing to show. */
    _emptyChart(canvasId, message, href, linkText) {
        const canvas = document.getElementById(canvasId);
        const box = canvas?.parentElement;
        if (!box) return;
        box.classList.add('has-chart-empty');
        box.querySelector('.chart-empty')?.remove();
        box.insertAdjacentHTML('beforeend', App.emptyChart(message, href, linkText));
    },

    renderCampaignChart(campaigns) {
        const ctx = document.getElementById('campaignChart');
        if (!ctx) return;
        const anySent = (campaigns || this.campaignsData || []).some(c => (c.sent || 0) > 0);
        if (!anySent) {
            if (this.charts.campaign) { this.charts.campaign.destroy(); this.charts.campaign = null; }
            this._emptyChart('campaignChart', 'Nothing sent yet. Bars appear here once a campaign starts sending.', '#/campaigns', 'Go to campaigns');
            return;
        }

        const theme = this.getThemeColors();
        const recent = (campaigns || this.campaignsData).slice(0, 8).reverse();
        const labels = recent.map(c => App.truncateLabel(c.name, 22));
        const sent = recent.map(c => c.sent || 0);
        const views = recent.map(c => c.views || 0);
        const clicks = recent.map(c => c.clicks || 0);

        // A series whose values are all zero draws no bars but still claims a legend
        // entry: in cold-email mode (no tracking pixels) "Views" was permanently
        // empty and just added noise to the legend.
        const hasData = (arr) => arr.some(v => Number(v) > 0);

        const datasets = [
            { label: 'Sent', data: sent, backgroundColor: 'rgba(83, 58, 253, 0.82)', borderRadius: 6 },
        ];
        if (hasData(views)) {
            datasets.push({ label: 'Views', data: views, backgroundColor: 'rgba(16, 185, 129, 0.82)', borderRadius: 6 });
        }
        // Cold email mode disables tracking, so clicks are not charted there.
        if (App.currentMode !== 'cold_email' && hasData(clicks)) {
            datasets.push({ label: 'Clicks', data: clicks, backgroundColor: 'rgba(245, 158, 11, 0.82)', borderRadius: 6 });
        }

        if (this.charts.campaign) this.charts.campaign.destroy();
        this.charts.campaign = new Chart(ctx, {
            type: 'bar',
            data: {
                labels,
                datasets,
            },
            options: {
                responsive: true,
                maintainAspectRatio: false,
                // A lone campaign otherwise stretches into one full-width slab.
                datasets: { bar: { maxBarThickness: 64 } },
                plugins: {
                    legend: {
                        labels: {
                            color: theme.textColor,
                            font: { family: 'Plus Jakarta Sans', weight: 500, size: 12 },
                            usePointStyle: true,
                            pointStyle: 'circle',
                            padding: 18,
                        },
                    },
                    tooltip: {
                        backgroundColor: theme.tooltipBg,
                        titleColor: theme.tooltipText,
                        bodyColor: theme.tooltipText,
                        borderColor: theme.tooltipBorder,
                        borderWidth: 1,
                        padding: 12,
                        cornerRadius: 10,
                        bodyFont: { family: 'Plus Jakarta Sans' },
                        titleFont: { family: 'Plus Jakarta Sans', weight: 600 },
                    },
                },
                scales: {
                    x: {
                        ticks: { color: theme.textColor, font: { family: 'Plus Jakarta Sans', size: 11 } },
                        grid: { color: theme.gridColor, drawBorder: false },
                    },
                    y: {
                        ticks: { color: theme.textColor, font: { family: 'Plus Jakarta Sans', size: 11 } },
                        grid: { color: theme.gridColor, drawBorder: false },
                        beginAtZero: true,
                    },
                },
            },
        });
    },

    renderStatusChart(campaigns) {
        const ctx = document.getElementById('statusChart');
        if (!ctx) return;

        const theme = this.getThemeColors();
        const statusCounts = {};
        if (this.summary?.status_counts) {
            Object.assign(statusCounts, this.summary.status_counts);
        } else {
            (campaigns || this.campaignsData).forEach(c => {
                statusCounts[c.status] = (statusCounts[c.status] || 0) + 1;
            });
        }

        const colorMap = {
            draft: '#3b82f6',
            running: '#10b981',
            finished: '#533afd',
            paused: '#f59e0b',
            cancelled: '#ea2261',
            scheduled: '#8b5cf6',
        };

        const labels = Object.keys(statusCounts).filter(k => statusCounts[k] > 0);
        const data = labels.map(k => statusCounts[k]);
        const bgColors = labels.map(s => colorMap[s] || '#64748b');
        if (!data.length) {
            if (this.charts.status) { this.charts.status.destroy(); this.charts.status = null; }
            this._emptyChart('statusChart', 'No campaigns yet.', '#/campaigns', 'Create a campaign');
            return;
        }

        if (this.charts.status) this.charts.status.destroy();
        this.charts.status = new Chart(ctx, {
            type: 'doughnut',
            data: {
                labels,
                datasets: [{
                    data,
                    backgroundColor: bgColors,
                    // A single 100% slice still drew its 2px separator, which
                    // showed up as a hairline seam splitting the ring. Only
                    // slices with a value should count as separators.
                    borderWidth: data.filter(v => Number(v) > 0).length > 1 ? 2 : 0,
                    borderColor: theme.isDark ? '#0f172a' : '#ffffff',
                    hoverOffset: 6,
                }],
            },
            options: {
                responsive: true,
                maintainAspectRatio: false,
                cutout: '70%',
                plugins: {
                    legend: {
                        position: 'bottom',
                        labels: {
                            color: theme.textColor,
                            padding: 16,
                            font: { family: 'Plus Jakarta Sans', weight: 500, size: 12 },
                            usePointStyle: true,
                            pointStyle: 'circle',
                        },
                    },
                    tooltip: {
                        backgroundColor: theme.tooltipBg,
                        titleColor: theme.tooltipText,
                        bodyColor: theme.tooltipText,
                        borderColor: theme.tooltipBorder,
                        borderWidth: 1,
                        padding: 12,
                        cornerRadius: 10,
                        bodyFont: { family: 'Plus Jakarta Sans' },
                    },
                },
            },
        });
    },

    renderColdSentimentChart(repStats) {
        const ctx = document.getElementById('statusChart');
        if (!ctx) return;

        const theme = this.getThemeColors();
        const positive = repStats?.positive || 0;
        const neutral = repStats?.neutral || 0;
        const autoReply = repStats?.auto_reply || 0;
        const negative = repStats?.negative || 0;

        // Neutral is the classifier's default for a human reply without a
        // clear signal — usually the largest slice. Leaving it out drew one
        // positive reply among ten neutral ones as a 100% "Interested" ring.
        const labels = ['Positive / Interested', 'Neutral / No Signal', 'Automated / Auto-Reply', 'Not Interested'];
        const data = [positive, neutral, autoReply, negative];

        // If no data, show placeholder empty chart
        const total = data.reduce((a, b) => a + b, 0);

        const bgColors = ['#10b981', '#3b82f6', '#64748b', '#f43f5e'];

        // No replies: an explanation instead of a grey placeholder ring
        // (which also drew as a broken dark arc in the light theme).
        if (total === 0) {
            if (this.charts.status) { this.charts.status.destroy(); this.charts.status = null; }
            this._emptyChart('statusChart', 'No prospect replies yet. The mailbox is scanned on a schedule; you can also scan it now.', '#/replies', 'Open the reply inbox');
            return;
        }

        if (this.charts.status) this.charts.status.destroy();
        this.charts.status = new Chart(ctx, {
            type: 'doughnut',
            data: {
                labels,
                datasets: [{
                    data,
                    backgroundColor: bgColors,
                    // No separator when the ring is a single slice (see above).
                    borderWidth: data.filter(v => Number(v) > 0).length > 1 ? 2 : 0,
                    borderColor: theme.isDark ? '#0f172a' : '#ffffff',
                    hoverOffset: 6,
                }],
            },
            options: {
                responsive: true,
                maintainAspectRatio: false,
                cutout: '70%',
                plugins: {
                    legend: {
                        position: 'bottom',
                        labels: {
                            color: theme.textColor,
                            padding: 14,
                            font: { family: 'Plus Jakarta Sans', weight: 500, size: 11 },
                            usePointStyle: true,
                            pointStyle: 'circle',
                        },
                    },
                    tooltip: {
                        backgroundColor: theme.tooltipBg,
                        titleColor: theme.tooltipText,
                        bodyColor: theme.tooltipText,
                        borderColor: theme.tooltipBorder,
                        borderWidth: 1,
                        padding: 12,
                        cornerRadius: 10,
                        bodyFont: { family: 'Plus Jakarta Sans' },
                    },
                },
            },
        });
    },
};

// Listen to theme switch to dynamically update charts.
// The second dashboard doughnut belongs to a different metric per mode
// (campaign status vs. prospect sentiment) — re-render the one the current
// mode actually shows, otherwise toggling the theme silently swaps in the
// wrong data under the wrong heading.
window.addEventListener('themeChanged', () => {
    if (App.currentPage === 'dashboard') {
        Dashboard.renderCampaignChart();
        if (App.currentMode === 'cold_email') {
            Dashboard.renderColdSentimentChart(Dashboard.lastRepStats || {});
        } else {
            Dashboard.renderStatusChart();
        }
    }
});

