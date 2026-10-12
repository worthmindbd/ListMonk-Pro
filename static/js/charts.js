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
        App.setContent('<div class="loading-spinner">Loading dashboard...</div>');

        const isCold = App.currentMode === 'cold_email';

        try {
            // Fetch all data in parallel including reply stats and deliverability if in cold email mode
            const requests = [
                API.get('/api/lists?per_page=1&minimal=true'),
                API.get('/api/campaigns?per_page=10&order_by=created_at&order=DESC'),
                API.get('/api/subscribers?per_page=1'),
                API.get('/api/campaigns/summary'),
            ];

            if (isCold) {
                requests.push(API.get('/api/replies/stats'));
                requests.push(API.get('/api/deliverability/checklist'));
                requests.push(API.get('/api/replies?per_page=5'));
            }

            const results = await Promise.allSettled(requests);
            const [listsRes, campaignsRes, subscribersRes, summaryRes] = results;
            const replyStatsRes = isCold ? results[4] : null;
            const delivRes = isCold ? results[5] : null;
            const recentRepliesRes = isCold ? results[6] : null;

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
                const deliv = delivRes?.status === 'fulfilled' ? delivRes.value : {};
                const recentReplies = recentRepliesRes?.status === 'fulfilled' ? (recentRepliesRes.value?.results || []) : [];
                const delivScore = deliv?.score ?? 100;
                const totalReplies = repStats.total_replies || 0;
                const replyRate = repStats.reply_rate || 0;
                const positiveLeads = repStats.positive || 0;

                html += `
                    <!-- Deliverability Status Strip -->
                    <div class="card deliverability-status-strip ${delivScore === 100 ? 'status-optimal' : 'status-tuning'}" style="margin-bottom:20px;padding:16px 20px;display:flex;align-items:center;justify-content:space-between;flex-wrap:wrap;gap:12px;">
                        <div style="display:flex;align-items:center;gap:14px;">
                            <span class="deliverability-icon-badge" style="display:inline-flex;align-items:center;justify-content:center;width:38px;height:38px;border-radius:10px;background:${delivScore === 100 ? 'rgba(16,185,129,0.15)' : 'rgba(245,158,11,0.15)'};color:${delivScore === 100 ? 'var(--emerald)' : 'var(--amber)'}">
                                ${delivScore === 100 
                                    ? '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="20" height="20"><path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z"/><polyline points="9 12 11 14 15 10"/></svg>'
                                    : '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="20" height="20"><path d="M10.29 3.86L1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z"/><line x1="12" y1="9" x2="12" y2="13"/><line x1="12" y1="17" x2="12.01" y2="17"/></svg>'}
                            </span>
                            <div>
                                <strong style="color:var(--text-primary);font-size:1rem">Deliverability Health: ${delivScore}% ${delivScore === 100 ? '(Optimal for Cold Outreach)' : '(Action Recommended)'}</strong>
                                <div style="font-size:0.82rem;color:var(--text-secondary);margin-top:2px">Zero tracking pixels • RFC List-Unsubscribe header omitted • Humanized drip batching</div>
                            </div>
                        </div>
                        <a href="#/deliverability" class="btn btn-sm btn-secondary">Audit & Presets →</a>
                    </div>

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
                                <h3 class="card-title">Outbound Volume per Campaign</h3>
                            </div>
                            <div class="chart-container">
                                <canvas id="campaignChart"></canvas>
                            </div>
                        </div>
                        <div class="card">
                            <div class="card-header">
                                <h3 class="card-title">Prospect Sentiment Distribution</h3>
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
                                <h3 class="card-title">Recent Prospect Replies</h3>
                                <a href="#/replies" class="btn btn-sm">View Inbox Feed →</a>
                            </div>
                            <div class="replies-compact-list" style="padding:10px 0;">`;

                if (!recentReplies.length) {
                    html += `<div style="text-align:center;padding:32px 16px;color:var(--text-muted);font-size:0.9rem">No replies recorded yet. Click <a href="#/replies" style="color:var(--primary)">Scan Mailbox</a> to sync incoming replies.</div>`;
                } else {
                    recentReplies.slice(0, 4).forEach(r => {
                        const sentMap = {
                            positive: { label: 'Interested', cls: 'badge-success', dotCls: 'badge-dot-positive' },
                            question: { label: 'Question', cls: 'badge-warning', dotCls: 'badge-dot-question' },
                            auto_reply: { label: 'Auto-Reply', cls: 'badge-secondary', dotCls: 'badge-dot-auto-reply' },
                            negative: { label: 'Not Interested', cls: 'badge-danger', dotCls: 'badge-dot-negative' },
                        };
                        const sent = sentMap[r.sentiment] || { label: r.sentiment || 'Reply', cls: 'badge-secondary', dotCls: 'badge-dot-auto-reply' };
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
                                <h3 class="card-title">Recent Outreach Campaigns</h3>
                                <a href="#/campaigns" class="btn btn-sm">All Campaigns →</a>
                            </div>
                            <div class="table-wrapper" style="border:none;box-shadow:none;background:transparent">
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

                App.setContent(html);

                // Render cold charts
                this.renderCampaignChart(campaigns);
                this.renderColdSentimentChart(repStats);

            } else {
                // Newsletter Mode
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
                                <h3 class="card-title">Campaign Performance</h3>
                            </div>
                            <div class="chart-container">
                                <canvas id="campaignChart"></canvas>
                            </div>
                        </div>
                        <div class="card">
                            <div class="card-header">
                                <h3 class="card-title">Campaign Status Distribution</h3>
                            </div>
                            <div class="chart-container">
                                <canvas id="statusChart"></canvas>
                            </div>
                        </div>
                    </div>

                    <div class="card" style="margin-top:24px">
                        <div class="card-header">
                            <h3 class="card-title">Recent Campaigns</h3>
                            <a href="#/campaigns" class="btn btn-sm">View All</a>
                        </div>
                        <div class="table-wrapper" style="border:none;box-shadow:none;background:transparent">
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
                App.setContent(html);

                this.renderCampaignChart(campaigns);
                this.renderStatusChart(campaigns);
            }

        } catch (err) {
            App.setContent(`<div class="empty-state"><h3>Failed to load dashboard</h3><p>${App.escapeHtml(err.message)}</p></div>`);
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
        const question = repStats?.question || 0;
        const autoReply = repStats?.auto_reply || 0;
        const negative = repStats?.negative || 0;

        const labels = ['Positive / Interested', 'Questions', 'Out of Office', 'Not Interested'];
        let data = [positive, question, autoReply, negative];

        // If no data, show placeholder empty chart
        const total = data.reduce((a, b) => a + b, 0);
        if (total === 0) {
            labels.push('Awaiting Replies');
            data = [0, 0, 0, 0, 1];
        }

        const bgColors = ['#10b981', '#f59e0b', '#64748b', '#f43f5e', '#334155'];

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

// Listen to theme switch to dynamically update charts
window.addEventListener('themeChanged', () => {
    if (App.currentPage === 'dashboard') {
        Dashboard.renderCampaignChart();
        Dashboard.renderStatusChart();
    }
});

