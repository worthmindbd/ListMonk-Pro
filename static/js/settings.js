/**
 * Settings page - combines Scheduler, Warm-up, Email Cleanup and Auto-Unblock into one page with tabs.
 */
const Settings = {
    activeTab: 'scheduler',
    schedule: null,
    warmup: null,
    cleanup: null,
    unblockStatus: null,
    _refreshInterval: null,
    _cleanupPoll: null,
    _previewTimer: null,
    _previewSeq: 0,

    TABS: [
        ['scheduler', 'Send Window', '<circle cx="12" cy="12" r="10"/><polyline points="12 6 12 12 16 14"/>'],
        ['warmup', 'Warm-up', '<polyline points="22 7 13.5 15.5 8.5 10.5 2 17"/><polyline points="16 7 22 7 22 13"/>'],
        ['automation', 'Automation', '<polyline points="16 3 21 3 21 8"/><line x1="4" y1="20" x2="21" y2="3"/><polyline points="21 16 21 21 16 21"/><line x1="15" y1="15" x2="21" y2="21"/><line x1="4" y1="4" x2="9" y2="9"/>'],
        ['cleanup', 'Email Cleanup', '<path d="M3 6h18"/><path d="M8 6V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/><path d="M19 6l-1 14a2 2 0 0 1-2 2H8a2 2 0 0 1-2-2L5 6"/>'],
        ['autounblock', 'Auto-Unblock', '<path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z"/>'],
    ],

    async render() {
        this._seq = App.seq();
        // Deep-linkable tab: #/settings?tab=automation
        const wantedTab = App.routeQuery().get('tab');
        if (wantedTab && this.TABS.some(t => t[0] === wantedTab)) this.activeTab = wantedTab;
        App.setContent('<div class="loading-spinner">Loading settings...</div>');

        try {
            const [sched, unblock, warmup, cleanup, unsub] = await Promise.all([
                API.get('/api/scheduler'),
                API.get('/api/auto-unblock/status'),
                // Warm-up is its own feature: if it fails to load, the other
                // tabs must still work (the Warm-up tab explains the failure).
                API.get('/api/warmup', true).catch(() => null),
                // Same for email cleanup.
                API.get('/api/email-validation/cleanup', true).catch(() => null),
                // Automation tab: unsubscribe action + reply rules.
                API.get('/api/unsubscribes/settings', true).catch(() => null),
                typeof Replies !== 'undefined' ? Replies.loadSettings() : Promise.resolve(),
            ]);
            if (App.stale(this._seq)) return;
            this.schedule = sched;
            this.unblockStatus = unblock;
            this.warmup = warmup;
            this.cleanup = cleanup;
            this.unsubSettings = unsub;
        } catch {
            if (App.stale(this._seq)) return;
            App.setContent('<div class="empty-state"><h2>Failed to load settings</h2></div>');
            return;
        }

        this.renderPage();

        if (this._refreshInterval) clearInterval(this._refreshInterval);
        this._refreshInterval = setInterval(() => this.refreshStatus(), 15000);
    },

    renderPage() {
        if (this._seq !== undefined && App.stale(this._seq)) return;
        const html = `
            <div class="settings-tabs" role="tablist" aria-label="Settings sections">
                ${this.TABS.map(([key, label, icon]) => `
                <button class="settings-tab ${this.activeTab === key ? 'active' : ''}" role="tab" aria-selected="${this.activeTab === key}" data-tab="${key}" onclick="Settings.switchTab('${key}')">
                    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="16" height="16">${icon}</svg>
                    ${label}
                </button>`).join('')}
            </div>
            <div id="settingsContent"></div>
        `;
        App.setContent(html);
        this.renderTab();
    },

    switchTab(tab) {
        this.activeTab = tab;
        document.querySelectorAll('.settings-tab').forEach(t => {
            const on = t.dataset.tab === tab;
            t.classList.toggle('active', on);
            t.setAttribute('aria-selected', on ? 'true' : 'false');
            if (on) t.scrollIntoView({ block: 'nearest', inline: 'nearest' });
        });
        App.syncUrl('settings', { tab });
        this.renderTab();
    },

    renderTab() {
        const el = document.getElementById('settingsContent');
        if (!el) return;
        if (this.activeTab === 'scheduler') {
            el.innerHTML = this.renderScheduler();
        } else if (this.activeTab === 'warmup') {
            el.innerHTML = this.renderWarmup();
        } else if (this.activeTab === 'cleanup') {
            el.innerHTML = this.renderCleanup();
        } else if (this.activeTab === 'automation') {
            el.innerHTML = this.renderAutomation();
        } else {
            el.innerHTML = this.renderAutoUnblock();
        }
        // These tabs replace innerHTML *after* App.setContent() ran, so they
        // miss the central accessible-name pass unless they re-run it here.
        App.labelControls(el);
        if (this.activeTab === 'warmup' && this.warmup) {
            this._previewWarmup();
            this._loadWarmupInsights();
        }
        if (this.activeTab === 'cleanup' && this.cleanup) {
            this._cleanupFreqChanged();
            if (this.cleanup.job?.running) this._startCleanupPoll();
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

        // Outside the window is the normal state most of the day: only call
        // it out as a warning when campaigns are actually being held.
        const held = (s.auto_paused_campaigns || []).length;
        const nextOpen = enabled && !inWindow ? this._nextWindowOpen(s) : '';
        let statusHtml;
        if (!enabled) {
            statusHtml = '<span class="badge badge-default">Off: campaigns send any time</span>';
        } else if (inWindow) {
            statusHtml = '<span class="badge badge-success">Inside send window</span>';
        } else if (held) {
            statusHtml = `<span class="badge badge-warning">Holding ${held} campaign${held === 1 ? '' : 's'}${nextOpen ? ` until ${App.escapeHtml(nextOpen)}` : ''}</span>`;
        } else {
            statusHtml = `<span class="badge badge-default">Outside send window${nextOpen ? ` · opens ${App.escapeHtml(nextOpen)}` : ''}</span>`;
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
        // The stored value is always offered, even off the 15-minute grid or
        // outside the short timezone list (the API accepts any minute and any
        // IANA zone): otherwise the select fell back to its first option and
        // "Save Schedule" silently rewrote the window.
        const minuteOptions = (selectedMin) => {
            const minutes = [0, 15, 30, 45];
            if (Number.isInteger(selectedMin) && !minutes.includes(selectedMin)) {
                minutes.push(selectedMin);
                minutes.sort((a, b) => a - b);
            }
            return minutes.map(m =>
                `<option value="${m}" ${m === selectedMin ? 'selected' : ''}>${m.toString().padStart(2, '0')}</option>`
            ).join('');
        };

        const timezones = [
            'US/Eastern', 'US/Central', 'US/Mountain', 'US/Pacific',
            'UTC', 'Europe/London', 'Europe/Berlin', 'Europe/Paris',
            'Asia/Tokyo', 'Asia/Shanghai', 'Asia/Kolkata', 'Asia/Dhaka',
            'Australia/Sydney', 'Pacific/Auckland',
        ];
        if (s.timezone && !timezones.includes(s.timezone)) timezones.unshift(s.timezone);
        const tzOptions = timezones.map(tz =>
            `<option value="${App.escapeHtml(tz)}" ${tz === s.timezone ? 'selected' : ''}>${App.escapeHtml(tz)}</option>`
        ).join('');

        const autoPaused = s.auto_paused_campaigns || [];

        return `
            <div class="card" style="margin-bottom:20px">
                <div class="card-header">
                    <h2 class="card-title">Campaign Send Window</h2>
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
                <div class="card-header"><h2 class="card-title">Schedule Configuration</h2></div>

                ${this._switchRow({
                    id: 'schedEnabled', checked: enabled, onchange: 'Settings.toggleScheduler(this.checked)',
                    title: 'Only send inside the window', labelId: 'schedEnabledLabel',
                    desc: 'Running campaigns auto-pause outside the window and resume when it opens. Manually paused campaigns are never touched. Turning this off resumes the campaigns it paused.',
                })}

                <div class="form-group">
                    <label>Timezone</label>
                    <select id="schedTimezone" style="width:auto;min-width:200px">${tzOptions}</select>
                </div>

                <div style="display:flex;gap:16px;flex-wrap:wrap;align-items:flex-end;margin-bottom:16px">
                    <div class="form-group" style="margin-bottom:0">
                        <label>Start Time</label>
                        <div style="display:flex;gap:4px">
                            <select id="schedStartHour" aria-label="Start hour" style="width:auto">${hourOptions(s.start_hour)}</select>
                            <select id="schedStartMin" aria-label="Start minute" style="width:auto">${minuteOptions(s.start_minute)}</select>
                        </div>
                    </div>
                    <span style="color:var(--text-muted);font-size:1.2rem;padding-bottom:8px">to</span>
                    <div class="form-group" style="margin-bottom:0">
                        <label>End Time</label>
                        <div style="display:flex;gap:4px">
                            <select id="schedEndHour" aria-label="End hour" style="width:auto">${hourOptions(s.end_hour)}</select>
                            <select id="schedEndMin" aria-label="End minute" style="width:auto">${minuteOptions(s.end_minute)}</select>
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
                <div class="card-header"><h2 class="card-title">How it works</h2></div>
                <div style="font-size:0.85rem;color:var(--text-secondary);line-height:1.8">
                    <p><strong>Every 60 seconds</strong>, the scheduler checks:</p>
                    <ul style="padding-left:20px;margin:8px 0">
                        <li>If current time is <strong>outside</strong> the send window and a campaign is <strong>running</strong> → it gets <strong>auto-paused</strong></li>
                        <li>If current time is <strong>inside</strong> the send window and a campaign was <strong>auto-paused</strong> → it gets <strong>auto-resumed</strong></li>
                        <li>Campaigns you <strong>manually pause</strong> are never touched by the scheduler</li>
                        <li>Turning the scheduler <strong>off</strong> resumes the campaigns it auto-paused</li>
                    </ul>
                    <p>The scheduler only affects campaigns with status "running". Draft, finished, or cancelled campaigns are ignored.</p>
                </div>
            </div>
        `;
    },

    // ── Warm-up Tab ──────────────────────────────────────
    _n(value) {
        return Number(value).toLocaleString();
    },

    _warmupBadge(w) {
        const style = 'font-size:0.9rem;padding:6px 14px';
        if (!w.enabled) return `<span class="badge badge-default" style="${style}">OFF</span>`;
        if (w.state === 'quota_reached') {
            return `<span class="badge badge-warning" style="${style}">DAILY LIMIT REACHED - resumes tomorrow</span>`;
        }
        if (w.state === 'sending') {
            return `<span class="badge badge-success" style="${style}">SENDING - ${this._n(w.remaining)} left today</span>`;
        }
        return `<span class="badge badge-info" style="${style}">READY - nothing is sending</span>`;
    },

    _warmupProgressText(w) {
        if (w.at_target) return `Day ${this._n(w.day)} - at your target of ${this._n(w.target_volume)} a day`;
        const left = w.days_to_target;
        return `Day ${this._n(w.day)} - tomorrow up to ${this._n(w.next_quota)} - target ${this._n(w.target_volume)} in ${this._n(left)} more day${left === 1 ? '' : 's'}`;
    },

    renderWarmup() {
        const w = this.warmup;
        if (!w) {
            return `<div class="card"><div class="empty-state"><h2>Warm-up unavailable</h2>
                <p>The warm-up status could not be loaded. Reload the page to try again.</p></div></div>`;
        }
        const pct = w.quota > 0 ? Math.min(100, Math.round(w.sent_today / w.quota * 100)) : 0;

        const timezones = [
            'US/Eastern', 'US/Central', 'US/Mountain', 'US/Pacific',
            'UTC', 'Europe/London', 'Europe/Berlin', 'Europe/Paris',
            'Asia/Tokyo', 'Asia/Shanghai', 'Asia/Kolkata', 'Asia/Dhaka',
            'Australia/Sydney', 'Pacific/Auckland',
        ];
        // Never configured: start from the send-window timezone, which is
        // almost always the one the user means by "my day".
        const savedTz = (w.enabled || (w.history && w.history.length) || w.sent_today > 0)
            ? w.timezone : (this.schedule?.timezone || w.timezone);
        if (savedTz && !timezones.includes(savedTz)) timezones.unshift(savedTz);
        const tzOptions = timezones.map(tz =>
            `<option value="${App.escapeHtml(tz)}" ${tz === savedTz ? 'selected' : ''}>${App.escapeHtml(tz)}</option>`
        ).join('');

        const presetButtons = Object.entries(w.presets || {}).map(([key, p]) =>
            `<button type="button" class="btn btn-sm" title="${App.escapeHtml(p.summary)}" onclick="Settings.applyWarmupPreset('${App.escapeHtml(key)}')">${App.escapeHtml(p.label)} <span style="color:var(--text-muted)">${p.growth_percent === 100 ? 'x2' : '+' + p.growth_percent + '%'}</span></button>`
        ).join('');

        // Newest first. Days with no entry were never closed (this app was
        // not running then): say so instead of silently skipping them, and
        // flag a day that went over its plan.
        const hist = (w.history || []).filter(h => h && h.date);
        const dayMs = 86400000;
        const parseDay = (iso) => { const [y, m, d] = iso.split('-').map(Number); return Date.UTC(y, m - 1, d); };
        const fmtDay = (ms) => new Date(ms).toISOString().slice(0, 10);
        const historyRows = hist.map((h, i) => {
            const over = (h.sent ?? 0) - (h.quota ?? 0);
            const overBadge = over > 0
                ? ` <span class="badge badge-danger" title="ListMonk sent faster than the per-second check could stop it, or this app was not running for part of the day">Over by ${this._n(over)}</span>`
                : '';
            let row = `
            <tr>
                <td>${App.escapeHtml(h.date)}</td>
                <td>${this._n(h.day ?? 0)}</td>
                <td>${this._n(h.quota ?? 0)}</td>
                <td>${this._n(h.sent ?? 0)}${overBadge}</td>
                <td>${h.advanced
                    ? '<span class="badge badge-success">Stepped up</span>'
                    : `<span class="badge badge-warning" title="Fewer than ${this._n(w.advance_min_percent)}% of the planned volume was sent, so the ramp waited">Held</span>`}</td>
            </tr>`;
            const older = hist[i + 1];
            if (older?.date) {
                const gap = Math.round((parseDay(h.date) - parseDay(older.date)) / dayMs) - 1;
                if (gap > 0) {
                    const first = fmtDay(parseDay(older.date) + dayMs);
                    const last = fmtDay(parseDay(h.date) - dayMs);
                    row += `
            <tr class="row-muted">
                <td>${gap === 1 ? first : `${first} – ${last}`}</td>
                <td colspan="4">Not recorded: this app was not running, so ${gap === 1 ? 'that day was' : `those ${gap} days were`} neither counted nor capped.</td>
            </tr>`;
                }
            }
            return row;
        }).join('');

        return `
            <div class="card" style="margin-bottom:20px">
                <div class="card-header warmup-head">
                    <h2 class="card-title">Sending Warm-up</h2>
                    <div class="action-btns" id="warmupBadge">${this._warmupBadge(w)}</div>
                </div>
                <div style="display:flex;align-items:baseline;gap:10px;flex-wrap:wrap">
                    <span style="font-size:2rem;font-weight:700;line-height:1" id="warmupSent">${this._n(w.sent_today)}</span>
                    <span style="color:var(--text-secondary)">of <strong id="warmupQuota">${this._n(w.quota)}</strong> emails sent today
                        <span style="color:var(--text-muted)">(${App.escapeHtml(w.timezone)})</span></span>
                </div>
                <div class="progress-bar" role="progressbar" aria-label="Today's warm-up quota used" aria-valuemin="0" aria-valuemax="100" aria-valuenow="${pct}" id="warmupBar">
                    <div class="progress-fill" id="warmupFill" style="width:${pct}%"></div>
                </div>
                <p style="font-size:0.85rem;color:var(--text-muted);margin-top:10px" id="warmupProgress">${this._warmupProgressText(w)}</p>
            </div>

            <div class="card" style="margin-bottom:20px">
                <div class="card-header"><h2 class="card-title">Warm-up Plan</h2></div>

                <div style="margin-bottom:20px">
                ${this._switchRow({
                    id: 'warmupEnabled', checked: !!w.enabled, onchange: 'Settings.toggleWarmup(this.checked)',
                    title: 'Limit daily sending (warm-up)',
                    desc: "All campaigns together may send only today's quota. Running campaigns pause when it is used up and resume on their own the next day. Manually paused campaigns are never touched.",
                })}
                </div>

                <div class="form-group">
                    <label>Start from a preset</label>
                    <div class="warmup-presets">${presetButtons}</div>
                    <p style="font-size:0.8rem;color:var(--text-muted);margin-top:8px">Presets only fill in the start volume and daily increase; your target stays as set below.</p>
                </div>

                <div class="warmup-fields">
                    <div class="form-group" style="margin-bottom:0">
                        <label for="warmupStart">Day 1 volume</label>
                        <input type="number" id="warmupStart" min="1" max="10000" step="1" value="${w.start_volume}" oninput="Settings._schedulePreview()">
                    </div>
                    <div class="form-group" style="margin-bottom:0">
                        <label for="warmupGrowth">Daily increase (%)</label>
                        <input type="number" id="warmupGrowth" min="0" max="300" step="1" value="${w.growth_percent}" oninput="Settings._schedulePreview()">
                    </div>
                    <div class="form-group" style="margin-bottom:0">
                        <label for="warmupTarget">Target per day</label>
                        <input type="number" id="warmupTarget" min="1" max="100000" step="1" value="${w.target_volume}" oninput="Settings._schedulePreview()">
                    </div>
                    <div class="form-group" style="margin-bottom:0">
                        <label for="warmupLevel">Current ramp day</label>
                        <input type="number" id="warmupLevel" min="1" max="365" step="1" value="${w.day}">
                    </div>
                    <div class="form-group" style="margin-bottom:0">
                        <label for="warmupTimezone">Day starts at midnight in</label>
                        <select id="warmupTimezone">${tzOptions}</select>
                    </div>
                </div>
                <p style="font-size:0.8rem;color:var(--text-muted);margin:12px 0 0">
                    Each day grows by the percentage but by at least one email. 100% doubles every day (10, 20, 40, 80 ...).
                    Already sending for a while? Set <strong>Current ramp day</strong> to where you are. Set it to 1 to start over.
                </p>

                <div style="margin-top:24px">
                    <label style="font-size:0.85rem;font-weight:500;color:var(--text-secondary);margin-bottom:10px;display:block">Ramp preview (emails per day)</label>
                    <div id="warmupRamp" aria-live="polite"></div>
                </div>

                <div class="form-actions">
                    <button class="btn btn-primary" onclick="Settings.saveWarmup()">Save Plan</button>
                </div>
            </div>

            <div class="card" style="margin-bottom:20px">
                <div class="card-header"><h2 class="card-title">Queue &amp; Sending Speed</h2></div>
                <div id="warmupInsights" style="font-size:0.9rem;color:var(--text-secondary)">Checking the campaign queue...</div>
            </div>

            ${historyRows ? `
            <div class="card" style="margin-bottom:20px">
                <div class="card-header"><h2 class="card-title">Recent Days</h2></div>
                <div class="table-wrapper" tabindex="0" role="region" aria-label="Scrollable data table"><table>
                    <thead><tr><th>Date</th><th>Ramp day</th><th>Planned</th><th>Sent</th><th>Result</th></tr></thead>
                    <tbody>${historyRows}</tbody>
                </table></div>
            </div>` : ''}

            <div class="card">
                <div class="card-header"><h2 class="card-title">How it works</h2></div>
                <div style="font-size:0.85rem;color:var(--text-secondary);line-height:1.8">
                    <ul style="padding-left:20px;margin:8px 0">
                        <li>The quota is checked <strong>every second</strong> while a campaign is running. When it is used up, running campaigns are <strong>auto-paused</strong> and resume on their own the next day.</li>
                        <li>The ramp <strong>steps up only after a day that sent at least ${this._n(w.advance_min_percent)}%</strong> of its planned volume. Weekends, an empty queue or a pause never push you to a bigger volume on a cold domain.</li>
                        <li>It works together with the <strong>Campaign Scheduler</strong>: a campaign sends only inside the send window <em>and</em> while quota is left.</li>
                        <li>The limit can be exceeded by whatever ListMonk sends between two checks (about a second of sending). Keep ListMonk's message rate low for a tight cap, see the box above.</li>
                        <li>This app must be running for the limit to hold: if it is stopped, ListMonk keeps sending without one.</li>
                    </ul>
                </div>
            </div>
        `;
    },

    /** Update the live numbers in place (no re-render, so unsaved edits survive). */
    _syncWarmupStatus() {
        const w = this.warmup;
        if (!w) return;
        const set = (id, text) => { const el = document.getElementById(id); if (el) el.textContent = text; };
        const badge = document.getElementById('warmupBadge');
        if (badge) badge.innerHTML = this._warmupBadge(w);
        set('warmupSent', this._n(w.sent_today));
        set('warmupQuota', this._n(w.quota));
        set('warmupProgress', this._warmupProgressText(w));
        const pct = w.quota > 0 ? Math.min(100, Math.round(w.sent_today / w.quota * 100)) : 0;
        const fill = document.getElementById('warmupFill');
        if (fill) fill.style.width = pct + '%';
        document.getElementById('warmupBar')?.setAttribute('aria-valuenow', String(pct));
        const box = document.getElementById('warmupEnabled');
        if (box) {
            box.checked = !!w.enabled;
            box.closest('.switch-row')?.classList.toggle('on', !!w.enabled);
        }
    },

    _warmupForm() {
        const read = id => {
            const raw = document.getElementById(id)?.value ?? '';
            return /^\d+$/.test(String(raw).trim()) ? parseInt(raw, 10) : NaN;
        };
        return {
            start_volume: read('warmupStart'),
            growth_percent: read('warmupGrowth'),
            target_volume: read('warmupTarget'),
            level: read('warmupLevel'),
            timezone: document.getElementById('warmupTimezone')?.value,
        };
    },

    applyWarmupPreset(key) {
        const p = this.warmup?.presets?.[key];
        if (!p) return;
        document.getElementById('warmupStart').value = p.start_volume;
        document.getElementById('warmupGrowth').value = p.growth_percent;
        this._schedulePreview();
    },

    /** Debounced: redraw the ramp chart while the user types. */
    _schedulePreview() {
        clearTimeout(this._previewTimer);
        this._previewTimer = setTimeout(() => this._previewWarmup(), 250);
    },

    async _previewWarmup() {
        const el = document.getElementById('warmupRamp');
        if (!el) return;
        const f = this._warmupForm();
        if ([f.start_volume, f.growth_percent, f.target_volume].some(Number.isNaN)) {
            el.innerHTML = '<p style="color:var(--text-muted);font-size:0.85rem">Enter whole numbers to see the ramp.</p>';
            return;
        }
        if (f.target_volume < f.start_volume) {
            el.innerHTML = '<p style="color:var(--warning);font-size:0.85rem">The target must be at least the day 1 volume.</p>';
            return;
        }
        const seq = ++this._previewSeq;
        let data;
        try {
            data = await API.get(`/api/warmup/preview?start_volume=${f.start_volume}&growth_percent=${f.growth_percent}&target_volume=${f.target_volume}`, true);
        } catch {
            if (seq === this._previewSeq && document.getElementById('warmupRamp') === el) {
                el.innerHTML = '<p style="color:var(--text-muted);font-size:0.85rem">Preview unavailable. Check the values (day 1: 1-10,000, increase: 0-300%, target: up to 100,000).</p>';
            }
            return;
        }
        // A newer keystroke (or leaving the tab) superseded this response.
        if (seq !== this._previewSeq || document.getElementById('warmupRamp') !== el) return;

        // Keep today's ramp day in view: show 30 days around it.
        const current = this.warmup?.enabled ? this.warmup?.day : null;
        const startIdx = current && current > 30 ? Math.min(current - 15, Math.max(0, data.ramp.length - 30)) : 0;
        const shown = data.ramp.slice(startIdx, startIdx + 30);
        const top = Math.max(...shown, 1);
        const bars = shown.map((v, i) => {
            const day = startIdx + i + 1;
            const here = day === current;
            return `<div class="warmup-bar ${here ? 'is-current' : ''}" style="height:${Math.max(3, Math.round(v / top * 100))}%" title="Day ${day}: ${this._n(v)} emails${here ? ' (today)' : ''}">${here ? '<span class="warmup-today">Today</span>' : ''}</div>`;
        }).join('');
        const more = data.ramp.length > shown.length
            ? ` (chart shows days ${startIdx + 1}–${startIdx + shown.length})` : '';
        el.innerHTML = `
            <div class="warmup-chart">
                <div class="warmup-yaxis" aria-hidden="true"><span>${this._n(top)}</span><span>${this._n(Math.round(top / 2))}</span><span>0</span></div>
                <div class="warmup-bars" role="img" aria-label="Daily volume by ramp day">${bars}</div>
            </div>
            <div class="warmup-ramp-axis"><span>Day ${startIdx + 1}: ${this._n(shown[0])}</span>${current ? `<span>Today: day ${this._n(current)}</span>` : ''}<span>Day ${startIdx + shown.length}: ${this._n(shown[shown.length - 1])}</span></div>
            <p style="font-size:0.85rem;color:var(--text-secondary);margin-top:8px">
                Reaches <strong>${this._n(f.target_volume)}/day on day ${this._n(data.days)}</strong>${more};
                ${this._n(data.total)} emails in total by then.
            </p>`;
    },

    async _loadWarmupInsights() {
        const el = document.getElementById('warmupInsights');
        if (!el) return;
        let d;
        try {
            d = await API.get('/api/warmup/insights', true);
        } catch {
            if (document.getElementById('warmupInsights') === el) {
                el.textContent = 'The campaign queue could not be read right now.';
            }
            return;
        }
        if (document.getElementById('warmupInsights') !== el) return;
        const lines = [];
        if (d.backlog === null) {
            lines.push('Could not read the campaign queue from ListMonk.');
        } else if (d.backlog === 0) {
            lines.push('No running or paused campaign has recipients waiting.');
        } else {
            const days = d.days_to_finish;
            const eta = days === 0 ? 'today\'s remaining quota covers it'
                : days >= 365 ? 'more than a year at this ramp'
                : `about ${this._n(days)} more day${days === 1 ? '' : 's'} after today at this ramp`;
            lines.push(`<strong>${this._n(d.backlog)}</strong> recipients are waiting in running or paused campaigns: ${eta}.`);
        }
        if (d.max_per_second) {
            lines.push(`ListMonk may send up to <strong>${this._n(d.max_per_second)}</strong> email${d.max_per_second === 1 ? '' : 's'} per second (message rate ${this._n(d.message_rate)} x concurrency ${this._n(d.concurrency)}).`);
            if (d.overshoot_warning) {
                lines.push('<span style="color:var(--warning)">That is fast compared with today\'s quota, so the daily limit could be overshot by a noticeable share. Lower the message rate and concurrency on the Deliverability page for a tighter cap.</span>');
            }
        }
        el.innerHTML = lines.map(l => `<p style="margin:0 0 8px">${l}</p>`).join('');
    },

    async toggleWarmup(checked) {
        try {
            const res = await API.put('/api/warmup', { enabled: checked });
            this.warmup = res.warmup;
            App.toast(checked ? 'Warm-up enabled' : 'Warm-up disabled', 'success');
            this._syncWarmupStatus();
        } catch {
            // API.request already surfaced the server message; put the switch back.
            const box = document.getElementById('warmupEnabled');
            if (box) box.checked = !checked;
        }
    },

    async saveWarmup() {
        const f = this._warmupForm();
        if ([f.start_volume, f.growth_percent, f.target_volume, f.level].some(Number.isNaN)) {
            App.toast('Use whole numbers in every field', 'error');
            return;
        }
        if (f.target_volume < f.start_volume) {
            App.toast('The target must be at least the day 1 volume', 'error');
            return;
        }
        const body = {
            start_volume: f.start_volume,
            growth_percent: f.growth_percent,
            target_volume: f.target_volume,
            timezone: f.timezone,
        };
        // Only move the ramp when the user actually typed a different day.
        if (f.level !== this.warmup.day) body.level = f.level;
        try {
            const res = await API.put('/api/warmup', body);
            this.warmup = res.warmup;
            App.toast('Warm-up plan saved', 'success');
            this.renderTab();
        } catch {
            // API.request already surfaced the server message.
        }
    },

    // ── Email Cleanup Tab ────────────────────────────────
    WEEKDAYS: ['Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday'],

    /** An ISO instant shown in the schedule's own timezone, so "03:00" means 03:00. */
    _cleanupWhen(iso, tz) {
        if (!iso) return '-';
        try {
            const text = new Date(iso).toLocaleString([], {
                timeZone: tz || undefined, weekday: 'short', month: 'short', day: 'numeric',
                hour: '2-digit', minute: '2-digit',
            });
            return App.escapeHtml(`${text}${tz ? ' (' + tz + ')' : ''}`);
        } catch {
            return App.escapeHtml(App.formatDateTime(iso));
        }
    },

    _cleanupBadge(c) {
        const style = 'font-size:0.9rem;padding:6px 14px';
        const job = c.job || {};
        if (job.running) {
            const label = job.kind === 'cleanup' ? 'CLEANING…' : 'SCAN RUNNING…';
            return `<span class="badge badge-warning" style="${style}">${label}</span>`;
        }
        if (c.enabled) {
            return `<span class="badge badge-success" style="${style}">ON - next run ${this._cleanupWhen(c.next_run, c.timezone)}</span>`;
        }
        return `<span class="badge badge-default" style="${style}">OFF</span>`;
    },

    /** Progress bar while a job runs, otherwise nothing. */
    _cleanupProgressHtml(c) {
        const job = c.job || {};
        if (!job.running) return '';
        const pct = job.total > 0 ? Math.min(100, Math.round(job.checked / job.total * 100)) : 0;
        const what = job.kind === 'cleanup' ? 'Cleanup' : 'A scan';
        return `
            <div class="progress-bar" role="progressbar" aria-label="Cleanup progress" aria-valuemin="0" aria-valuemax="100" aria-valuenow="${pct}">
                <div class="progress-fill" style="width:${pct}%"></div>
            </div>
            <p style="font-size:0.85rem;color:var(--text-muted);margin:10px 0 0">
                ${what} is running: ${this._n(job.checked)} of ${job.total ? this._n(job.total) : '…'} subscribers checked
                (${App.escapeHtml(job.phase || '')})
                <button class="btn btn-sm" style="margin-left:8px" onclick="Settings.cancelCleanup()" ${job.cancel_requested ? 'disabled' : ''}>${job.cancel_requested ? 'Stopping…' : 'Cancel'}</button>
            </p>`;
    },

    _cleanupLastRunHtml(c) {
        const r = c.last_run;
        if (!r) {
            return '<p style="font-size:0.9rem;color:var(--text-muted);margin:12px 0 0">No cleanup has run yet.</p>';
        }
        const tone = { ok: 'success', held: 'warning', failed: 'danger', cancelled: 'default' }[r.status] || 'default';
        const label = { ok: 'Completed', held: 'Held for review', failed: 'Failed', cancelled: 'Stopped' }[r.status] || r.status;
        const tile = (k, v) => `<div class="cell"><div class="k">${k}</div><div class="v">${this._n(v || 0)}</div></div>`;
        return `
            <div style="display:flex;align-items:center;gap:10px;flex-wrap:wrap;margin:16px 0 10px">
                <span class="badge badge-${tone}">${App.escapeHtml(label)}</span>
                <span style="font-size:0.85rem;color:var(--text-secondary)">Last cleanup ${this._cleanupWhen(r.at, c.timezone)}</span>
            </div>
            <div class="import-summary">
                ${tile('Checked', r.checked)}${tile('Dead found', r.candidates)}${tile('Blocklisted', r.blocklisted)}${tile('Not blocklisted', r.failed_blocklist)}
            </div>
            ${r.message ? `<p style="font-size:0.85rem;color:var(--text-secondary);margin:0">${App.escapeHtml(r.message)}</p>` : ''}
            ${r.status === 'held' ? `<p style="font-size:0.85rem;margin:8px 0 0"><a href="#/validation?verdict=invalid">Review these addresses on the Email Validation page</a>, then blocklist the ones you agree with.</p>` : ''}`;
    },

    _cleanupRecentHtml(c) {
        const rows = (c.recent_cleaned || []).slice(0, 15);
        if (!rows.length) return '';
        const reasonLabel = (r) => (typeof Validation !== 'undefined' ? Validation.reasonLabel(r) : r);
        const body = rows.map(r => `
            <tr>
                <td>${this._cleanupWhen(r.at, c.timezone)}</td>
                <td><strong class="cell-email">${App.escapeHtml(r.email || '')}</strong></td>
                <td>${App.escapeHtml((r.reasons || []).map(reasonLabel).join(', '))}</td>
            </tr>`).join('');
        return `
            <div class="card" style="margin-bottom:20px">
                <div class="card-header"><h2 class="card-title">Recently Blocklisted</h2></div>
                <p style="font-size:0.85rem;color:var(--text-muted);margin:0 0 12px">
                    Blocklisted, never deleted. To bring one back, find it on the Leads page (filter Status: Blocklisted) and press Unblock.
                </p>
                <div class="table-wrapper" tabindex="0" role="region" aria-label="Scrollable data table"><table>
                    <thead><tr><th>When</th><th>Email</th><th>Why</th></tr></thead>
                    <tbody>${body}</tbody>
                </table></div>
            </div>`;
    },

    renderCleanup() {
        const c = this.cleanup;
        if (!c) {
            return `<div class="card"><div class="empty-state"><h2>Email cleanup unavailable</h2>
                <p>The cleanup settings could not be loaded. Reload the page to try again.</p></div></div>`;
        }
        const [hh, mm] = String(c.time || '03:00').split(':').map(n => parseInt(n, 10));
        const hourOptions = Array.from({ length: 24 }, (_, h) => {
            const label = h === 0 ? '12 AM' : h < 12 ? `${h} AM` : h === 12 ? '12 PM' : `${h - 12} PM`;
            return `<option value="${h}" ${h === hh ? 'selected' : ''}>${label}</option>`;
        }).join('');
        // The stored minute is always offered, even off the 15-minute grid.
        const minutes = [0, 15, 30, 45];
        if (Number.isInteger(mm) && !minutes.includes(mm)) { minutes.push(mm); minutes.sort((a, b) => a - b); }
        const minuteOptions = minutes.map(m =>
            `<option value="${m}" ${m === mm ? 'selected' : ''}>${String(m).padStart(2, '0')}</option>`).join('');

        const timezones = [
            'US/Eastern', 'US/Central', 'US/Mountain', 'US/Pacific',
            'UTC', 'Europe/London', 'Europe/Berlin', 'Europe/Paris',
            'Asia/Tokyo', 'Asia/Shanghai', 'Asia/Kolkata', 'Asia/Dhaka',
            'Australia/Sydney', 'Pacific/Auckland',
        ];
        if (c.timezone && !timezones.includes(c.timezone)) timezones.unshift(c.timezone);
        const tzOptions = timezones.map(tz =>
            `<option value="${App.escapeHtml(tz)}" ${tz === c.timezone ? 'selected' : ''}>${App.escapeHtml(tz)}</option>`).join('');
        const weekdayOptions = this.WEEKDAYS.map((d, i) =>
            `<option value="${i}" ${i === c.weekday ? 'selected' : ''}>${d}</option>`).join('');
        const running = !!c.job?.running;

        return `
            <div class="card" style="margin-bottom:20px">
                <div class="card-header warmup-head">
                    <h2 class="card-title">Automatic Email Cleanup</h2>
                    <div class="action-btns" id="cleanupBadge">${this._cleanupBadge(c)}</div>
                </div>
                <p style="font-size:0.9rem;color:var(--text-secondary);margin:0">
                    Finds addresses that can never receive mail and <strong>blocklists</strong> them, so they stop hurting your sender reputation.
                    Nothing is deleted.
                </p>
                <div id="cleanupProgress" style="margin-top:14px">${this._cleanupProgressHtml(c)}</div>
                <div id="cleanupLastRun">${this._cleanupLastRunHtml(c)}</div>
            </div>

            <div class="card" style="margin-bottom:20px">
                <div class="card-header"><h2 class="card-title">Schedule</h2></div>

                <div class="form-group" style="margin-bottom:20px">
                    <label class="checkbox-label ${c.enabled ? 'toggle-on' : ''}" id="cleanupEnabledBanner" style="font-size:1rem;padding:12px 18px;border-width:2px;display:flex;width:100%">
                        <input type="checkbox" id="cleanupEnabled" ${c.enabled ? 'checked' : ''} onchange="Settings.toggleCleanup(this.checked)">
                        <span style="font-weight:600" id="cleanupEnabledLabel">${c.enabled ? 'Automatic cleanup is ON' : 'Automatic cleanup is OFF'}</span>
                        <span class="banner-switch" aria-hidden="true"></span>
                    </label>
                    <p style="font-size:0.85rem;color:var(--text-muted);margin-top:8px">
                        While on, every enabled subscriber is checked on the schedule below. If the app was stopped when a run was due, it runs once after it starts again.
                    </p>
                </div>

                <div class="warmup-fields">
                    <div class="form-group" style="margin-bottom:0">
                        <label for="cleanupFrequency">How often</label>
                        <select id="cleanupFrequency" onchange="Settings._cleanupFreqChanged()">
                            <option value="daily" ${c.frequency === 'daily' ? 'selected' : ''}>Every day</option>
                            <option value="weekly" ${c.frequency === 'weekly' ? 'selected' : ''}>Every week</option>
                            <option value="monthly" ${c.frequency === 'monthly' ? 'selected' : ''}>Every month</option>
                        </select>
                    </div>
                    <div class="form-group" style="margin-bottom:0" id="cleanupWeekdayWrap">
                        <label for="cleanupWeekday">On</label>
                        <select id="cleanupWeekday">${weekdayOptions}</select>
                    </div>
                    <div class="form-group" style="margin-bottom:0" id="cleanupDomWrap">
                        <label for="cleanupDom">On day of the month</label>
                        <input type="number" id="cleanupDom" min="1" max="28" step="1" value="${c.day_of_month}">
                    </div>
                    <div class="form-group" style="margin-bottom:0">
                        <label for="cleanupHour">At</label>
                        <div style="display:flex;gap:4px">
                            <select id="cleanupHour" aria-label="Hour" style="width:auto">${hourOptions}</select>
                            <select id="cleanupMin" aria-label="Minute" style="width:auto">${minuteOptions}</select>
                        </div>
                    </div>
                    <div class="form-group" style="margin-bottom:0">
                        <label for="cleanupTimezone">Timezone</label>
                        <select id="cleanupTimezone">${tzOptions}</select>
                    </div>
                </div>
                <p style="font-size:0.8rem;color:var(--text-muted);margin:12px 0 0">Monthly runs use days 1-28 so every month has the day.</p>

                <div class="form-group" style="margin-top:20px">
                    <label>What counts as dead</label>
                    <div class="checkbox-group">
                        <label class="checkbox-label"><input type="checkbox" id="cleanupInvalid" ${c.clean_invalid ? 'checked' : ''}><span>Invalid addresses</span></label>
                        <label class="checkbox-label"><input type="checkbox" id="cleanupBounces" ${c.clean_hard_bounces ? 'checked' : ''}><span>Hard-bounced contacts</span></label>
                    </div>
                    <p style="font-size:0.8rem;color:var(--text-muted);margin-top:8px">
                        <strong>Invalid</strong> = malformed, domain does not exist, or no mail server.
                        <strong>Hard-bounced</strong> = a permanent bounce on record (people who opened that campaign are never counted).
                        Disposable domains, shared inboxes like <code>support@</code> and typos are only reported on the Email Validation page, never blocklisted automatically.
                    </p>
                </div>

                <div class="form-actions">
                    <button class="btn btn-primary" onclick="Settings.saveCleanup()">Save Schedule</button>
                    <button class="btn" id="cleanupRunBtn" onclick="Settings.runCleanupNow()" ${running ? 'disabled' : ''}>Run Cleanup Now</button>
                    <a class="btn" href="#/validation">Open Email Validation</a>
                </div>
            </div>

            <div id="cleanupRecent">${this._cleanupRecentHtml(c)}</div>

            <div class="card">
                <div class="card-header"><h2 class="card-title">How it works</h2></div>
                <div style="font-size:0.85rem;color:var(--text-secondary);line-height:1.8">
                    <ul style="padding-left:20px;margin:8px 0">
                        <li>Each address is checked for a valid format and for a domain that exists and <strong>can receive mail</strong> (MX / Null MX records). No email is ever sent.</li>
                        <li>A <strong>DNS timeout or error never counts as dead</strong>: those addresses are only listed as "Unknown". Before any run the app confirms DNS works, otherwise it stops without changing anything.</li>
                        <li>If a run would blocklist <strong>more than ${this._n(c.hold_percent || 20)}%</strong> of your subscribers, it is <strong>held for review</strong>: nothing is blocklisted and you decide on the Email Validation page. That size usually means a data problem, not dead addresses.</li>
                        <li>It cannot confirm that a specific mailbox exists (that would need an SMTP probe, which is unreliable and can hurt your sending IP). Addresses that pass here can still bounce.</li>
                    </ul>
                </div>
            </div>
        `;
    },

    /** Show only the field the chosen frequency needs. */
    _cleanupFreqChanged() {
        const freq = document.getElementById('cleanupFrequency')?.value;
        const wk = document.getElementById('cleanupWeekdayWrap');
        const dom = document.getElementById('cleanupDomWrap');
        if (wk) wk.style.display = freq === 'weekly' ? '' : 'none';
        if (dom) dom.style.display = freq === 'monthly' ? '' : 'none';
    },

    /** Update the live parts in place (never re-render: unsaved edits survive). */
    _syncCleanupStatus() {
        const c = this.cleanup;
        if (!c) return;
        const put = (id, html) => { const el = document.getElementById(id); if (el) el.innerHTML = html; };
        put('cleanupBadge', this._cleanupBadge(c));
        put('cleanupProgress', this._cleanupProgressHtml(c));
        put('cleanupLastRun', this._cleanupLastRunHtml(c));
        put('cleanupRecent', this._cleanupRecentHtml(c));
        const label = document.getElementById('cleanupEnabledLabel');
        const banner = document.getElementById('cleanupEnabledBanner');
        const box = document.getElementById('cleanupEnabled');
        if (label) label.textContent = c.enabled ? 'Automatic cleanup is ON' : 'Automatic cleanup is OFF';
        if (banner) banner.classList.toggle('toggle-on', !!c.enabled);
        if (box) box.checked = !!c.enabled;
        const run = document.getElementById('cleanupRunBtn');
        if (run) run.disabled = !!c.job?.running;
    },

    /** The schedule form as a settings patch, or null (with a toast) if invalid. */
    _cleanupForm() {
        const val = (id) => document.getElementById(id)?.value;
        const hour = parseInt(val('cleanupHour'), 10);
        const minute = parseInt(val('cleanupMin'), 10);
        const form = {
            frequency: val('cleanupFrequency'),
            time: `${String(hour).padStart(2, '0')}:${String(minute).padStart(2, '0')}`,
            weekday: parseInt(val('cleanupWeekday'), 10),
            day_of_month: parseInt(val('cleanupDom'), 10),
            timezone: val('cleanupTimezone'),
            clean_invalid: !!document.getElementById('cleanupInvalid')?.checked,
            clean_hard_bounces: !!document.getElementById('cleanupBounces')?.checked,
        };
        if (Number.isNaN(hour) || Number.isNaN(minute)) {
            App.toast('Pick a time of day', 'error');
            return null;
        }
        if (Number.isNaN(form.day_of_month) || form.day_of_month < 1 || form.day_of_month > 28) {
            App.toast('Day of the month must be between 1 and 28', 'error');
            return null;
        }
        if (Number.isNaN(form.weekday)) {
            App.toast('Pick a day of the week', 'error');
            return null;
        }
        return form;
    },

    async saveCleanup() {
        const form = this._cleanupForm();
        if (!form) return;
        if (this.cleanup.enabled && !form.clean_invalid && !form.clean_hard_bounces) {
            App.toast('Choose at least one kind of address to clean', 'error');
            return;
        }
        try {
            const res = await API.put('/api/email-validation/cleanup', form);
            this.cleanup = res.cleanup;
            App.toast('Cleanup schedule saved', 'success');
            this.renderTab();
        } catch {
            // API.request already surfaced the server message.
        }
    },

    async toggleCleanup(checked) {
        // Turning it on applies what is on screen, so a schedule edited a
        // moment ago is not silently ignored. Turning it off needs no form.
        let body = { enabled: false };
        if (checked) {
            const form = this._cleanupForm();
            if (!form) { this._syncCleanupStatus(); return; }
            body = { ...form, enabled: true };
        }
        try {
            const res = await API.put('/api/email-validation/cleanup', body);
            this.cleanup = res.cleanup;
            App.toast(checked ? 'Automatic cleanup enabled' : 'Automatic cleanup disabled', 'success');
            this._syncCleanupStatus();
        } catch {
            // API.request already surfaced the server message; put the switch back.
            const box = document.getElementById('cleanupEnabled');
            if (box) box.checked = !checked;
        }
    },

    async runCleanupNow() {
        if (!await App.confirm('Run Email Cleanup',
                'This scans every enabled subscriber now and blocklists addresses that can never receive mail (malformed, nonexistent domain, no mail server, or hard-bounced). They are blocklisted, not deleted, and you can unblock them later. If more than ' + (this.cleanup?.hold_percent || 20) + '% would be blocklisted, nothing is changed. Continue?')) return;
        try {
            const res = await API.post('/api/email-validation/cleanup/run');
            this.cleanup = { ...this.cleanup, job: res.job };
            this._syncCleanupStatus();
            this._startCleanupPoll();
            App.toast('Cleanup started', 'info');
        } catch {
            // Already surfaced (e.g. 409 when a scan is already running).
        }
    },

    async cancelCleanup() {
        try {
            await API.post('/api/email-validation/scan/cancel');
            App.toast('Stopping after the current batch…', 'info');
        } catch {
            // Already surfaced.
        }
    },

    _startCleanupPoll() {
        if (this._cleanupPoll) return;
        this._cleanupPoll = setInterval(() => this._pollCleanup(), 2000);
    },

    _stopCleanupPoll() {
        if (this._cleanupPoll) {
            clearInterval(this._cleanupPoll);
            this._cleanupPoll = null;
        }
    },

    async _pollCleanup() {
        if (App.currentPage !== 'settings' || this.activeTab !== 'cleanup') {
            this._stopCleanupPoll();
            return;
        }
        try {
            const wasRunning = !!this.cleanup?.job?.running;
            const fresh = await API.get('/api/email-validation/cleanup', true);
            if (App.currentPage !== 'settings' || this.activeTab !== 'cleanup') return;
            this.cleanup = fresh;
            this._syncCleanupStatus();
            if (!fresh.job?.running) {
                this._stopCleanupPoll();
                const r = fresh.last_run;
                if (wasRunning && r) {
                    if (r.status === 'failed') App.toast(r.message || 'Cleanup failed', 'error');
                    else if (r.status === 'held') App.toast('Cleanup held for review: too many addresses looked dead', 'info');
                    else if (r.status === 'cancelled') App.toast('Cleanup stopped', 'info');
                    else App.toast(`Cleanup done: ${this._n(r.blocklisted)} blocklisted`, 'success');
                }
            }
        } catch {
            // Keep polling; the next tick may succeed.
        }
    },

    // ── Automation Tab ───────────────────────────────────
    /** Every rule that acts on people without a click, in one place: what an
        unsubscribe does, and the reply-inbox rules (which can blocklist real
        prospects). Email cleanup and auto-unblock keep their own tabs. */
    renderAutomation() {
        const u = this.unsubSettings;
        const unsubCard = u ? `
            <div class="card" style="margin-bottom:20px">
                <div class="card-header"><h2 class="card-title">Unsubscribe requests</h2>
                    <a class="btn btn-sm" href="#/unsubscribes">Open Unsubscribes</a></div>
                <p class="field-hint" style="margin-bottom:12px">Applies to "remove me" replies and to unsubscribe-link clicks found by the hourly scan.
                    The ${App.term('subscriber')} is always taken off every list.</p>
                ${this._switchRow({
                    id: 'unsubBlocklist', checked: !!u.blocklist_enabled,
                    onchange: 'Settings.toggleUnsubBlocklist(this.checked)',
                    title: 'Also blocklist the address',
                    desc: 'On: ListMonk will never mail them again, even after a re-import. Off: they are only unsubscribed and could be added back to a list later.',
                })}
            </div>` : `<div class="card" style="margin-bottom:20px"><p class="field-hint">Unsubscribe settings could not be loaded.</p></div>`;

        const replyCard = typeof Replies !== 'undefined'
            ? `<div id="settingsReplyAuto">${Replies.renderAutomation(true)}</div>
               <p class="field-hint" style="margin-top:8px">${App.isCold() ? '' : 'These rules belong to the Cold Email reply inbox (Replies & Leads). '}Open the inbox: <a href="#/replies">Replies &amp; Leads</a>.</p>`
            : '';
        return unsubCard + replyCard;
    },

    async toggleUnsubBlocklist(enabled) {
        if (typeof Unsubscribes === 'undefined') return;
        Unsubscribes.settings = { ...(Unsubscribes.settings || {}), ...(this.unsubSettings || {}) };
        const ok = await Unsubscribes.toggleBlocklist(enabled);
        if (ok) this.unsubSettings = { ...(this.unsubSettings || {}), blocklist_enabled: enabled };
        else {
            const box = document.getElementById('unsubBlocklist');
            if (box) { box.checked = !enabled; box.closest('.switch-row')?.classList.toggle('on', !enabled); }
        }
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

        const enabled = u.enabled !== false;
        const last = u.last_run;
        const lastLine = last
            ? `${App.formatDateTime(last.at)} (${last.trigger === 'manual' ? 'run by hand' : 'scheduled'}) · ${last.error
                ? `<span style="color:var(--danger)">failed: ${App.escapeHtml(last.error)}</span>`
                : `${last.success} re-enabled${last.failed ? `, ${last.failed} failed` : ''}`}`
            : 'Not run since this was installed';
        const history = u.history || [];
        const historyHtml = history.length
            ? `<div class="table-wrapper" tabindex="0" role="region" aria-label="Auto-unblock history"><table>
                    <thead><tr><th>Address</th><th>When</th><th>How</th></tr></thead>
                    <tbody>${history.map(h => `<tr>
                        <td><a href="#/subscribers?q=${encodeURIComponent(h.email)}">${App.escapeHtml(h.email)}</a></td>
                        <td>${App.formatDateTime(h.at)}</td>
                        <td>${h.trigger === 'manual' ? 'By hand' : 'Scheduled'}</td>
                    </tr>`).join('')}</tbody></table></div>
               ${u.history_total > history.length ? `<p class="field-hint">Showing the latest ${history.length} of ${App.formatNumber(u.history_total)}.</p>` : ''}`
            : '<p class="field-hint">Nobody has been re-enabled yet.</p>';

        return `
            <div class="card" style="margin-bottom:20px">
                <div class="card-header">
                    <h2 class="card-title">Auto-Unblock Status</h2>
                    <div class="action-btns">${statusBadge}</div>
                </div>
                ${this._switchRow({
                    id: 'autoUnblockEnabled', checked: enabled,
                    onchange: 'Settings.toggleAutoUnblock(this.checked)',
                    title: `Run automatically every ${interval} hours`,
                    desc: 'Off: nothing is re-enabled in the background. "Run Auto-Unblock Now" below still works.',
                })}
                <div style="display:flex;gap:20px;align-items:center;flex-wrap:wrap;margin-top:14px">
                    <div>
                        <span style="color:var(--text-muted);font-size:0.85rem">Waiting to be re-enabled:</span>
                        <strong style="color:${failed ? 'var(--text-muted)' : (count > 0 ? 'var(--warning)' : 'var(--success)')}">${failed ? '—' : count}</strong>
                    </div>
                    <div>
                        <span style="color:var(--text-muted);font-size:0.85rem">Last run:</span>
                        <strong>${lastLine}</strong>
                    </div>
                </div>
            </div>

            <div class="card" style="margin-bottom:20px">
                <div class="card-header"><h2 class="card-title">Re-enabled addresses</h2></div>
                ${historyHtml}
            </div>

            <div class="card" style="margin-bottom:20px">
                <div class="card-header"><h2 class="card-title">Manual Unblock</h2></div>
                <p style="font-size:0.85rem;color:var(--text-secondary);margin-bottom:16px">
                    Click the button below to scan for subscribers who were blocklisted because of a bounce but opened or clicked
                    a campaign <strong>after</strong> that bounce. They will be re-enabled and their bounce records will be deleted.
                    Anyone who unsubscribed, and every blocklisting made on purpose in this app (Leads, Replies, Email Validation,
                    cleanup), is never touched.
                </p>
                <button class="btn btn-primary" id="runUnblockBtn" onclick="Settings.runUnblock()">
                    Run Auto-Unblock Now
                </button>
                <div id="unblockResult" role="status" aria-live="polite" style="margin-top:16px"></div>
            </div>

            <div class="card">
                <div class="card-header"><h2 class="card-title">How it works</h2></div>
                <div style="font-size:0.85rem;color:var(--text-secondary);line-height:1.8">
                    <p><strong>Every ${interval} hours</strong>, the system automatically:</p>
                    <ul style="padding-left:20px;margin:8px 0">
                        <li>Finds subscribers who are <strong>blocklisted with a bounce record</strong> but <strong>opened or clicked after their latest bounce</strong> (engagement before a bounce proves nothing: the mailbox may have died since)</li>
                        <li>Skips anyone who unsubscribed and every blocklisting made on purpose in this app</li>
                        <li><strong>Re-enables</strong> them (removes blocklist status)</li>
                        <li><strong>Deletes</strong> their bounce records (since they clearly received the email)</li>
                    </ul>
                    <p>This protects real subscribers who get falsely bounced — e.g. security scanners that pre-scan links, or delayed bounce DSNs after the recipient already opened the email.</p>
                </div>
            </div>
        `;
    },

    async toggleAutoUnblock(enabled) {
        try {
            await API.put('/api/auto-unblock/settings', { enabled });
            this.unblockStatus = { ...(this.unblockStatus || {}), enabled };
            App.toast(`Automatic auto-unblock ${enabled ? 'on' : 'off'}`, 'success');
        } catch {
            const box = document.getElementById('autoUnblockEnabled');
            if (box) { box.checked = !enabled; box.closest('.switch-row')?.classList.toggle('on', !enabled); }
        }
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
            const res = await API.put('/api/scheduler', { enabled: checked });
            const released = (this.schedule?.auto_paused_campaigns?.length || 0)
                - (res?.schedule?.auto_paused_campaigns?.length || 0);
            App.toast(checked ? 'Scheduler enabled'
                : `Scheduler disabled${released > 0 ? ` — resumed ${released} auto-paused campaign(s)` : ''}`,
                'success');
            this.schedule = {
                ...(this.schedule || {}),
                enabled: checked,
                auto_paused_campaigns: res?.schedule?.auto_paused_campaigns
                    ?? this.schedule?.auto_paused_campaigns ?? [],
            };
            // Refresh only the banner: a full tab re-render from server state
            // used to silently discard unsaved timezone/hours/days edits.
            this._syncSchedulerBanner();
        } catch {
            // Put the checkbox back where the server says it is.
            const box = document.getElementById('schedEnabled');
            if (box) box.checked = !checked;
            // API.request already surfaced the server message.
        }
    },

    /** Update the switch row in place. */
    _syncSchedulerBanner() {
        const enabled = !!this.schedule?.enabled;
        const box = document.getElementById('schedEnabled');
        if (box) {
            box.checked = enabled;
            box.closest('.switch-row')?.classList.toggle('on', enabled);
        }
    },

    /** A compact on/off row (the same switch the Replies automation uses),
        instead of a full-width coloured banner. */
    _switchRow({ id, checked, onchange, title, desc, labelId = '' }) {
        return `<label class="switch-row settings-switch ${checked ? 'on' : ''}">
            <span class="switch">
                <input type="checkbox" id="${id}" ${checked ? 'checked' : ''} onchange="this.closest('.switch-row').classList.toggle('on', this.checked);${onchange}">
                <span class="slider"></span>
            </span>
            <span class="switch-text">
                <span class="switch-title"${labelId ? ` id="${labelId}"` : ''}>${title}</span>
                <span class="switch-desc">${desc}</span>
            </span>
        </label>`;
    },

    /** "Thu 8:00 AM" (or "today 8:00 AM") for the next opening of the send
        window, computed in the window's own timezone. */
    _nextWindowOpen(s) {
        try {
            const tz = s.timezone || 'UTC';
            const parts = Object.fromEntries(new Intl.DateTimeFormat('en-US', {
                timeZone: tz, weekday: 'short', hour: 'numeric', minute: 'numeric', hourCycle: 'h23',
            }).formatToParts(new Date()).map(p => [p.type, p.value]));
            const keys = ['sun', 'mon', 'tue', 'wed', 'thu', 'fri', 'sat'];
            const labels = ['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat'];
            const today = keys.indexOf(parts.weekday.slice(0, 3).toLowerCase());
            const nowMin = (parseInt(parts.hour, 10) % 24) * 60 + parseInt(parts.minute, 10);
            const startMin = s.start_hour * 60 + s.start_minute;
            const days = new Set(s.days || []);
            for (let i = 0; i < 8; i++) {
                const d = (today + i) % 7;
                if (!days.has(keys[d])) continue;
                if (i === 0 && startMin <= nowMin) continue;
                const when = this.formatTime(s.start_hour, s.start_minute);
                return i === 0 ? `today ${when}` : i === 1 ? `tomorrow ${when}` : `${labels[d]} ${when}`;
            }
        } catch { /* unknown timezone in this browser */ }
        return '';
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
        // Mutates real data (re-enables subscribers, deletes bounce records):
        // needs the same confirmation as every other destructive action.
        if (!await App.confirm('Run Auto-Unblock',
                'This re-enables bounce-blocklisted subscribers who opened or clicked after their latest bounce, and deletes their false bounce records. Unsubscribed addresses and deliberate blocklistings stay blocklisted. Continue?')) return;
        const btn = document.getElementById('runUnblockBtn');
        const resultEl = document.getElementById('unblockResult');
        btn.disabled = true;
        btn.textContent = 'Running…';
        resultEl.innerHTML = '';

        try {
            const result = await API.post('/api/auto-unblock/run', null, API.LONG_TIMEOUT);
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
            // Refresh status (last run + history) without losing the result line.
            this.unblockStatus = await API.get('/api/auto-unblock/status');
            const keep = resultEl.innerHTML;
            this.renderTab();
            const fresh = document.getElementById('unblockResult');
            if (fresh) fresh.innerHTML = keep;
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
            // silent: failures render into #unblockResult below; a toast on
            // every 30s poll during an outage would stack up pointlessly.
            this.schedule = await API.get('/api/scheduler', true);
            const el = document.getElementById('schedulerCurrentTime');
            if (el) el.textContent = this.schedule.current_time || '-';
        } catch {}
        if (this.activeTab === 'warmup' && this.warmup) {
            try {
                this.warmup = await API.get('/api/warmup', true);
                this._syncWarmupStatus();
            } catch {}
        }
        if (this.activeTab === 'cleanup' && this.cleanup) {
            try {
                this.cleanup = await API.get('/api/email-validation/cleanup', true);
                this._syncCleanupStatus();
                if (this.cleanup.job?.running) this._startCleanupPoll();
            } catch {}
        }
    },
};
