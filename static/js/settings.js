/**
 * Settings page - combines Scheduler and Auto-Unblock into one page with tabs.
 */
const Settings = {
    activeTab: 'scheduler',
    schedule: null,
    unblockStatus: null,
    _refreshInterval: null,

    async render() {
        App.setContent('<div class="loading-spinner">Loading settings...</div>');

        try {
            const [sched, unblock] = await Promise.all([
                API.get('/api/scheduler'),
                API.get('/api/auto-unblock/status'),
            ]);
            this.schedule = sched;
            this.unblockStatus = unblock;
        } catch {
            App.setContent('<div class="empty-state"><h3>Failed to load settings</h3></div>');
            return;
        }

        this.renderPage();

        if (this._refreshInterval) clearInterval(this._refreshInterval);
        this._refreshInterval = setInterval(() => this.refreshStatus(), 30000);
    },

    renderPage() {
        const html = `
            <div class="settings-tabs">
                <button class="settings-tab ${this.activeTab === 'scheduler' ? 'active' : ''}" onclick="Settings.switchTab('scheduler')">
                    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="16" height="16"><circle cx="12" cy="12" r="10"/><polyline points="12 6 12 12 16 14"/></svg>
                    Campaign Scheduler
                </button>
                <button class="settings-tab ${this.activeTab === 'autounblock' ? 'active' : ''}" onclick="Settings.switchTab('autounblock')">
                    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="16" height="16"><path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z"/></svg>
                    Auto-Unblock Protection
                </button>
            </div>
            <div id="settingsContent"></div>
        `;
        App.setContent(html);
        this.renderTab();
    },

    switchTab(tab) {
        this.activeTab = tab;
        document.querySelectorAll('.settings-tab').forEach(t => t.classList.remove('active'));
        document.querySelector(`.settings-tab[onclick="Settings.switchTab('${tab}')"]`)?.classList.add('active');
        this.renderTab();
    },

    renderTab() {
        const el = document.getElementById('settingsContent');
        if (!el) return;
        if (this.activeTab === 'scheduler') {
            el.innerHTML = this.renderScheduler();
        } else {
            el.innerHTML = this.renderAutoUnblock();
        }
    },

    // ── Scheduler Tab ────────────────────────────────────
    /**
     * Percentage offsets for the send-window band on a 0-100% day timeline.
     * An overnight window (e.g. 20:00 -> 08:00) wraps past midnight, so it is
     * drawn as two segments — emitting left/right for the raw times yields a
     * negative width and the band disappears.
     */
    _windowSegments(startHour, startMinute, endHour, endMinute) {
        const startPct = (startHour * 60 + startMinute) / 1440 * 100;
        const endPct = (endHour * 60 + endMinute) / 1440 * 100;
        if (endPct > startPct) {
            return [{ left: startPct, width: endPct - startPct }];
        }
        // Overnight: [start -> 24:00] and [00:00 -> end]
        return [
            { left: startPct, width: 100 - startPct },
            { left: 0, width: endPct },
        ];
    },

    renderScheduler() {
        const s = this.schedule;
        const enabled = s.enabled;
        const inWindow = s.in_send_window;

        let statusHtml;
        if (!enabled) {
            statusHtml = '<span class="badge badge-default" style="font-size:0.9rem;padding:6px 14px">DISABLED</span>';
        } else if (inWindow) {
            statusHtml = '<span class="badge badge-success" style="font-size:0.9rem;padding:6px 14px">SENDING - Inside send window</span>';
        } else {
            statusHtml = '<span class="badge badge-warning" style="font-size:0.9rem;padding:6px 14px">PAUSED - Outside send window</span>';
        }

        const allDays = [
            { key: 'mon', label: 'Mon' }, { key: 'tue', label: 'Tue' },
            { key: 'wed', label: 'Wed' }, { key: 'thu', label: 'Thu' },
            { key: 'fri', label: 'Fri' }, { key: 'sat', label: 'Sat' },
            { key: 'sun', label: 'Sun' },
        ];
        const activeDays = new Set(s.days || []);
        const daysHtml = allDays.map(d =>
            `<label class="checkbox-label"><input type="checkbox" name="schedDays" value="${d.key}" ${activeDays.has(d.key) ? 'checked' : ''}><span>${d.label}</span></label>`
        ).join('');

        const hourOptions = (selectedHour) => {
            let html = '';
            for (let h = 0; h < 24; h++) {
                const label = h === 0 ? '12 AM' : h < 12 ? `${h} AM` : h === 12 ? '12 PM' : `${h - 12} PM`;
                html += `<option value="${h}" ${h === selectedHour ? 'selected' : ''}>${label}</option>`;
            }
            return html;
        };
        const minuteOptions = (selectedMin) => {
            let html = '';
            for (let m = 0; m < 60; m += 15) {
                html += `<option value="${m}" ${m === selectedMin ? 'selected' : ''}>${m.toString().padStart(2, '0')}</option>`;
            }
            return html;
        };

        const timezones = [
            'US/Eastern', 'US/Central', 'US/Mountain', 'US/Pacific',
            'UTC', 'Europe/London', 'Europe/Berlin', 'Europe/Paris',
            'Asia/Tokyo', 'Asia/Shanghai', 'Asia/Kolkata', 'Asia/Dhaka',
            'Australia/Sydney', 'Pacific/Auckland',
        ];
        const tzOptions = timezones.map(tz =>
            `<option value="${tz}" ${tz === s.timezone ? 'selected' : ''}>${tz}</option>`
        ).join('');

        const autoPaused = s.auto_paused_campaigns || [];

        return `
            <div class="card" style="margin-bottom:20px">
                <div class="card-header">
                    <h3 class="card-title">Campaign Send Window</h3>
                    <div class="action-btns">
                        ${statusHtml}
                        <button class="btn btn-sm" onclick="Settings.runSchedulerNow()">Check Now</button>
                    </div>
                </div>
                <div style="display:flex;gap:20px;align-items:center;flex-wrap:wrap">
                    <div>
                        <span style="color:var(--text-muted);font-size:0.85rem">Current time:</span>
                        <strong id="schedulerCurrentTime">${s.current_time || '-'}</strong>
                    </div>
                    <div>
                        <span style="color:var(--text-muted);font-size:0.85rem">Window:</span>
                        <strong>${this.formatTime(s.start_hour, s.start_minute)} - ${this.formatTime(s.end_hour, s.end_minute)} ${s.timezone}</strong>
                    </div>
                    ${autoPaused.length ? `
                    <div>
                        <span style="color:var(--text-muted);font-size:0.85rem">Auto-paused campaigns:</span>
                        <strong style="color:var(--warning)">${autoPaused.length}</strong>
                    </div>` : ''}
                </div>
            </div>

            <div class="card">
                <div class="card-header"><h3 class="card-title">Schedule Configuration</h3></div>

                <div class="form-group" style="margin-bottom:20px">
                    <label class="checkbox-label ${enabled ? 'toggle-on' : ''}" style="font-size:1rem;padding:12px 18px;border-width:2px">
                        <input type="checkbox" id="schedEnabled" ${enabled ? 'checked' : ''} onchange="Settings.toggleScheduler(this.checked)">
                        <span style="font-weight:600">${enabled ? 'Scheduler is ON' : 'Scheduler is OFF'}</span>
                    </label>
                    <p style="font-size:0.85rem;color:var(--text-muted);margin-top:8px">
                        When enabled, running campaigns will auto-pause outside the send window and auto-resume when the window opens.
                        Manually paused campaigns are never affected.
                    </p>
                </div>

                <div class="form-group">
                    <label>Timezone</label>
                    <select id="schedTimezone" style="width:auto;min-width:200px">${tzOptions}</select>
                </div>

                <div style="display:flex;gap:16px;flex-wrap:wrap;align-items:flex-end;margin-bottom:16px">
                    <div class="form-group" style="margin-bottom:0">
                        <label>Start Time</label>
                        <div style="display:flex;gap:4px">
                            <select id="schedStartHour" style="width:auto">${hourOptions(s.start_hour)}</select>
                            <select id="schedStartMin" style="width:auto">${minuteOptions(s.start_minute)}</select>
                        </div>
                    </div>
                    <span style="color:var(--text-muted);font-size:1.2rem;padding-bottom:8px">to</span>
                    <div class="form-group" style="margin-bottom:0">
                        <label>End Time</label>
                        <div style="display:flex;gap:4px">
                            <select id="schedEndHour" style="width:auto">${hourOptions(s.end_hour)}</select>
                            <select id="schedEndMin" style="width:auto">${minuteOptions(s.end_minute)}</select>
                        </div>
                    </div>
                </div>

                <div class="form-group">
                    <label>Send on these days</label>
                    <div class="checkbox-group">${daysHtml}</div>
                </div>

                <div style="margin-top:24px;margin-bottom:24px">
                    <label style="font-size:0.85rem;font-weight:500;color:var(--text-secondary);margin-bottom:10px;display:block">Daily Timeline</label>
                    <div style="position:relative;height:44px;background:var(--bg-input);border-radius:var(--radius-md);overflow:hidden;border:1px solid var(--glass-border)">
                        ${this._windowSegments(s.start_hour, s.start_minute, s.end_hour, s.end_minute).map(seg => `
                            <div style="position:absolute;left:${seg.left}%;width:${seg.width}%;top:0;bottom:0;background:rgba(16,185,129,0.2);border-left:2px solid var(--emerald);border-right:2px solid var(--emerald)"></div>
                        `).join('')}
                        ${this.renderTimeMarkers()}
                    </div>
                    <div style="display:flex;justify-content:space-between;font-size:0.75rem;color:var(--text-muted);margin-top:6px;padding:0 2px">
                        <span>12AM</span><span>3AM</span><span>6AM</span><span>9AM</span><span>12PM</span><span>3PM</span><span>6PM</span><span>9PM</span><span>12AM</span>
                    </div>
                    ${(s.start_hour * 60 + s.start_minute) > (s.end_hour * 60 + s.end_minute) ? '<p style="font-size:0.75rem;color:var(--text-muted);margin-top:6px">Overnight window — the band wraps past midnight.</p>' : ''}
                </div>

                <div class="form-actions">
                    <button class="btn btn-primary" onclick="Settings.saveSchedule()">Save Schedule</button>
                </div>
            </div>

            <div class="card" style="margin-top:20px">
                <div class="card-header"><h3 class="card-title">How it works</h3></div>
                <div style="font-size:0.85rem;color:var(--text-secondary);line-height:1.8">
                    <p><strong>Every 60 seconds</strong>, the scheduler checks:</p>
                    <ul style="padding-left:20px;margin:8px 0">
                        <li>If current time is <strong>outside</strong> the send window and a campaign is <strong>running</strong> → it gets <strong>auto-paused</strong></li>
                        <li>If current time is <strong>inside</strong> the send window and a campaign was <strong>auto-paused</strong> → it gets <strong>auto-resumed</strong></li>
                        <li>Campaigns you <strong>manually pause</strong> are never touched by the scheduler</li>
                    </ul>
                    <p>The scheduler only affects campaigns with status "running". Draft, finished, or cancelled campaigns are ignored.</p>
                </div>
            </div>
        `;
    },

    // ── Auto-Unblock Tab ─────────────────────────────────
    renderAutoUnblock() {
        const u = this.unblockStatus || {};
        const failed = Boolean(u.error);
        const count = u.blocklisted_engaged ?? u.blocklisted_clickers ?? 0;
        const interval = u.interval_hours || 6;
        const statusBadge = failed
            ? `<span class="badge badge-danger" style="font-size:0.9rem;padding:6px 14px" title="${App.escapeHtml(u.error)}">Status unavailable</span>`
            : count > 0
                ? `<span class="badge badge-warning" style="font-size:0.9rem;padding:6px 14px">${count} blocklisted engaged subscriber(s) found</span>`
                : '<span class="badge badge-success" style="font-size:0.9rem;padding:6px 14px">All clear</span>';

        return `
            <div class="card" style="margin-bottom:20px">
                <div class="card-header">
                    <h3 class="card-title">Auto-Unblock Status</h3>
                    <div class="action-btns">${statusBadge}</div>
                </div>
                <div style="display:flex;gap:20px;align-items:center;flex-wrap:wrap">
                    <div>
                        <span style="color:var(--text-muted);font-size:0.85rem">Blocklisted engaged:</span>
                        <strong style="color:${failed ? 'var(--text-muted)' : (count > 0 ? 'var(--warning)' : 'var(--success)')}">${failed ? '—' : count}</strong>
                    </div>
                    <div>
                        <span style="color:var(--text-muted);font-size:0.85rem">Auto-check interval:</span>
                        <strong>Every ${interval} hours</strong>
                    </div>
                </div>
            </div>

            <div class="card" style="margin-bottom:20px">
                <div class="card-header"><h3 class="card-title">Manual Unblock</h3></div>
                <p style="font-size:0.85rem;color:var(--text-secondary);margin-bottom:16px">
                    Click the button below to scan for subscribers who opened or clicked campaigns but got blocklisted due to bounces.
                    They will be re-enabled and their bounce records will be deleted.
                </p>
                <button class="btn btn-primary" id="runUnblockBtn" onclick="Settings.runUnblock()">
                    Run Auto-Unblock Now
                </button>
                <div id="unblockResult" style="margin-top:16px"></div>
            </div>

            <div class="card">
                <div class="card-header"><h3 class="card-title">How it works</h3></div>
                <div style="font-size:0.85rem;color:var(--text-secondary);line-height:1.8">
                    <p><strong>Every ${interval} hours</strong>, the system automatically:</p>
                    <ul style="padding-left:20px;margin:8px 0">
                        <li>Finds subscribers who <strong>opened or clicked</strong> any campaign but are <strong>blocklisted</strong></li>
                        <li><strong>Re-enables</strong> them (removes blocklist status)</li>
                        <li><strong>Deletes</strong> their bounce records (since they clearly received the email)</li>
                    </ul>
                    <p>This protects real subscribers who get falsely bounced — e.g. security scanners that pre-scan links, or delayed bounce DSNs after the recipient already opened the email.</p>
                </div>
            </div>
        `;
    },

    // ── Scheduler Actions ────────────────────────────────
    formatTime(hour, minute) {
        const ampm = hour >= 12 ? 'PM' : 'AM';
        const h = hour === 0 ? 12 : hour > 12 ? hour - 12 : hour;
        return `${h}:${minute.toString().padStart(2, '0')} ${ampm}`;
    },

    renderTimeMarkers() {
        let html = '';
        for (let h = 0; h <= 24; h += 3) {
            const pct = (h / 24) * 100;
            html += `<div style="position:absolute;left:${pct}%;top:0;bottom:0;width:1px;background:var(--glass-border)"></div>`;
        }
        return html;
    },

    async toggleScheduler(checked) {
        try {
            await API.put('/api/scheduler', { enabled: checked });
            App.toast(checked ? 'Scheduler enabled' : 'Scheduler disabled', 'success');
            this.schedule = await API.get('/api/scheduler');
            this.renderTab();
        } catch {
            // API.request already surfaced the server message.
        }
    },

    async saveSchedule() {
        const data = {
            enabled: document.getElementById('schedEnabled').checked,
            timezone: document.getElementById('schedTimezone').value,
            start_hour: parseInt(document.getElementById('schedStartHour').value),
            start_minute: parseInt(document.getElementById('schedStartMin').value),
            end_hour: parseInt(document.getElementById('schedEndHour').value),
            end_minute: parseInt(document.getElementById('schedEndMin').value),
            days: Array.from(document.querySelectorAll('input[name="schedDays"]:checked')).map(cb => cb.value),
        };
        if (data.days.length === 0) {
            App.toast('Select at least one day', 'error');
            return;
        }
        try {
            await API.put('/api/scheduler', data);
            App.toast('Schedule saved', 'success');
            this.schedule = await API.get('/api/scheduler');
            this.renderTab();
        } catch {
            // API.request already surfaced the server message.
        }
    },

    async runSchedulerNow() {
        try {
            const result = await API.post('/api/scheduler/run');
            const status = result.in_send_window ? 'Inside send window' : 'Outside send window';
            const paused = result.auto_paused_campaigns?.length || 0;
            App.toast(`${status} | ${paused} campaign(s) auto-paused`, 'info');
            this.schedule = await API.get('/api/scheduler');
            this.renderTab();
        } catch {
            // API.request already surfaced the server message.
        }
    },

    // ── Auto-Unblock Actions ─────────────────────────────
    async runUnblock() {
        const btn = document.getElementById('runUnblockBtn');
        const resultEl = document.getElementById('unblockResult');
        btn.disabled = true;
        btn.textContent = 'Running...';
        resultEl.innerHTML = '';

        try {
            const result = await API.post('/api/auto-unblock/run');
            if (result.success === 0 && result.failed === 0) {
                resultEl.innerHTML = '<div class="badge badge-success" style="padding:8px 14px">No blocklisted engaged subscribers found - all clear!</div>';
            } else {
                let html = `<div class="badge badge-success" style="padding:8px 14px">${result.success} unblocked, ${result.failed} failed</div>`;
                if (result.unblocked?.length) {
                    html += '<div style="margin-top:12px;font-size:0.85rem;color:var(--text-secondary)">';
                    html += '<strong>Unblocked:</strong> ' + result.unblocked.map(e => `<code>${App.escapeHtml(e)}</code>`).join(', ');
                    html += '</div>';
                }
                resultEl.innerHTML = html;
            }
            // Refresh status
            this.unblockStatus = await API.get('/api/auto-unblock/status');
        } catch {
            // API.request already surfaced the server message; reflect it inline.
            resultEl.innerHTML = '<div class="badge badge-danger" style="padding:8px 14px">Auto-unblock run failed — see the notification above</div>';
        } finally {
            btn.disabled = false;
            btn.textContent = 'Run Auto-Unblock Now';
        }
    },

    // ── Auto-refresh ─────────────────────────────────────
    async refreshStatus() {
        if (App.currentPage !== 'settings') {
            if (this._refreshInterval) {
                clearInterval(this._refreshInterval);
                this._refreshInterval = null;
            }
            return;
        }
        try {
            this.schedule = await API.get('/api/scheduler');
            const el = document.getElementById('schedulerCurrentTime');
            if (el) el.textContent = this.schedule.current_time || '-';
        } catch {}
    },
};
