/**
 * Settings page - combines Scheduler, Warm-up and Auto-Unblock into one page with tabs.
 */
const Settings = {
    activeTab: 'scheduler',
    schedule: null,
    warmup: null,
    unblockStatus: null,
    _refreshInterval: null,
    _previewTimer: null,
    _previewSeq: 0,

    async render() {
        this._seq = App.seq();
        App.setContent('<div class="loading-spinner">Loading settings...</div>');

        try {
            const [sched, unblock, warmup] = await Promise.all([
                API.get('/api/scheduler'),
                API.get('/api/auto-unblock/status'),
                // Warm-up is its own feature: if it fails to load, the other
                // tabs must still work (the Warm-up tab explains the failure).
                API.get('/api/warmup', true).catch(() => null),
            ]);
            if (App.stale(this._seq)) return;
            this.schedule = sched;
            this.unblockStatus = unblock;
            this.warmup = warmup;
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
            <div class="settings-tabs">
                <button class="settings-tab ${this.activeTab === 'scheduler' ? 'active' : ''}" onclick="Settings.switchTab('scheduler')">
                    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="16" height="16"><circle cx="12" cy="12" r="10"/><polyline points="12 6 12 12 16 14"/></svg>
                    Campaign Scheduler
                </button>
                <button class="settings-tab ${this.activeTab === 'warmup' ? 'active' : ''}" onclick="Settings.switchTab('warmup')">
                    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="16" height="16"><polyline points="22 7 13.5 15.5 8.5 10.5 2 17"/><polyline points="16 7 22 7 22 13"/></svg>
                    Sending Warm-up
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
        } else if (this.activeTab === 'warmup') {
            el.innerHTML = this.renderWarmup();
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

                <div class="form-group" style="margin-bottom:20px">
                    <label class="checkbox-label ${enabled ? 'toggle-on' : ''}" id="schedEnabledBanner" style="font-size:1rem;padding:12px 18px;border-width:2px;display:flex;width:100%">
                        <input type="checkbox" id="schedEnabled" ${enabled ? 'checked' : ''} onchange="Settings.toggleScheduler(this.checked)">
                        <span style="font-weight:600" id="schedEnabledLabel">${enabled ? 'Scheduler is ON' : 'Scheduler is OFF'}</span>
                        <span class="banner-switch" aria-hidden="true"></span>
                    </label>
                    <p style="font-size:0.85rem;color:var(--text-muted);margin-top:8px">
                        When enabled, running campaigns will auto-pause outside the send window and auto-resume when the window opens.
                        Manually paused campaigns are never affected. Turning the scheduler off resumes the campaigns it auto-paused.
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

        const historyRows = (w.history || []).map(h => `
            <tr>
                <td>${App.escapeHtml(h.date || '')}</td>
                <td>${this._n(h.day ?? 0)}</td>
                <td>${this._n(h.quota ?? 0)}</td>
                <td>${this._n(h.sent ?? 0)}</td>
                <td>${h.advanced
                    ? '<span class="badge badge-success">Stepped up</span>'
                    : `<span class="badge badge-warning" title="Fewer than ${this._n(w.advance_min_percent)}% of the planned volume was sent, so the ramp waited">Held</span>`}</td>
            </tr>`).join('');

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

                <div class="form-group" style="margin-bottom:20px">
                    <label class="checkbox-label ${w.enabled ? 'toggle-on' : ''}" id="warmupEnabledBanner" style="font-size:1rem;padding:12px 18px;border-width:2px;display:flex;width:100%">
                        <input type="checkbox" id="warmupEnabled" ${w.enabled ? 'checked' : ''} onchange="Settings.toggleWarmup(this.checked)">
                        <span style="font-weight:600" id="warmupEnabledLabel">${w.enabled ? 'Warm-up is ON' : 'Warm-up is OFF'}</span>
                        <span class="banner-switch" aria-hidden="true"></span>
                    </label>
                    <p style="font-size:0.85rem;color:var(--text-muted);margin-top:8px">
                        While on, all campaigns together may send only today's quota. Running campaigns pause when it is used up
                        and resume automatically on the next day. Manually paused campaigns are never touched.
                    </p>
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
        const label = document.getElementById('warmupEnabledLabel');
        const banner = document.getElementById('warmupEnabledBanner');
        const box = document.getElementById('warmupEnabled');
        if (label) label.textContent = w.enabled ? 'Warm-up is ON' : 'Warm-up is OFF';
        if (banner) banner.classList.toggle('toggle-on', !!w.enabled);
        if (box) box.checked = !!w.enabled;
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

        const shown = data.ramp.slice(0, 30);
        const top = Math.max(...shown, 1);
        const current = this.warmup?.day;
        const bars = shown.map((v, i) => {
            const day = i + 1;
            return `<div class="warmup-bar ${day === current ? 'is-current' : ''}" style="height:${Math.max(3, Math.round(v / top * 100))}%" title="Day ${day}: ${this._n(v)} emails"></div>`;
        }).join('');
        const more = data.ramp.length > shown.length
            ? ` (chart shows the first ${shown.length})` : '';
        el.innerHTML = `
            <div class="warmup-bars" role="img" aria-label="Daily volume by ramp day">${bars}</div>
            <div class="warmup-ramp-axis"><span>Day 1: ${this._n(data.ramp[0])}</span><span>Day ${shown.length}: ${this._n(shown[shown.length - 1])}</span></div>
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
                    <h2 class="card-title">Auto-Unblock Status</h2>
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
                <div class="card-header"><h2 class="card-title">Manual Unblock</h2></div>
                <p style="font-size:0.85rem;color:var(--text-secondary);margin-bottom:16px">
                    Click the button below to scan for subscribers who opened or clicked campaigns but got blocklisted due to bounces.
                    They will be re-enabled and their bounce records will be deleted. Blocklistings without a bounce record
                    (manual, reply or import blocklists) and anyone who unsubscribed are never touched.
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
                        <li>Finds subscribers who <strong>opened or clicked</strong> any campaign but are <strong>blocklisted with a bounce record</strong> (unsubscribed addresses are always skipped)</li>
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

    /** Update the ON/OFF banner (text + switch visual) in place. */
    _syncSchedulerBanner() {
        const enabled = !!this.schedule?.enabled;
        const label = document.getElementById('schedEnabledLabel');
        const banner = document.getElementById('schedEnabledBanner');
        if (label) label.textContent = enabled ? 'Scheduler is ON' : 'Scheduler is OFF';
        if (banner) banner.classList.toggle('toggle-on', enabled);
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
                'This re-enables bounce-blocklisted subscribers who engaged (opened or clicked) and deletes their false bounce records. Unsubscribed addresses stay blocklisted. Continue?')) return;
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
    },
};
