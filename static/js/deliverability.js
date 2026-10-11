/**
 * Deliverability & Safety Pre-Flight Module for ListMonk Pro.
 * Provides pre-flight audits, scorecards, and 1-click preset switching.
 */
const Deliverability = {
    checklistData: null,
    isLoading: false,

    async render() {
        App.setContent('<div class="loading-spinner">Auditing deliverability settings...</div>');

        try {
            this.checklistData = await API.get('/api/deliverability/checklist');
            this.renderPage();
        } catch (err) {
            console.error('Deliverability error:', err);
            App.setContent(`<div class="empty-state"><h3>Failed to load deliverability checklist</h3><p>${App.escapeHtml(err?.message || '')}</p></div>`);
        }
    },

    renderPage() {
        const data = this.checklistData || {};
        const score = data.score || 0;
        const checks = data.checks || [];
        const isOptimal = score === 100;

        let html = `
            <!-- Deliverability Score Header Banner -->
            <div class="deliverability-header-card card ${isOptimal ? 'optimal' : 'needs-tuning'}">
                <div class="deliverability-score-circle">
                    <div class="score-number">${score}%</div>
                    <div class="score-label">Deliverability Score</div>
                </div>

                <div class="deliverability-banner-info">
                    <h2>${isOptimal ? '🛡️ Optimal Cold Outreach Configuration' : '⚠️ Deliverability Recommendations'}</h2>
                    <p style="color:var(--text-secondary);margin:6px 0 16px 0;line-height:1.5;">
                        ${isOptimal 
                            ? 'Your Listmonk instance is fully optimized for B2B cold email deliverability. Tracking pixels are disabled, RFC unsubscribe headers are omitted, and batch concurrency is safely throttled for primary inbox placement.'
                            : 'Certain settings may increase spam filter suspicion or route messages to the Promotions tab. Use the 1-click preset button below to apply recommended cold outreach safeguards.'}
                    </p>

                    <div class="preset-action-bar">
                        <button class="btn btn-success" onclick="Deliverability.applyPreset('cold_email')">
                            🎯 Apply Cold Email Deliverability Preset
                        </button>
                        <button class="btn btn-secondary" onclick="Deliverability.applyPreset('newsletter')">
                            📰 Switch to Newsletter Marketing Preset
                        </button>
                    </div>
                </div>
            </div>

            <!-- Pre-Flight Checklist Table -->
            <div class="card" style="margin-top:24px;">
                <div class="card-header">
                    <div>
                        <h3 class="card-title">Pre-Flight Deliverability Audit</h3>
                        <p style="margin:4px 0 0 0;font-size:0.85rem;color:var(--text-secondary)">Real-time verification of Listmonk engine parameters</p>
                    </div>
                    <button class="btn btn-sm btn-secondary" onclick="Deliverability.render()">
                        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="14" height="14"><polyline points="23 4 23 10 17 10"/><path d="M20.49 15a9 9 0 1 1-2.12-9.36L23 10"/></svg>
                        Refresh Audit
                    </button>
                </div>

                <div class="table-wrapper">
                    <table class="table deliverability-table">
                        <thead>
                            <tr>
                                <th>Check Item</th>
                                <th>Status</th>
                                <th>Current Setting</th>
                                <th>Recommended for Cold Email</th>
                                <th>Impact & Why It Matters</th>
                            </tr>
                        </thead>
                        <tbody>
        `;

        checks.forEach(c => {
            const statusBadge = c.status === 'pass'
                ? '<span class="badge badge-success">✓ PASS</span>'
                : (c.status === 'warn'
                    ? '<span class="badge badge-warning">! WARN</span>'
                    : '<span class="badge badge-danger">✕ FAIL</span>');

            html += `
                <tr>
                    <td>
                        <strong style="color:var(--text-primary)">${App.escapeHtml(c.name)}</strong>
                        <div style="font-size:0.82rem;color:var(--text-muted);margin-top:2px">${App.escapeHtml(c.description)}</div>
                    </td>
                    <td>${statusBadge}</td>
                    <td><code>${App.escapeHtml(String(c.current_value))}</code></td>
                    <td><code>${App.escapeHtml(String(c.recommended))}</code></td>
                    <td style="font-size:0.85rem;color:var(--text-secondary);">${App.escapeHtml(c.impact)}</td>
                </tr>
            `;
        });

        html += `
                        </tbody>
                    </table>
                </div>
            </div>

            <!-- Best Practice Guidelines -->
            <div class="best-practices-grid" style="margin-top:24px;display:grid;grid-template-columns:repeat(auto-fit,minmax(320px,1fr));gap:20px;">
                <div class="card">
                    <h3 style="font-size:1.1rem;margin-bottom:8px">📬 Why Tracking Pixels Hurt Cold Email</h3>
                    <p style="font-size:0.88rem;color:var(--text-secondary);line-height:1.6">
                        Spam filters (Google Workspace, Microsoft 365) detect 1x1 invisible GIF pixels and link redirect wrappers. In cold email, removing tracking pixels dramatically improves inbox placement by making the email indistinguishable from a personal 1-to-1 message.
                    </p>
                </div>

                <div class="card">
                    <h3 style="font-size:1.1rem;margin-bottom:8px">🚫 Why Remove List-Unsubscribe Header</h3>
                    <p style="font-size:0.88rem;color:var(--text-secondary);line-height:1.6">
                        The RFC <code>List-Unsubscribe</code> header explicitly identifies an email as a bulk marketing broadcast. In B2B sales outreach, use a soft plain-text opt-out (e.g. <em>"Reply 'stop' to opt out"</em>) to avoid automated Promotions tab sorting.
                    </p>
                </div>

                <div class="card">
                    <h3 style="font-size:1.1rem;margin-bottom:8px">⏱️ Safe Sending Cadence & Batching</h3>
                    <p style="font-size:0.88rem;color:var(--text-secondary);line-height:1.6">
                        Keep batch size <= 100 with concurrency of 1 worker. Spreading emails evenly prevents sudden IP/domain spikes and maintains a natural sending rhythm that matches human behavior.
                    </p>
                </div>
            </div>
        `;

        App.setContent(html);
    },

    async applyPreset(mode) {
        const modeName = mode === 'cold_email' ? 'Cold Email Deliverability' : 'Newsletter Marketing';
        const ok = await App.confirm(
            `Apply ${modeName} Preset`,
            `This will automatically update your Listmonk engine settings for ${modeName}. Are you sure you want to proceed?`
        );
        if (!ok) return;

        App.showProgress('Applying Preset', `Updating Listmonk settings for ${modeName}...`);
        try {
            await API.post('/api/deliverability/apply-preset', { mode });
            App.closeModal();
            App.toast(`Successfully applied ${modeName} preset!`, 'success');
            await this.render();
        } catch (err) {
            App.closeModal();
            App.toast(`Failed to apply preset: ${err?.message}`, 'danger');
        }
    }
};
