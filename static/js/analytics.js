/**
 * Dedicated Analytics page with campaign-level insights, charts, and exports.
 */
const Analytics = {
    charts: {},
    campaigns: [],
    summary: null,
    selectedCampaignId: 0,
    fromDate: '',
    toDate: '',
    campaignUnsubCount: 0,
    replyStats: null,
    campaignReplyStats: null,
    replyByCampaign: {},
    /** Daily send statistics (/api/sends/daily) and the range shown. */
    sends: null,
    sendsDays: 30,
    /** Generation counter for campaign-scoped side stats, so a slow response
        for the previous campaign cannot repaint over the newer selection. */
    _sideSeq: 0,
    /** Separate counter for chart analytics loads so they don't invalidate
        in-flight side stats (and vice versa). */
    _chartSeq: 0,

    isColdMode() {
        return App.currentMode === 'cold_email';
    },

    async render() {
        this._seq = App.seq();
        // Deep-linkable state: #/analytics?campaign=6&from=…&to=…
        const rq = App.routeQuery();
        if (rq.get('campaign')) this.selectedCampaignId = parseInt(rq.get('campaign'), 10) || 0;
        if (rq.get('from')) this.fromDate = rq.get('from');
        if (rq.get('to')) this.toDate = rq.get('to');
        // #/analytics?campaign=6&section=unsubscribes opens on that campaign's
        // unsubscribe records (the campaign page and old links point here).
        this._scrollToUnsubs = rq.get('section') === 'unsubscribes';
        // Set default date range: last 30 days (LOCAL calendar days — an
        // toISOString() date is the UTC day, which shifts near local midnight).
        const now = new Date();
        const thirtyDaysAgo = new Date(now);
        thirtyDaysAgo.setDate(thirtyDaysAgo.getDate() - 30);
        const localDay = d => `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`;
        if (!this.fromDate) this.fromDate = localDay(thirtyDaysAgo);
        if (!this.toDate) this.toDate = localDay(now);

        App.setContent('<div class="loading-spinner">Loading analytics…</div>');

        // Fetch campaigns, unsubscribe stats, and accurate totals in parallel.
        // Cold email mode additionally pulls prospect reply stats (overall
        // reply rate plus the per-campaign breakdown used by the chart).
        try {
            const requests = [
                API.get('/api/campaigns?per_page=100&order_by=created_at&order=DESC'),
                API.get('/api/unsubscribes/stats'),
                API.get('/api/campaigns/summary'),
                API.get(`/api/sends/daily?days=${this.sendsDays}`, true),
            ];
            // Per-campaign unsubscribe counts for the comparison table.
            requests.push(API.get('/api/unsubscribes/campaigns', true));
            if (this.isColdMode()) requests.push(API.get('/api/replies/stats'));

            const [campRes, unsubRes, summaryRes, sendsRes, unsubGroupsRes, replyRes] = await Promise.allSettled(requests);
            this.unsubByCampaign = {};
            if (unsubGroupsRes.status === 'fulfilled') {
                (unsubGroupsRes.value?.data || []).forEach(g => {
                    if (Number.isInteger(g.campaign_id)) this.unsubByCampaign[g.campaign_id] = g.count || 0;
                });
            }
            this.sends = sendsRes.status === 'fulfilled' ? (sendsRes.value || null) : null;
            this.campaigns = campRes.status === 'fulfilled' ? (campRes.value?.data?.results || []) : [];
            this.unsubStats = unsubRes.status === 'fulfilled' ? unsubRes.value : { total: 0 };
            this.summary = summaryRes.status === 'fulfilled' ? (summaryRes.value || null) : null;

            if (this.isColdMode() && replyRes?.status === 'fulfilled') {
                this.replyStats = replyRes.value || null;
                this.replyByCampaign = this.replyStats?.by_campaign || {};
            } else {
                this.replyStats = null;
                this.replyByCampaign = {};
            }

            // Default to first campaign if none selected
            if (!this.selectedCampaignId && this.campaigns.length) {
                this.selectedCampaignId = this.campaigns[0].id;
            }

            // Fetch per-campaign unsubscribe count and reply stats
            await this.loadCampaignSideStats();
            this.renderPage();
        } catch (err) {
            console.error('Analytics load error:', err);
            this.campaigns = [];
            this.unsubStats = { total: 0 };
            this.replyStats = null;
            this.replyByCampaign = {};
            if (App.stale(this._seq)) return;
            App.setContent(`<div class="empty-state"><h2>Failed to load analytics</h2><p>${App.escapeHtml(err?.message || '')}</p></div>`);
        }
    },

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

    renderPage() {
        if (this._seq !== undefined && App.stale(this._seq)) return;
        const campOptions = this.campaigns.map(c =>
            `<option value="${c.id}" ${c.id === this.selectedCampaignId ? 'selected' : ''}>${App.escapeHtml(c.name)} (${App.escapeHtml(c.status || '')})</option>`
        ).join('');

        // Build the overview stats. Prefer the all-campaigns summary endpoint;
        // fall back to the loaded page only if it is unavailable.
        const selectedCamp = this.campaigns.find(c => c.id === this.selectedCampaignId);
        const totalSent = this.summary?.sent ?? this.campaigns.reduce((sum, c) => sum + (c.sent || 0), 0);
        const totalViews = this.summary?.views ?? this.campaigns.reduce((sum, c) => sum + (c.views || 0), 0);
        const totalClicks = this.summary?.clicks ?? this.campaigns.reduce((sum, c) => sum + (c.clicks || 0), 0);
        const totalBounces = this.summary?.bounces ?? this.campaigns.reduce((sum, c) => sum + (c.bounces || 0), 0);
        const totalUnsubs = this.unsubStats?.total || 0;

        // Cold email mode disables link tracking, so click metrics are hidden there
        // and prospect reply stats take their place.
        const isCold = this.isColdMode();
        // Human replies, matching the reply rate shown with them (the rate
        // excludes automated responses; the raw total does not).
        const totalReplies = this.replyStats?.human_replies ?? this.replyStats?.total_replies ?? 0;
        const overallReplyRate = this.replyStats?.reply_rate || 0;
        const campReplies = this.campaignReplyStats?.human_replies ?? this.campaignReplyStats?.total_replies ?? 0;
        const campReplyRate = this.campaignReplyStats?.reply_rate || 0;

        let html = `
            <!-- Overview Stats -->
            <div class="stats-grid">
                <div class="stat-card accent">
                    <div class="stat-label">Total Sent</div>
                    <div class="stat-value">${App.formatNumber(totalSent)}</div>
                    <div style="font-size:0.8rem;color:var(--text-muted)">all time</div>
                </div>
                ${this._sentTodayCard()}
                ${isCold ? '' : `
                <div class="stat-card success">
                    <div class="stat-label">Total Opens</div>
                    <div class="stat-value">${App.formatNumber(totalViews)}</div>
                    <div style="font-size:0.8rem;color:var(--text-muted)">${totalSent ? ((totalViews/totalSent)*100).toFixed(1) : 0}% open rate</div>
                </div>`}
                ${isCold ? '' : `
                <div class="stat-card warning">
                    <div class="stat-label">Total Clicks</div>
                    <div class="stat-value">${App.formatNumber(totalClicks)}</div>
                    <div style="font-size:0.8rem;color:var(--text-muted)">${totalSent ? ((totalClicks/totalSent)*100).toFixed(1) : 0}% CTR</div>
                </div>`}
                <div class="stat-card" style="border-left-color:#14b8a6">
                    <div class="stat-label">Unsubscribes</div>
                    <div class="stat-value" style="color:#14b8a6" id="anUnsubTotal">${App.formatNumber(totalUnsubs)}</div>
                    <div style="font-size:0.8rem;color:var(--text-muted)">listed per campaign below · <a href="#/settings?tab=automation" style="color:#14b8a6;text-decoration:none">monitor</a></div>
                </div>
                <div class="stat-card danger">
                    <div class="stat-label">Total Bounces</div>
                    <div class="stat-value">${App.formatNumber(totalBounces)}</div>
                    <div style="font-size:0.8rem;color:var(--text-muted)">${totalSent ? ((totalBounces/totalSent)*100).toFixed(1) : 0}% bounce rate</div>
                </div>
                ${isCold ? `
                <div class="stat-card success">
                    <div class="stat-label">Prospect Replies</div>
                    <div class="stat-value" style="color:var(--emerald)">${App.formatNumber(totalReplies)}</div>
                    <div style="font-size:0.8rem;color:var(--text-muted)"><strong style="color:var(--emerald)">${overallReplyRate}%</strong> reply rate</div>
                </div>` : ''}
            </div>

            ${this._dailySendsCard()}

            <!-- Campaign Comparison Chart -->
            <div class="card" style="margin-bottom:20px">
                <div class="card-header">
                    <h2 class="card-title">Campaign Comparison</h2>
                    <button class="btn btn-sm" onclick="Analytics.exportAllCampaigns()">Export Summary CSV</button>
                </div>
                <div class="chart-container" style="height:300px">
                    <canvas id="comparisonChart"></canvas>
                </div>
                ${this._comparisonTable()}
            </div>

            <!-- Campaign Selector & Date Range -->
            <div class="card" style="margin-bottom:20px">
                <div class="card-header">
                    <h2 class="card-title">Campaign Analytics</h2>
                </div>
                <div class="an-filter-grid">
                    <div class="form-group">
                        <label for="analyticsCampaign">Campaign</label>
                        <select id="analyticsCampaign" onchange="Analytics.onCampaignChange(this.value)">
                            ${campOptions || '<option>No campaigns</option>'}
                        </select>
                    </div>
                    <div class="form-group">
                        <label for="analyticsFrom">From</label>
                        <input type="date" id="analyticsFrom" value="${this.fromDate}" onchange="Analytics.onDateChange()">
                    </div>
                    <div class="form-group">
                        <label for="analyticsTo">To</label>
                        <input type="date" id="analyticsTo" value="${this.toDate}" onchange="Analytics.onDateChange()">
                    </div>
                    <div class="form-group an-filter-action">
                        <span class="an-quick-label">Quick range</span>
                        <div class="deliv-seg" role="group" aria-label="Quick date range">
                            ${[[7, '7d'], [30, '30d'], [90, '90d'], [0, 'All']].map(([n, l]) =>
                                `<button type="button" class="deliv-seg-btn ${this._rangeDays() === n ? 'is-active' : ''}" onclick="Analytics.quickRange(${n})">${l}</button>`).join('')}
                        </div>
                    </div>
                </div>
                <p class="field-hint" style="margin:0 0 4px">The charts and the "in range" numbers follow the dates; the large numbers are the campaign's totals.</p>
                ${selectedCamp ? `
                <div class="stats-grid" style="margin-top:12px">
                    <div class="stat-card"><div class="stat-label">Status</div><div style="margin-top:4px">${App.statusBadge(selectedCamp.status)}</div></div>
                    <div class="stat-card accent"><div class="stat-label">Sent</div><div class="stat-value">${App.formatNumber(selectedCamp.sent || 0)}</div></div>
                    ${isCold ? '' : `<div class="stat-card success"><div class="stat-label">Opens</div><div class="stat-value">${App.formatNumber(selectedCamp.views || 0)}</div><div class="stat-sub" id="anRange_views"></div></div>`}
                    ${isCold ? '' : `<div class="stat-card warning"><div class="stat-label">Clicks</div><div class="stat-value">${App.formatNumber(selectedCamp.clicks || 0)}</div><div class="stat-sub" id="anRange_clicks"></div></div>`}
                    ${isCold ? `
                    <div class="stat-card success"><div class="stat-label">Replies</div><div class="stat-value" style="color:var(--emerald)">${App.formatNumber(campReplies)}</div><div class="stat-sub"><strong style="color:var(--emerald)">${campReplyRate}%</strong> reply rate</div><div class="stat-sub" id="anRange_replies"></div></div>` : ''}
                    <div class="stat-card danger"><div class="stat-label">Bounces</div><div class="stat-value">${App.formatNumber(selectedCamp.bounces || 0)}</div><div class="stat-sub" id="anRange_bounces"></div></div>
                    <a class="stat-card stat-card-link" href="javascript:void(0)" onclick="Analytics.scrollToUnsubs()" style="border-left-color:#14b8a6"><div class="stat-label">Unsubscribes</div><div class="stat-value" style="color:#14b8a6" id="anUnsubCount">${App.formatNumber(this.campaignUnsubCount)}</div><div class="stat-sub">view records ↓</div></a>
                </div>` : ''}
            </div>

            <!-- Charts Row -->
            <div class="charts-grid">
                ${isCold ? `
                <div class="card">
                    <div class="card-header">
                        <h2 class="card-title">Replies Over Time</h2>
                        <a class="btn btn-sm" href="#/replies?campaign=${this.selectedCampaignId}">Open replies</a>
                    </div>
                    <div class="chart-container"><canvas id="repliesChart"></canvas></div>
                </div>` : `
                <div class="card">
                    <div class="card-header">
                        <h2 class="card-title">Opens Over Time</h2>
                        <button class="btn btn-sm" onclick="Analytics.exportSubscribers('views')">Export Who Opened</button>
                    </div>
                    <div class="chart-container"><canvas id="viewsChart"></canvas></div>
                </div>`}
                ${isCold ? '' : `
                <div class="card">
                    <div class="card-header">
                        <h2 class="card-title">Clicks Over Time</h2>
                        <button class="btn btn-sm" onclick="Analytics.exportSubscribers('clicks')">Export Who Clicked</button>
                    </div>
                    <div class="chart-container"><canvas id="clicksChart"></canvas></div>
                </div>`}
            </div>

            <div class="charts-grid" style="margin-top:20px">
                <div class="card">
                    <div class="card-header">
                        <h2 class="card-title">Bounces Over Time</h2>
                        <button class="btn btn-sm" onclick="Analytics.exportSubscribers('bounces')">Export Who Bounced</button>
                    </div>
                    <div class="chart-container"><canvas id="bouncesChart"></canvas></div>
                </div>
                ${isCold ? '' : `
                <div class="card">
                    <div class="card-header">
                        <h2 class="card-title">Top Links</h2>
                    </div>
                    <div class="chart-container"><canvas id="linksChart"></canvas></div>
                </div>`}
            </div>

            <!-- The selected campaign's unsubscribe records (Unsubscribes.mount) -->
            ${selectedCamp ? '<div class="card" id="anUnsubs" style="margin-top:20px;scroll-margin-top:16px"></div>' : ''}

            <!-- Links Table (click analytics, newsletter mode only) -->
            ${isCold ? '' : `
            <div class="card" style="margin-top:20px">
                <div class="card-header">
                    <h2 class="card-title">Link Click Details</h2>
                </div>
                <div id="linksTable"></div>
            </div>`}
        `;

        App.syncUrl('analytics', {
            campaign: this.selectedCampaignId, from: this.fromDate, to: this.toDate,
        });
        App.setContent(html);

        // Render comparison chart
        this.renderComparisonChart();
        this.renderDailySendsChart();

        // Auto-load analytics for selected campaign
        if (this.selectedCampaignId) {
            this.loadCampaignAnalytics();
        }
        if (selectedCamp && typeof Unsubscribes !== 'undefined') {
            Unsubscribes.mount('anUnsubs', this.selectedCampaignId).then(() => {
                if (this._scrollToUnsubs) { this._scrollToUnsubs = false; this.scrollToUnsubs(); }
            });
        }
    },

    scrollToUnsubs() {
        document.getElementById('anUnsubs')?.scrollIntoView({ behavior: 'smooth', block: 'start' });
    },

    /** Pick a campaign from the comparison table and jump to its records. */
    async openCampaignUnsubs(id) {
        this._scrollToUnsubs = true;
        await this.onCampaignChange(id);
    },

    /** A record removal or scan changed the log: refresh the two counts in place. */
    async refreshUnsubCounts() {
        const campId = this.selectedCampaignId;
        try {
            const st = await API.get(`/api/unsubscribes/stats?campaign_id=${campId || 0}`, true);
            if (App.currentPage !== 'analytics' || campId !== this.selectedCampaignId) return;
            this.unsubStats = { ...(this.unsubStats || {}), total: st.total || 0 };
            this.campaignUnsubCount = st.campaign_count || 0;
            if (campId) this.unsubByCampaign[campId] = this.campaignUnsubCount;
            const t = document.getElementById('anUnsubTotal');
            if (t) t.textContent = App.formatNumber(st.total || 0);
            const c = document.getElementById('anUnsubCount');
            if (c) c.textContent = App.formatNumber(this.campaignUnsubCount);
        } catch {
            // Counts stay as they were; the records panel itself is current.
        }
    },

    /** "Sent Today" KPI. ListMonk keeps no per-day history, so this comes
        from the app's own counter (services/send_stats). */
    _sentTodayCard() {
        const s = this.sends;
        if (!s) {
            return `
                <div class="stat-card">
                    <div class="stat-label">Sent Today</div>
                    <div class="stat-value">–</div>
                    <div style="font-size:0.8rem;color:var(--text-muted)">Daily counts unavailable</div>
                </div>`;
        }
        const days = s.days || [];
        const yesterday = days.length > 1 ? days[days.length - 2] : null;
        const sub = yesterday && yesterday.tracked
            ? `${App.formatNumber(yesterday.total)} yesterday`
            : 'counting started recently';
        return `
            <div class="stat-card accent">
                <div class="stat-label">Sent Today</div>
                <div class="stat-value">${App.formatNumber(s.sent_today || 0)}</div>
                <div style="font-size:0.8rem;color:var(--text-muted)">${App.escapeHtml(sub)}</div>
            </div>`;
    },

    _dailySendsCard() {
        const s = this.sends;
        const ranges = [7, 30, 90];
        const seg = ranges.map(n => `<button type="button" class="deliv-seg-btn ${n === this.sendsDays ? 'is-active' : ''}" aria-pressed="${n === this.sendsDays}" onclick="Analytics.setSendsRange(${n})">${n} days</button>`).join('');
        let body;
        if (!s) {
            body = '<div class="empty-state"><p>Daily send counts could not be loaded.</p></div>';
        } else {
            // The server names each campaign, including deleted ones.
            const nameOf = c => c.name || this.campaigns.find(x => x.id === c.id)?.name || `Campaign #${c.id}`;
            const split = (s.today_by_campaign || []).map(c => `
                <li style="display:flex;justify-content:space-between;gap:12px;padding:6px 0;border-bottom:1px solid var(--glass-border)">
                    <span class="cell-email" title="${App.escapeHtml(nameOf(c))}"${c.deleted ? ' style="color:var(--text-muted)"' : ''}>${App.escapeHtml(nameOf(c))}</span>
                    <strong>${App.formatNumber(c.sent)}</strong>
                </li>`).join('');
            const hasGaps = (s.days || []).some(d => !d.tracked);
            const since = s.tracking_since
                ? `Counted by this app since ${App.escapeHtml(this._dayLabel(s.tracking_since, true))}`
                : 'Counting starts with the next send';
            body = `
                <div style="display:flex;flex-wrap:wrap;gap:8px 20px;font-size:0.85rem;color:var(--text-secondary);margin-bottom:12px">
                    <span><strong style="color:var(--text-primary)">${App.formatNumber(s.period_total || 0)}</strong> sent in the last ${this.sendsDays} days</span>
                    <span>Day boundary: ${App.escapeHtml(s.timezone || 'UTC')}</span>
                </div>
                <div class="chart-container" style="height:260px"><canvas id="dailySendsChart"></canvas></div>
                <p style="font-size:0.78rem;color:var(--text-muted);margin-top:10px">
                    ${since}. ListMonk stores only a running total per campaign, so days before that${hasGaps ? ' (shown without a bar)' : ''} are unknown, not zero.
                </p>
                ${split ? `<div style="margin-top:14px"><div class="stat-label" style="margin-bottom:6px">Today by campaign</div><ul style="list-style:none;margin:0;padding:0;font-size:0.85rem">${split}</ul></div>` : ''}`;
        }
        return `
            <div class="card" style="margin-bottom:20px">
                <div class="card-header">
                    <h2 class="card-title">Daily Sends</h2>
                    <div class="deliv-seg" role="group" aria-label="Days shown">${seg}</div>
                </div>
                ${body}
            </div>`;
    },

    /** "Nov 4" (or "Nov 4, 2026") for an ISO calendar date. Parsed as local
        parts: new Date('2026-11-04') is UTC midnight and can show Nov 3. */
    _dayLabel(iso, withYear = false) {
        const [y, m, d] = String(iso).split('-').map(Number);
        const opts = withYear ? { year: 'numeric', month: 'short', day: 'numeric' } : { month: 'short', day: 'numeric' };
        return new Date(y, m - 1, d).toLocaleDateString(undefined, opts);
    },

    async setSendsRange(days) {
        if (days === this.sendsDays) return;
        this.sendsDays = days;
        try {
            this.sends = await API.get(`/api/sends/daily?days=${days}`);
        } catch {
            return; // API.request already surfaced the server message.
        }
        if (this._seq !== undefined && App.stale(this._seq)) return;
        this.renderPage();
    },

    renderDailySendsChart() {
        const ctx = document.getElementById('dailySendsChart');
        if (!ctx || !this.sends) return;
        const theme = this.getThemeColors();
        const days = this.sends.days || [];
        const labels = days.map(d => this._dayLabel(d.date));
        // Untracked days get no bar (null), so "unknown" never reads as zero.
        const data = days.map(d => (d.tracked ? d.total : null));
        const today = days.length - 1;
        if (this.charts.daily) this.charts.daily.destroy();
        this.charts.daily = new Chart(ctx, {
            type: 'bar',
            data: {
                labels,
                datasets: [{
                    label: 'Sent',
                    data,
                    backgroundColor: days.map((_, i) => (i === today ? 'rgba(16, 185, 129, 0.85)' : 'rgba(83, 58, 253, 0.82)')),
                    borderRadius: 6,
                    maxBarThickness: 36,
                }],
            },
            options: {
                responsive: true,
                maintainAspectRatio: false,
                plugins: {
                    legend: { display: false },
                    tooltip: {
                        backgroundColor: theme.tooltipBg,
                        titleColor: theme.tooltipText,
                        bodyColor: theme.tooltipText,
                        borderColor: theme.tooltipBorder,
                        borderWidth: 1,
                        padding: 12,
                        callbacks: {
                            label: (item) => {
                                const d = days[item.dataIndex];
                                if (!d.tracked) return 'Not tracked';
                                return `${App.formatNumber(d.total)} sent${item.dataIndex === today ? ' (today so far)' : ''}`;
                            },
                        },
                    },
                },
                scales: {
                    x: { ticks: { color: theme.textColor, maxRotation: 0, autoSkip: true, maxTicksLimit: 12 }, grid: { display: false } },
                    y: { beginAtZero: true, ticks: { color: theme.textColor, precision: 0 }, grid: { color: theme.gridColor } },
                },
            },
        });
    },

    /** Campaigns side by side: the numbers behind the comparison chart,
        with the rates the chart cannot show. */
    _comparisonTable() {
        const rows = this.campaigns.filter(c => (c.sent || 0) > 0 || ['running', 'paused'].includes(c.status));
        if (!rows.length) return '';
        const isCold = this.isColdMode();
        const pct = (n, d) => d ? `${(n / d * 100).toFixed(1)}%` : '–';
        const body = rows.map(c => {
            const sent = c.sent || 0;
            const replies = this.replyByCampaign?.[String(c.id)] || 0;
            const unsubs = this.unsubByCampaign?.[c.id] || 0;
            return `<tr>
                <td><a class="cell-link cell-clamp cell-clamp-lg" href="javascript:void(0)" onclick="Analytics.onCampaignChange(${c.id});document.getElementById('analyticsCampaign')?.scrollIntoView({behavior:'smooth',block:'center'})" title="${App.escapeHtml(c.name)}">${App.escapeHtml(c.name)}</a></td>
                <td>${App.statusBadge(c.status)}</td>
                <td class="num">${App.formatNumber(sent)}</td>
                ${isCold
                    ? `<td class="num">${App.formatNumber(replies)}</td><td class="num">${pct(replies, sent)}</td>`
                    : `<td class="num">${App.formatNumber(c.views || 0)}</td><td class="num">${pct(c.views || 0, sent)}</td><td class="num">${pct(c.clicks || 0, sent)}</td>`}
                <td class="num">${App.formatNumber(c.bounces || 0)}</td><td class="num">${pct(c.bounces || 0, sent)}</td>
                <td class="num">${unsubs ? `<a class="cell-link" href="javascript:void(0)" onclick="Analytics.openCampaignUnsubs(${c.id})" title="Show these unsubscribes">${App.formatNumber(unsubs)}</a>` : '0'}</td>
            </tr>`;
        }).join('');
        return `<div class="table-wrapper an-compare" tabindex="0" role="region" aria-label="Campaign comparison table" style="margin-top:16px"><table>
            <thead><tr><th>Campaign</th><th>Status</th><th class="num">Sent</th>
                ${isCold ? '<th class="num">Replies</th><th class="num">Reply %</th>' : '<th class="num">Opens</th><th class="num">Open %</th><th class="num">CTR</th>'}
                <th class="num">Bounces</th><th class="num">Bounce %</th><th class="num">Unsubs</th></tr></thead>
            <tbody>${body}</tbody></table></div>`;
    },

    _localDay(d) {
        return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`;
    },

    /** Which quick range the current dates match (7/30/90), 0 for "All"
        (from the campaign's creation), or null for a custom range. */
    _rangeDays() {
        const today = this._localDay(new Date());
        if (this.toDate !== today) return null;
        for (const n of [7, 30, 90]) {
            const d = new Date(); d.setDate(d.getDate() - n);
            if (this.fromDate === this._localDay(d)) return n;
        }
        const camp = this.campaigns.find(c => c.id === this.selectedCampaignId);
        if (camp?.created_at && this.fromDate === this._localDay(new Date(camp.created_at))) return 0;
        return null;
    },

    quickRange(days) {
        const now = new Date();
        let from;
        if (days) { from = new Date(now); from.setDate(from.getDate() - days); }
        else {
            const camp = this.campaigns.find(c => c.id === this.selectedCampaignId);
            from = camp?.created_at ? new Date(camp.created_at) : new Date(now.getFullYear() - 1, now.getMonth(), now.getDate());
        }
        this.fromDate = this._localDay(from);
        this.toDate = this._localDay(now);
        const f = document.getElementById('analyticsFrom');
        const t = document.getElementById('analyticsTo');
        if (f) f.value = this.fromDate;
        if (t) t.value = this.toDate;
        this._syncQuickRange();
        this.loadCampaignAnalytics();
    },

    _syncQuickRange() {
        const active = this._rangeDays();
        document.querySelectorAll('.an-filter-action .deliv-seg-btn').forEach((b, i) => {
            b.classList.toggle('is-active', [7, 30, 90, 0][i] === active);
        });
        App.syncUrl('analytics', { campaign: this.selectedCampaignId, from: this.fromDate, to: this.toDate });
    },

    renderComparisonChart() {
        const ctx = document.getElementById('comparisonChart');
        if (!ctx) return;
        if (!this.campaigns.some(c => (c.sent || 0) > 0)) {
            if (this.charts.comparison) { this.charts.comparison.destroy(); this.charts.comparison = null; }
            this._emptyChart('comparisonChart', 'No campaign has sent anything yet.', '#/campaigns', 'Go to campaigns');
            return;
        }

        const theme = this.getThemeColors();
        const sorted = [...this.campaigns].reverse();
        const labels = sorted.map(c => App.truncateLabel(c.name, 22));

        // All-zero series draw no bars but still occupy a legend entry, which
        // read as a phantom series ("Views", "Bounces") on every chart.
        const hasData = (arr) => arr.some(v => Number(v) > 0);

        const datasets = [
            { label: 'Sent', data: sorted.map(c => c.sent || 0), backgroundColor: 'rgba(83, 58, 253, 0.82)', borderRadius: 6 },
        ];
        const views = sorted.map(c => c.views || 0);
        if (hasData(views)) {
            datasets.push({ label: 'Views', data: views, backgroundColor: 'rgba(16, 185, 129, 0.82)', borderRadius: 6 });
        }

        // Click tracking is off in cold email mode, so the Clicks series is
        // replaced by per-campaign prospect replies.
        if (!this.isColdMode()) {
            const clicks = sorted.map(c => c.clicks || 0);
            if (hasData(clicks)) {
                datasets.push({ label: 'Clicks', data: clicks, backgroundColor: 'rgba(245, 158, 11, 0.82)', borderRadius: 6 });
            }
        }
        const bounces = sorted.map(c => c.bounces || 0);
        if (hasData(bounces)) {
            datasets.push({ label: 'Bounces', data: bounces, backgroundColor: 'rgba(234, 34, 97, 0.82)', borderRadius: 6 });
        }
        if (this.isColdMode()) {
            const replies = sorted.map(c => this.replyByCampaign?.[String(c.id)] || 0);
            if (hasData(replies)) {
                datasets.push({
                    label: 'Replies',
                    data: replies,
                    backgroundColor: 'rgba(20, 184, 166, 0.82)',
                    borderRadius: 6,
                });
            }
        }

        if (this.charts.comparison) this.charts.comparison.destroy();
        this.charts.comparison = new Chart(ctx, {
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
                            padding: 16,
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
                scales: {
                    x: {
                        ticks: { color: theme.textColor, maxRotation: 45, font: { family: 'Plus Jakarta Sans', size: 11 } },
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

    async onCampaignChange(val) {
        this.selectedCampaignId = parseInt(val);
        App.bumpSeq();
        await this.loadCampaignSideStats();
        this.renderPage();
    },

    // Per-campaign unsubscribe count plus (cold email only) reply stats, which
    // the campaign stat cards render alongside sent/views/clicks.
    async loadCampaignSideStats() {
        const seq = ++this._sideSeq;
        if (!this.selectedCampaignId) {
            this.campaignUnsubCount = 0;
            this.campaignReplyStats = null;
            return;
        }

        const requests = [
            API.get(`/api/unsubscribes/stats?campaign_id=${this.selectedCampaignId}`),
        ];
        if (this.isColdMode()) {
            requests.push(API.get(`/api/replies/stats?campaign_id=${this.selectedCampaignId}`));
        }

        const [unsubRes, replyRes] = await Promise.allSettled(requests);
        if (seq !== this._sideSeq) return;
        this.campaignUnsubCount = unsubRes.status === 'fulfilled' ? (unsubRes.value?.campaign_count || 0) : 0;
        this.campaignReplyStats = replyRes?.status === 'fulfilled' ? (replyRes.value || null) : null;
    },

    /** Dates apply as soon as they change (debounced while typing): a
        separate Load button was easy to miss and left stale charts up. */
    onDateChange() {
        this.fromDate = document.getElementById('analyticsFrom').value;
        this.toDate = document.getElementById('analyticsTo').value;
        if (!this.fromDate || !this.toDate) return;
        if (this.fromDate > this.toDate) { App.toast('"From" must be on or before "To"', 'error'); return; }
        clearTimeout(this._dateTimer);
        this._dateTimer = setTimeout(() => { this._syncQuickRange(); this.loadCampaignAnalytics(); }, 400);
    },

    _emptyChart(canvasId, message, href = '', linkText = '') {
        const canvas = document.getElementById(canvasId);
        const box = canvas?.parentElement;
        if (!box) return;
        box.classList.add('has-chart-empty');
        box.querySelector('.chart-empty')?.remove();
        box.insertAdjacentHTML('beforeend', App.emptyChart(message, href, linkText));
    },

    _clearEmptyChart(canvasId) {
        const box = document.getElementById(canvasId)?.parentElement;
        if (!box) return;
        box.classList.remove('has-chart-empty');
        box.querySelector('.chart-empty')?.remove();
    },

    _setRange(key, n) {
        const el = document.getElementById(`anRange_${key}`);
        if (el) el.textContent = n === null ? '' : `${App.formatNumber(n)} in the selected range`;
    },

    async loadCampaignAnalytics() {
        const seq = ++this._chartSeq;
        this.fromDate = document.getElementById('analyticsFrom')?.value || this.fromDate;
        this.toDate = document.getElementById('analyticsTo')?.value || this.toDate;

        if (!this.selectedCampaignId) {
            App.toast('Select a campaign first', 'error');
            return;
        }

        // Selection identity: picking A then quickly B lets A's slower
        // responses resolve last and repaint the charts under B's selector.
        const campId = this.selectedCampaignId;
        const baseParams = `campaign_id=${campId}&from_date=${this.fromDate}&to_date=${this.toDate}`;

        // Fetch the analytics types that are actually displayed. Cold email
        // mode hides click/link analytics (tracking is disabled there), so
        // those queries are skipped instead of being drawn into nothing.
        const isCold = this.isColdMode();
        const tz = Intl.DateTimeFormat().resolvedOptions().timeZone || 'UTC';
        const [viewsRes, clicksRes, bouncesRes, linksRes, repliesRes] = await Promise.allSettled([
            isCold ? null : API.get(`/api/campaigns/analytics/views?${baseParams}`),
            isCold ? null : API.get(`/api/campaigns/analytics/clicks?${baseParams}`),
            API.get(`/api/campaigns/analytics/bounces?${baseParams}`),
            isCold ? null : API.get(`/api/campaigns/analytics/links?${baseParams}`),
            isCold ? API.get(`/api/replies/timeline?campaign_id=${campId}&from_date=${this.fromDate}&to_date=${this.toDate}&tz=${encodeURIComponent(tz)}`, true) : null,
        ]);
        if (campId !== this.selectedCampaignId) return;
        if (seq !== this._chartSeq) return;
        // The theme-change listener can trigger a reload; if the user has
        // navigated away in the meantime, don't paint on whatever page is up.
        if (App.currentPage !== 'analytics') return;

        const sum = (arr) => arr.reduce((a, d) => a + (d.count || 0), 0);
        const bouncesData = bouncesRes.status === 'fulfilled' ? (bouncesRes.value?.data || []) : [];
        this.renderTimeChart('bouncesChart', 'bounces', bouncesData, '#ea2261', 'rgba(234,34,97,0.12)');
        this._setRange('bounces', bouncesRes.status === 'fulfilled' ? sum(bouncesData) : null);

        if (isCold) {
            const ok = repliesRes.status === 'fulfilled';
            // Reply days are calendar dates already; the chart's label code
            // reads them as local days.
            const repliesData = ok ? (repliesRes.value?.data || []).map(d => ({ timestamp: d.date, count: d.count })) : [];
            this.renderTimeChart('repliesChart', 'replies', repliesData, '#14b8a6', 'rgba(20,184,166,0.14)');
            this._setRange('replies', ok ? sum(repliesData) : null);
        } else {
            const viewsData = viewsRes.status === 'fulfilled' ? (viewsRes.value?.data || []) : [];
            const clicksData = clicksRes.status === 'fulfilled' ? (clicksRes.value?.data || []) : [];
            const linksData = linksRes.status === 'fulfilled' ? (linksRes.value?.data || []) : [];
            this.renderTimeChart('viewsChart', 'views', viewsData, '#10b981', 'rgba(16,185,129,0.12)');
            this.renderTimeChart('clicksChart', 'clicks', clicksData, '#f59e0b', 'rgba(245,158,11,0.12)');
            this._setRange('views', viewsRes.status === 'fulfilled' ? sum(viewsData) : null);
            this._setRange('clicks', clicksRes.status === 'fulfilled' ? sum(clicksData) : null);
            this.renderLinksChart(linksData);
            this.renderLinksTable(linksData);
        }
    },

    renderTimeChart(canvasId, key, data, borderColor, bgColor) {
        const ctx = document.getElementById(canvasId);
        if (!ctx) return;

        const theme = this.getThemeColors();
        if (this.charts[key]) { this.charts[key].destroy(); this.charts[key] = null; }

        if (!data.length) {
            const what = { views: 'opens', clicks: 'clicks', bounces: 'bounces', replies: 'replies' }[key] || key;
            this._emptyChart(canvasId, `No ${what} in the selected date range.`);
            return;
        }
        this._clearEmptyChart(canvasId);

        // ListMonk buckets hourly for ranges under a week and daily
        // otherwise. Day labels alone turned an hourly series into a run of
        // identical "Oct 26" ticks, so several buckets on one calendar day
        // get their time of day too.
        const days = data.map(d => String(d.timestamp).slice(0, 10));
        const hourly = new Set(days).size < days.length;
        const labels = data.map((d, i) => {
            if (hourly) {
                const at = new Date(d.timestamp);
                if (!Number.isNaN(at.getTime())) {
                    return at.toLocaleString(undefined, { month: 'short', day: 'numeric', hour: 'numeric' });
                }
            }
            // ListMonk's day buckets are UTC-midnight instants; parsing them
            // as such shifts every label one day early west of UTC. Treat the
            // date part as a LOCAL calendar day instead.
            const date = new Date(`${days[i]}T00:00:00`);
            return date.toLocaleDateString(undefined, { month: 'short', day: 'numeric' });
        });
        const values = data.map(d => d.count || 0);

        this.charts[key] = new Chart(ctx, {
            type: 'line',
            data: {
                labels,
                datasets: [{
                    label: key.charAt(0).toUpperCase() + key.slice(1),
                    data: values,
                    borderColor,
                    backgroundColor: bgColor,
                    fill: true,
                    tension: 0.35,
                    borderWidth: 2.5,
                    pointRadius: 4,
                    pointHoverRadius: 7,
                    pointBackgroundColor: borderColor,
                }],
            },
            options: {
                responsive: true,
                maintainAspectRatio: false,
                plugins: {
                    legend: {
                        labels: {
                            color: theme.textColor,
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

    renderLinksChart(data) {
        const ctx = document.getElementById('linksChart');
        if (!ctx) return;

        const theme = this.getThemeColors();
        if (this.charts.links) { this.charts.links.destroy(); this.charts.links = null; }

        if (!data.length) {
            this._emptyChart('linksChart', 'No link clicks in the selected date range.');
            return;
        }
        this._clearEmptyChart('linksChart');

        // Top 10 links
        const top = data.slice(0, 10);
        const labels = top.map(d => {
            try {
                const url = new URL(d.url);
                return App.truncateLabel(url.pathname || url.hostname, 30);
            } catch {
                return App.truncateLabel(d.url || '?', 30);
            }
        });
        const values = top.map(d => d.count || 0);

        this.charts.links = new Chart(ctx, {
            type: 'bar',
            data: {
                labels,
                datasets: [{
                    label: 'Clicks',
                    data: values,
                    backgroundColor: 'rgba(83, 58, 253, 0.82)',
                    borderRadius: 6,
                }],
            },
            options: {
                responsive: true,
                maintainAspectRatio: false,
                indexAxis: 'y',
                plugins: {
                    legend: { display: false },
                    tooltip: {
                        backgroundColor: theme.tooltipBg,
                        titleColor: theme.tooltipText,
                        bodyColor: theme.tooltipText,
                        borderColor: theme.tooltipBorder,
                        borderWidth: 1,
                        padding: 12,
                        cornerRadius: 10,
                    },
                },
                scales: {
                    x: {
                        ticks: { color: theme.textColor, font: { family: 'Plus Jakarta Sans', size: 11 } },
                        grid: { color: theme.gridColor, drawBorder: false },
                        beginAtZero: true,
                    },
                    y: {
                        ticks: { color: theme.textColor, font: { family: 'Plus Jakarta Sans', size: 11 } },
                        grid: { display: false },
                    },
                },
            },
        });
    },

    renderLinksTable(data) {
        const el = document.getElementById('linksTable');
        if (!el) return;

        if (!data.length) {
            el.innerHTML = '<p class="an-empty">No link data available</p>';
            return;
        }

        let html = '<div class="table-wrapper" tabindex="0" role="region" aria-label="Scrollable data table"><table><thead><tr><th>#</th><th>URL</th><th>Clicks</th></tr></thead><tbody>';
        data.forEach((d, i) => {
            const safeUrl = App.escapeHtml(d.url);
            // --primary-soft, not --accent: the raw indigo is a CTA fill and
            // only reaches 3.0:1 as text on the dark card.
            const isSafeUrl = d.url && /^https?:\/\//i.test(d.url);
            html += `<tr>
                <td class="an-rank">${i + 1}</td>
                <td class="an-url-cell">
                    ${isSafeUrl ? `<a href="${safeUrl}" target="_blank" rel="noopener" class="an-link">${safeUrl}</a>` : `<span>${safeUrl}</span>`}
                </td>
                <td><strong>${App.formatNumber(d.count || 0)}</strong></td>
            </tr>`;
        });
        html += '</tbody></table></div>';
        el.innerHTML = html;
    },

    // ── Export Functions ──────────────────────────────────

    // Export subscriber list (who opened / who clicked / who bounced).
    // The From/To range travels with the request so the CSV covers exactly
    // the window the charts beside it are showing.
    async exportSubscribers(type) {
        const campId = this.selectedCampaignId;
        if (!campId) { App.toast('Select a campaign first', 'error'); return; }

        const labels = { views: 'who opened', clicks: 'who clicked', bounces: 'who bounced' };
        App.toast(`Exporting ${labels[type] || type}… this may take a moment`, 'info');

        try {
            const params = new URLSearchParams();
            if (this.fromDate) params.set('from_date', this.fromDate);
            if (this.toDate) params.set('to_date', this.toDate);
            const qs = params.toString();
            const result = await API.download(`/api/campaigns/${campId}/subscribers/${type}/export${qs ? '?' + qs : ''}`);
            if (result.blob) {
                const campName = this.campaigns.find(c => c.id === campId)?.name || 'campaign';
                const safeName = campName.replace(/[^a-zA-Z0-9]/g, '_').substring(0, 30);
                API.downloadBlob(result.blob, `${safeName}_${type}_subscribers.csv`);
                App.toast(`${labels[type] || type} list exported`, 'success');
            }
        } catch {
            // API.request already surfaced the server message.
        }
    },

    // Export all campaigns summary (comparison chart data)
    async exportAllCampaigns() {
        try {
            const result = await API.download('/api/campaigns/export-all');
            if (result.blob) {
                API.downloadBlob(result.blob, 'all_campaigns_summary.csv');
                App.toast('Campaigns summary exported', 'success');
            }
        } catch {
            // API.request already surfaced the server message.
        }
    },
};

// Listen to theme switch to dynamically update analytics charts
window.addEventListener('themeChanged', () => {
    if (App.currentPage === 'analytics') {
        Analytics.renderComparisonChart();
        if (Analytics.selectedCampaignId) {
            Analytics.loadCampaignAnalytics();
        }
    }
});


document.addEventListener('unsubscribes:changed', () => {
    if (App.currentPage === 'analytics') Analytics.refreshUnsubCounts();
});
