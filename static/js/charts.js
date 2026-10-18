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

    async render() {
        const seq = App.seq();
        App.setContent('<div class="loading-spinner">Loading dashboard...</div>');

        const isCold = App.currentMode === 'cold_email';

        try {
            // Fetch all data in parallel including reply stats if in cold email mode
            const requests = [
                API.get('/api/lists?per_page=1&minimal=true'),
                API.get('/api/campaigns?per_page=10&order_by=created_at&order=DESC'),
                API.get('/api/subscribers?per_page=1'),
                API.get('/api/campaigns/summary'),
            ];

            if (isCold) {
                requests.push(API.get('/api/replies/stats'));
                requests.push(API.get('/api/replies?per_page=5'));
            }

            const results = await Promise.allSettled(requests);
            const [listsRes, campaignsRes, subscribersRes, summaryRes] = results;
            const replyStatsRes = isCold ? results[4] : null;
            const recentRepliesRes = isCold ? results[5] : null;

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
                const totalReplies = repStats.total_replies || 0;
                const replyRate = repStats.reply_rate || 0;
                const positiveLeads = repStats.positive || 0;

                html += `
                    <!-- Cold Email KPI Cards -->
                    <div class="stats-grid">
                        <div class="stat-card accent">
                            <div class="stat-label">Total Leads</div>
                            <div class="stat-value">${App.formatNumber(totalSubscribers)}</div>
                            <div style="font-size:0.82rem;color:var(--text-muted)">Across ${totalLists} lead lists</div>
                        </div>
                        <div class="stat-card info">
                            <div class="stat-label">Outbound Dispatched</div>
                            <div class="stat-value">${App.formatNumber(totalSent)}</div>
                            <div style="font-size:0.82rem;color:var(--text-muted)">${totalCampaigns} total campaigns</div>
                        </div>
                        <div class="stat-card success">
                            <div class="stat-label">Prospect Replies</div>
                            <div class="stat-value" style="color:var(--emerald)">${App.formatNumber(totalReplies)}</div>
                            <div style="font-size:0.82rem;color:var(--text-muted)"><strong style="color:var(--emerald)">${replyRate}%</strong> reply rate</div>
                        </div>
                        <div class="stat-card warning">
                            <div class="stat-label"><span class="badge-dot badge-dot-positive"></span> Hot Leads / Interested</div>
                            <div class="stat-value" style="color:var(--amber)">${App.formatNumber(positiveLeads)}</div>
                            <div style="font-size:0.82rem;color:var(--text-muted)">Positive meeting & call intent</div>
                        </div>
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
                                        <th>Campaign</th><th>Status</th><th>Dispatched</th>
                                    </tr></thead>
                                    <tbody>`;

                campaigns.slice(0, 4).forEach(c => {
                    html += `<tr style="cursor:pointer" onclick="Campaigns.showDetail(${c.id})">
                        <td><strong>${App.escapeHtml(c.name)}</strong></td>
                        <td>${App.statusBadge(c.status)}</td>
                        <td>${App.formatNumber(c.sent || c.to_send || 0)}</td>
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

            } else {                // Newsletter Mode
                html += `
                    <div class="stats-grid">
                        <div class="stat-card accent">
                            <div class="stat-label">Total Subscribers</div>
                            <div class="stat-value">${App.formatNumber(totalSubscribers)}</div>
                        </div>
                        <div class="stat-card success">
                            <div class="stat-label">Total Lists</div>
                            <div class="stat-value">${App.formatNumber(totalLists)}</div>
                        </div>
                        <div class="stat-card warning">
                            <div class="stat-label">Total Campaigns</div>
                            <div class="stat-value">${App.formatNumber(totalCampaigns)}</div>
                        </div>
                        <div class="stat-card info">
                            <div class="stat-label">Active Campaigns</div>
                            <div class="stat-value">${App.formatNumber(activeCampaigns ?? campaigns.filter(c => c.status === 'running').length)}</div>
                        </div>
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
            }

        } catch (err) {
            if (App.stale(seq)) return;
            App.setContent(`<div class="empty-state"><h2>Failed to load dashboard</h2><p>${App.escapeHtml(err.message)}</p></div>`);
        }
    },

    renderCampaignChart(campaigns) {
        const ctx = document.getElementById('campaignChart');
        if (!ctx) return;

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

        const labels = Object.keys(statusCounts);
        const data = Object.values(statusCounts);
        const bgColors = labels.map(s => colorMap[s] || '#64748b');

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
        const autoReply = repStats?.auto_reply || 0;
        const negative = repStats?.negative || 0;

        const labels = ['Positive / Interested', 'Out of Office', 'Not Interested'];
        let data = [positive, autoReply, negative];

        // If no data, show placeholder empty chart
        const total = data.reduce((a, b) => a + b, 0);
        if (total === 0) {
            labels.push('Awaiting Replies');
            data = [0, 0, 0, 1];
        }

        const bgColors = ['#10b981', '#64748b', '#f43f5e', '#334155'];

        if (this.charts.status) this.charts.status.destroy();
        this.charts.status = new Chart(ctx, {
            type: 'doughnut',
            data: {
                labels: total === 0 ? ['Awaiting Replies'] : labels,
                datasets: [{
                    data: total === 0 ? [1] : data,
                    backgroundColor: total === 0 ? ['#334155'] : bgColors,
                    // No separator when the ring is a single slice (see above).
                    borderWidth: (total === 0 ? [1] : data).filter(v => Number(v) > 0).length > 1 ? 2 : 0,
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

