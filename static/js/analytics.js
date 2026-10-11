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

    async render() {
        // Set default date range: last 30 days
        const now = new Date();
        const thirtyDaysAgo = new Date(now);
        thirtyDaysAgo.setDate(thirtyDaysAgo.getDate() - 30);
        if (!this.fromDate) this.fromDate = thirtyDaysAgo.toISOString().split('T')[0];
        if (!this.toDate) this.toDate = now.toISOString().split('T')[0];

        App.setContent('<div class="loading-spinner">Loading analytics...</div>');

        // Fetch campaigns, unsubscribe stats, and accurate totals in parallel
        try {
            const [campRes, unsubRes, summaryRes] = await Promise.allSettled([
                API.get('/api/campaigns?per_page=100&order_by=created_at&order=DESC'),
                API.get('/api/unsubscribes/stats'),
                API.get('/api/campaigns/summary'),
            ]);
            this.campaigns = campRes.status === 'fulfilled' ? (campRes.value?.data?.results || []) : [];
            this.unsubStats = unsubRes.status === 'fulfilled' ? unsubRes.value : { total: 0 };
            this.summary = summaryRes.status === 'fulfilled' ? (summaryRes.value || null) : null;

            // Default to first campaign if none selected
            if (!this.selectedCampaignId && this.campaigns.length) {
                this.selectedCampaignId = this.campaigns[0].id;
            }

            // Fetch per-campaign unsubscribe count
            await this.loadCampaignUnsubCount();
            this.renderPage();
        } catch (err) {
            console.error('Analytics load error:', err);
            this.campaigns = [];
            this.unsubStats = { total: 0 };
            App.setContent(`<div class="empty-state"><h3>Failed to load analytics</h3><p>${App.escapeHtml(err?.message || '')}</p></div>`);
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
        const campOptions = this.campaigns.map(c =>
            `<option value="${c.id}" ${c.id === this.selectedCampaignId ? 'selected' : ''}>${App.escapeHtml(c.name)} (${c.status})</option>`
        ).join('');

        // Build the overview stats. Prefer the all-campaigns summary endpoint;
        // fall back to the loaded page only if it is unavailable.
        const selectedCamp = this.campaigns.find(c => c.id === this.selectedCampaignId);
        const totalSent = this.summary?.sent ?? this.campaigns.reduce((sum, c) => sum + (c.sent || 0), 0);
        const totalViews = this.summary?.views ?? this.campaigns.reduce((sum, c) => sum + (c.views || 0), 0);
        const totalClicks = this.summary?.clicks ?? this.campaigns.reduce((sum, c) => sum + (c.clicks || 0), 0);
        const totalBounces = this.summary?.bounces ?? this.campaigns.reduce((sum, c) => sum + (c.bounces || 0), 0);
        const totalUnsubs = this.unsubStats?.total || 0;

        let html = `
            <!-- Overview Stats -->
            <div class="stats-grid">
                <div class="stat-card accent">
                    <div class="stat-label">Total Sent (All Campaigns)</div>
                    <div class="stat-value">${App.formatNumber(totalSent)}</div>
                </div>
                <div class="stat-card success">
                    <div class="stat-label">Total Views</div>
                    <div class="stat-value">${App.formatNumber(totalViews)}</div>
                    <div style="font-size:0.8rem;color:var(--text-muted)">${totalSent ? ((totalViews/totalSent)*100).toFixed(1) : 0}% open rate</div>
                </div>
                <div class="stat-card warning">
                    <div class="stat-label">Total Clicks</div>
                    <div class="stat-value">${App.formatNumber(totalClicks)}</div>
                    <div style="font-size:0.8rem;color:var(--text-muted)">${totalSent ? ((totalClicks/totalSent)*100).toFixed(1) : 0}% CTR</div>
                </div>
                <div class="stat-card" style="border-left-color:#14b8a6">
                    <div class="stat-label">Unsubscribes (IMAP)</div>
                    <div class="stat-value" style="color:#14b8a6">${App.formatNumber(totalUnsubs)}</div>
                    <div style="font-size:0.8rem;color:var(--text-muted)"><a href="#/unsubscribes" style="color:#14b8a6;text-decoration:none">View details →</a></div>
                </div>
                <div class="stat-card danger">
                    <div class="stat-label">Total Bounces</div>
                    <div class="stat-value">${App.formatNumber(totalBounces)}</div>
                    <div style="font-size:0.8rem;color:var(--text-muted)">${totalSent ? ((totalBounces/totalSent)*100).toFixed(1) : 0}% bounce rate</div>
                </div>
            </div>

            <!-- Campaign Comparison Chart -->
            <div class="card" style="margin-bottom:20px">
                <div class="card-header">
                    <h3 class="card-title">Campaign Comparison</h3>
                    <button class="btn btn-sm" onclick="Analytics.exportAllCampaigns()">Export Summary CSV</button>
                </div>
                <div class="chart-container" style="height:300px">
                    <canvas id="comparisonChart"></canvas>
                </div>
            </div>

            <!-- Campaign Selector & Date Range -->
            <div class="card" style="margin-bottom:20px">
                <div class="card-header">
                    <h3 class="card-title">Campaign Analytics</h3>
                    <div class="action-btns">
                        <button class="btn btn-sm btn-primary" onclick="Analytics.exportAllTypes()">Export This Campaign (All Data)</button>
                    </div>
                </div>
                <div class="form-grid" style="grid-template-columns: 2fr 1fr 1fr auto;">
                    <div class="form-group">
                        <label>Campaign</label>
                        <select id="analyticsCampaign" onchange="Analytics.onCampaignChange(this.value)">
                            ${campOptions || '<option>No campaigns</option>'}
                        </select>
                    </div>
                    <div class="form-group">
                        <label>From</label>
                        <input type="date" id="analyticsFrom" value="${this.fromDate}" onchange="Analytics.onDateChange()">
                    </div>
                    <div class="form-group">
                        <label>To</label>
                        <input type="date" id="analyticsTo" value="${this.toDate}" onchange="Analytics.onDateChange()">
                    </div>
                    <div class="form-group" style="display:flex;align-items:flex-end">
                        <button class="btn btn-primary" onclick="Analytics.loadCampaignAnalytics()">Load</button>
                    </div>
                </div>
                ${selectedCamp ? `
                <div class="stats-grid" style="margin-top:12px">
                    <div class="stat-card"><div class="stat-label">Status</div><div style="margin-top:4px">${App.statusBadge(selectedCamp.status)}</div></div>
                    <div class="stat-card accent"><div class="stat-label">Sent</div><div class="stat-value">${App.formatNumber(selectedCamp.sent || 0)}</div></div>
                    <div class="stat-card success"><div class="stat-label">Views</div><div class="stat-value">${App.formatNumber(selectedCamp.views || 0)}</div></div>
                    <div class="stat-card warning"><div class="stat-label">Clicks</div><div class="stat-value">${App.formatNumber(selectedCamp.clicks || 0)}</div></div>
                    <div class="stat-card danger"><div class="stat-label">Bounces</div><div class="stat-value">${App.formatNumber(selectedCamp.bounces || 0)}</div></div>
                    <div class="stat-card" style="border-left-color:#14b8a6"><div class="stat-label">Unsubscribes</div><div class="stat-value" style="color:#14b8a6">${App.formatNumber(this.campaignUnsubCount)}</div></div>
                </div>` : ''}
            </div>

            <!-- Charts Row -->
            <div class="charts-grid">
                <div class="card">
                    <div class="card-header">
                        <h3 class="card-title">Views Over Time</h3>
                        <div class="action-btns">
                            <button class="btn btn-sm" onclick="Analytics.exportSubscribers('views')">Export Who Opened</button>
                            <button class="btn btn-sm btn-secondary" onclick="Analytics.exportType('views')">Export Chart Data</button>
                        </div>
                    </div>
                    <div class="chart-container"><canvas id="viewsChart"></canvas></div>
                </div>
                <div class="card">
                    <div class="card-header">
                        <h3 class="card-title">Clicks Over Time</h3>
                        <div class="action-btns">
                            <button class="btn btn-sm" onclick="Analytics.exportSubscribers('clicks')">Export Who Clicked</button>
                            <button class="btn btn-sm btn-secondary" onclick="Analytics.exportType('clicks')">Export Chart Data</button>
                        </div>
                    </div>
                    <div class="chart-container"><canvas id="clicksChart"></canvas></div>
                </div>
            </div>

            <div class="charts-grid" style="margin-top:20px">
                <div class="card">
                    <div class="card-header">
                        <h3 class="card-title">Bounces Over Time</h3>
                        <div class="action-btns">
                            <button class="btn btn-sm" onclick="Analytics.exportSubscribers('bounces')">Export Who Bounced</button>
                            <button class="btn btn-sm btn-secondary" onclick="Analytics.exportType('bounces')">Export Chart Data</button>
                        </div>
                    </div>
                    <div class="chart-container"><canvas id="bouncesChart"></canvas></div>
                </div>
                <div class="card">
                    <div class="card-header">
                        <h3 class="card-title">Top Links</h3>
                        <button class="btn btn-sm" onclick="Analytics.exportType('links')">Export Links CSV</button>
                    </div>
                    <div class="chart-container"><canvas id="linksChart"></canvas></div>
                </div>
            </div>

            <!-- Links Table -->
            <div class="card" style="margin-top:20px">
                <div class="card-header">
                    <h3 class="card-title">Link Click Details</h3>
                </div>
                <div id="linksTable"></div>
            </div>
        `;

        App.setContent(html);

        // Render comparison chart
        this.renderComparisonChart();

        // Auto-load analytics for selected campaign
        if (this.selectedCampaignId) {
            this.loadCampaignAnalytics();
        }
    },

    renderComparisonChart() {
        const ctx = document.getElementById('comparisonChart');
        if (!ctx || !this.campaigns.length) return;

        const theme = this.getThemeColors();
        const sorted = [...this.campaigns].reverse();
        const labels = sorted.map(c => c.name?.substring(0, 25) || 'Untitled');

        if (this.charts.comparison) this.charts.comparison.destroy();
        this.charts.comparison = new Chart(ctx, {
            type: 'bar',
            data: {
                labels,
                datasets: [
                    { label: 'Sent', data: sorted.map(c => c.sent || 0), backgroundColor: 'rgba(83, 58, 253, 0.82)', borderRadius: 6 },
                    { label: 'Views', data: sorted.map(c => c.views || 0), backgroundColor: 'rgba(16, 185, 129, 0.82)', borderRadius: 6 },
                    { label: 'Clicks', data: sorted.map(c => c.clicks || 0), backgroundColor: 'rgba(245, 158, 11, 0.82)', borderRadius: 6 },
                    { label: 'Bounces', data: sorted.map(c => c.bounces || 0), backgroundColor: 'rgba(234, 34, 97, 0.82)', borderRadius: 6 },
                ],
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
        await this.loadCampaignUnsubCount();
        this.renderPage();
    },

    async loadCampaignUnsubCount() {
        if (!this.selectedCampaignId) {
            this.campaignUnsubCount = 0;
            return;
        }
        try {
            const res = await API.get(`/api/unsubscribes/stats?campaign_id=${this.selectedCampaignId}`);
            this.campaignUnsubCount = res.campaign_count || 0;
        } catch {
            this.campaignUnsubCount = 0;
        }
    },

    onDateChange() {
        this.fromDate = document.getElementById('analyticsFrom').value;
        this.toDate = document.getElementById('analyticsTo').value;
    },

    async loadCampaignAnalytics() {
        this.fromDate = document.getElementById('analyticsFrom')?.value || this.fromDate;
        this.toDate = document.getElementById('analyticsTo')?.value || this.toDate;

        if (!this.selectedCampaignId) {
            App.toast('Select a campaign first', 'error');
            return;
        }

        const baseParams = `campaign_id=${this.selectedCampaignId}&from_date=${this.fromDate}&to_date=${this.toDate}`;

        // Fetch all analytics types in parallel
        const [viewsRes, clicksRes, bouncesRes, linksRes] = await Promise.allSettled([
            API.get(`/api/campaigns/analytics/views?${baseParams}`),
            API.get(`/api/campaigns/analytics/clicks?${baseParams}`),
            API.get(`/api/campaigns/analytics/bounces?${baseParams}`),
            API.get(`/api/campaigns/analytics/links?${baseParams}`),
        ]);

        const viewsData = viewsRes.status === 'fulfilled' ? (viewsRes.value?.data || []) : [];
        const clicksData = clicksRes.status === 'fulfilled' ? (clicksRes.value?.data || []) : [];
        const bouncesData = bouncesRes.status === 'fulfilled' ? (bouncesRes.value?.data || []) : [];
        const linksData = linksRes.status === 'fulfilled' ? (linksRes.value?.data || []) : [];

        this.renderTimeChart('viewsChart', 'views', viewsData, '#10b981', 'rgba(16,185,129,0.12)');
        this.renderTimeChart('clicksChart', 'clicks', clicksData, '#f59e0b', 'rgba(245,158,11,0.12)');
        this.renderTimeChart('bouncesChart', 'bounces', bouncesData, '#ea2261', 'rgba(234,34,97,0.12)');
        this.renderLinksChart(linksData);
        this.renderLinksTable(linksData);
    },

    renderTimeChart(canvasId, key, data, borderColor, bgColor) {
        const ctx = document.getElementById(canvasId);
        if (!ctx) return;

        const theme = this.getThemeColors();
        if (this.charts[key]) this.charts[key].destroy();

        if (!data.length) {
            this.charts[key] = new Chart(ctx, {
                type: 'line',
                data: { labels: ['No data'], datasets: [{ label: key, data: [0] }] },
                options: { responsive: true, maintainAspectRatio: false },
            });
            return;
        }

        const labels = data.map(d => {
            const date = new Date(d.timestamp);
            return date.toLocaleDateString('en-US', { month: 'short', day: 'numeric' });
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
        if (this.charts.links) this.charts.links.destroy();

        if (!data.length) {
            this.charts.links = new Chart(ctx, {
                type: 'bar',
                data: { labels: ['No data'], datasets: [{ label: 'Clicks', data: [0] }] },
                options: { responsive: true, maintainAspectRatio: false },
            });
            return;
        }

        // Top 10 links
        const top = data.slice(0, 10);
        const labels = top.map(d => {
            try {
                const url = new URL(d.url);
                return url.pathname.substring(0, 30) || url.hostname;
            } catch {
                return d.url?.substring(0, 30) || '?';
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
            el.innerHTML = '<p style="color:var(--text-muted);padding:12px">No link data available</p>';
            return;
        }

        let html = '<div class="table-wrapper"><table><thead><tr><th>#</th><th>URL</th><th>Clicks</th></tr></thead><tbody>';
        data.forEach((d, i) => {
            const safeUrl = App.escapeHtml(d.url);
            const isSafeUrl = d.url && /^https?:\/\//i.test(d.url);
            html += `<tr>
                <td>${i + 1}</td>
                <td style="max-width:500px;overflow:hidden;text-overflow:ellipsis;word-break:break-all">
                    ${isSafeUrl ? `<a href="${safeUrl}" target="_blank" rel="noopener" style="color:var(--accent)">${safeUrl}</a>` : `<span>${safeUrl}</span>`}
                </td>
                <td><strong>${App.formatNumber(d.count || 0)}</strong></td>
            </tr>`;
        });
        html += '</tbody></table></div>';
        el.innerHTML = html;
    },

    // ── Export Functions ──────────────────────────────────

    // Export chart data (aggregate counts by date)
    async exportType(type) {
        const from = document.getElementById('analyticsFrom')?.value || this.fromDate;
        const to = document.getElementById('analyticsTo')?.value || this.toDate;
        const campId = this.selectedCampaignId;

        if (!campId) { App.toast('Select a campaign first', 'error'); return; }

        try {
            const result = await API.get(`/api/campaigns/analytics/${type}/export?campaign_id=${campId}&from_date=${from}&to_date=${to}`);
            if (result.blob) {
                const campName = this.campaigns.find(c => c.id === campId)?.name || 'campaign';
                const safeName = campName.replace(/[^a-zA-Z0-9]/g, '_').substring(0, 30);
                API.downloadBlob(result.blob, `${safeName}_${type}_chart.csv`);
                App.toast(`${type} chart data exported`, 'success');
            }
        } catch {
            // API.request already surfaced the server message.
        }
    },

    // Export subscriber list (who opened / who clicked / who bounced)
    async exportSubscribers(type) {
        const campId = this.selectedCampaignId;
        if (!campId) { App.toast('Select a campaign first', 'error'); return; }

        const labels = { views: 'who opened', clicks: 'who clicked', bounces: 'who bounced' };
        App.toast(`Exporting ${labels[type] || type}... this may take a moment`, 'info');

        try {
            const result = await API.get(`/api/campaigns/${campId}/subscribers/${type}/export`);
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

    // Export all subscriber lists for the selected campaign
    async exportAllTypes() {
        const campId = this.selectedCampaignId;
        if (!campId) { App.toast('Select a campaign first', 'error'); return; }

        const campName = this.campaigns.find(c => c.id === campId)?.name || 'campaign';
        const safeName = campName.replace(/[^a-zA-Z0-9]/g, '_').substring(0, 30);
        App.toast(`Exporting all data for "${campName}"... this may take a moment`, 'info');

        const types = ['views', 'clicks', 'bounces'];
        let exported = 0;

        for (const type of types) {
            try {
                const result = await API.get(`/api/campaigns/${campId}/subscribers/${type}/export`);
                if (result.blob) {
                    API.downloadBlob(result.blob, `${safeName}_${type}_subscribers.csv`);
                    exported++;
                }
            } catch {}
        }

        // Also export unsubscribes for this campaign
        try {
            const unsubResult = await API.get(`/api/unsubscribes/campaign/${campId}/export`);
            if (unsubResult.blob) {
                API.downloadBlob(unsubResult.blob, `${safeName}_unsubscribes.csv`);
                exported++;
            }
        } catch {}

        if (exported > 0) {
            App.toast(`Exported ${exported} file(s) including unsubscribes`, 'success');
        } else {
            App.toast('No data available to export', 'error');
        }
    },

    async exportAllCampaigns() {
        try {
            const result = await API.get('/api/campaigns/export-all');
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

