/**
 * Deliverability & Safety Module for ListMonk Pro.
 * Pre-flight audit, scorecard, 1-click presets — and a control on every row.
 *
 * Layout: a hero (score ring, verdict, tallies, presets) above a two-pane
 * body. The left pane is a section menu — "Needs attention", "All checks",
 * one entry per audit group (the taxonomy is served by the API's `groups`
 * field) and "Email format" — and the right pane shows the chosen section as
 * its own card. The section rides in the hash (`#/deliverability?section=…`).
 *
 * Each row carries the control that changes its setting: edits are staged
 * locally and written together from the sticky save bar, because every
 * ListMonk settings write reloads the ListMonk app. Staged edits survive a
 * section switch (controls render from `pending`), and the menu marks the
 * sections that hold one. A passing check stays quiet so colour is only spent
 * on the rows that need a decision.
 *
 * Two unsubscribe controls exist on purpose: the List-Unsubscribe *header*
 * (a ListMonk setting, invisible in the message body) and the footer *link*
 * (markup in the campaign templates, toggled via /unsubscribe-link).
 *
 * The "Email format" section switches each campaign between its HTML design
 * and a plain-text version. Unlike the settings rows it applies immediately:
 * it only swaps a template (no ListMonk reload) and is undone by switching back.
 */
const Deliverability = {
    checklistData: null,
    isLoading: false,
    isSaving: false,
    // Local clock reading of the last successful fetch. Rendered next to the
    // refresh control so an operator knows whether the audit is stale.
    checkedAt: null,
    // Staged edits: setting key → new value. Cleared on save/discard/reload.
    pending: {},
    // GET /email-format result: { campaigns: [...] } or { error }.
    formats: null,
    // Campaign id whose format switch is in flight.
    switchingFormat: null,
    // Keys whose input currently holds an unparseable value (blocks Save).
    invalid: new Set(),
    // Section shown in the right pane ('attention', 'all', a group id, 'format').
    section: null,

    /**
     * `keepSection` is passed by the page's own re-renders (refresh, preset):
     * App.routeQuery() still holds the query of the original navigation, so
     * reading it again would undo a section chosen since.
     */
    async render({ keepSection = false } = {}) {
        // applyPreset()/save() re-render after an await; if the user navigated
        // away in the meantime, painting over the new page would be wrong.
        if (App.currentPage !== 'deliverability') return;
        if (!keepSection) this.section = App.routeQuery().get('section') || null;
        const fresh = this._fresh === true;
        this._fresh = false;
        this._seq = App.seq();
        this.isLoading = true;
        this.pending = {};
        this.invalid = new Set();
        App.setContent(this.loadingHtml());

        try {
            // silent: an upstream outage (now a 502) renders in this page's
            // own error state below — a toast on top would say it twice.
            // The format card degrades on its own: a campaigns read failure
            // must not take the whole audit down with it.
            const [checklist, formats] = await Promise.all([
                API.get(`/api/deliverability/checklist${fresh ? '?refresh=true' : ''}`, true),
                API.get('/api/deliverability/email-format', true)
                    .catch(err => ({ error: err?.message || 'Unknown error' })),
            ]);
            this.checklistData = checklist;
            this.formats = formats;
            this.checkedAt = new Date();
            if (App.stale(this._seq)) return;
            // Before painting: the hero draws its Refresh button disabled
            // while loading, and used to stay disabled for good.
            this.isLoading = false;
            this.renderPage();
        } catch (err) {
            console.error('Deliverability error:', err);
            if (App.stale(this._seq)) return;
            App.setContent(`<div class="empty-state"><h2>Failed to load deliverability checklist</h2><p>ListMonk could not be reached — fix the connection and refresh.</p><p>${App.escapeHtml(err?.message || '')}</p></div>`);
        } finally {
            this.isLoading = false;
        }
    },

    loadingHtml() {
        const row = `<div class="deliv-skel-row"><span class="deliv-skel deliv-skel-mark"></span><span class="deliv-skel deliv-skel-line"></span><span class="deliv-skel deliv-skel-chip"></span></div>`;
        const navItem = `<span class="deliv-skel deliv-skel-nav"></span>`;
        return `
            <div class="card deliv-hero" aria-busy="true">
                <div class="deliv-hero-main">
                    <span class="deliv-skel deliv-skel-ring"></span>
                    <div class="deliv-hero-copy">
                        <span class="deliv-skel deliv-skel-eyebrow"></span>
                        <span class="deliv-skel deliv-skel-heading"></span>
                        <span class="deliv-skel deliv-skel-copy"></span>
                        <span class="deliv-skel deliv-skel-copy short"></span>
                    </div>
                </div>
            </div>
            <div class="deliv-layout" aria-busy="true">
                <div class="deliv-nav"><div class="deliv-nav-inner">${navItem.repeat(6)}</div></div>
                <div class="card deliv-panel-card deliv-skel-panel">${row.repeat(5)}</div>
            </div>
        `;
    },

    /** Counts and buckets every view of the page is drawn from. */
    model() {
        const data = this.checklistData || {};
        const checks = Array.isArray(data.checks) ? data.checks : [];
        const groups = Array.isArray(data.groups) ? data.groups : [];
        const score = Number.isFinite(data.score) ? data.score : 0;

        const scored = checks.filter(c => c.status !== 'info');
        const passing = scored.filter(c => c.status === 'pass').length;
        const failing = scored.filter(c => c.status === 'fail');
        const warning = scored.filter(c => c.status === 'warn');
        // Failures first: the attention view is a to-do list, worst on top.
        const unresolved = failing.concat(warning);
        const infoCount = checks.length - scored.length;
        const isOptimal = score === 100 && scored.length > 0;

        // Groups arrive with label/blurb; a check whose group id is unknown
        // (older API, new rule) still renders instead of vanishing.
        const order = groups.length ? groups : [{ id: 'checks', label: 'Checks', blurb: '' }];
        const seen = new Set();
        const buckets = order.map(g => {
            const items = groups.length
                ? checks.filter(c => (c.group || null) === g.id)
                : checks;
            items.forEach(c => seen.add(c.id));
            return { group: g, items };
        });
        const orphans = checks.filter(c => !seen.has(c.id));
        if (orphans.length) {
            buckets.push({
                group: { id: 'other', label: 'Other checks', blurb: '' },
                items: orphans,
            });
        }
        const filled = buckets.filter(b => b.items.length);
        const groupOf = {};
        filled.forEach(b => b.items.forEach(c => { groupOf[c.id] = b.group; }));

        return {
            data, checks, score, scored, passing, failing, warning, unresolved,
            infoCount, isOptimal, buckets: filled, groupOf,
        };
    },

    /** The section to show: the requested one if it exists, else a default. */
    resolveSection(m) {
        const valid = new Set(['all', 'format', ...m.buckets.map(b => b.group.id)]);
        if (m.unresolved.length) valid.add('attention');
        if (this.section && valid.has(this.section)) return this.section;
        if (m.unresolved.length) return 'attention';
        return m.buckets.length ? m.buckets[0].group.id : 'all';
    },

    renderPage() {
        if (this._seq !== undefined && App.stale(this._seq)) return;
        const m = this.model();
        this._model = m;
        this.section = this.resolveSection(m);

        let html = '';
        if (m.data.error) {
            html += `
                <div class="notice notice-warning deliv-error-notice">
                    ${_dSvgIcon('alert')}
                    <div>
                        <strong>Listmonk settings could not be read.</strong>
                        The audit below is empty — fix the connection and refresh.
                        <div class="deliv-error-detail">${App.escapeHtml(m.data.error)}</div>
                    </div>
                </div>
            `;
        }

        html += this.heroHtml(m);
        html += `
            <div class="deliv-layout">
                <nav class="deliv-nav" aria-label="Deliverability sections">${this.navHtml(m)}</nav>
                <div class="deliv-panel" id="delivPanel">${this.panelHtml(m)}</div>
            </div>
            <div class="deliv-savebar" id="delivSavebar" role="region" aria-label="Unsaved deliverability changes" hidden></div>
        `;

        App.setContent(html);
        this.refreshDirty();
    },

    /* ── Hero ────────────────────────────────────────────────────────────── */

    heroHtml(m) {
        const { score, scored, passing, failing, warning, unresolved, infoCount, isOptimal } = m;
        const newsletter = m.data.mode === 'newsletter';
        const tone = !scored.length ? 'is-empty' : (isOptimal ? 'is-optimal' : (score >= 70 ? 'is-good' : 'is-poor'));

        let verdict;
        let note;
        if (!scored.length) {
            verdict = 'No settings to audit';
            note = 'Listmonk returned no readable settings, so no checks could be scored.';
        } else if (isOptimal) {
            verdict = 'Everything scored is in place';
            note = newsletter
                ? 'Every scored setting is in place for newsletter sending: a working opt-out in header and footer, clean list hygiene and a healthy sending reputation.'
                : 'Every setting this audit scores matches the cold-outreach profile: no tracking pixels, a working opt-out in header and footer, and cadence paced by hand.';
        } else {
            verdict = `${unresolved.length} of ${scored.length} scored ${scored.length === 1 ? 'check needs' : 'checks need'} a decision`;
            note = 'Change any setting with the control on its row, then save once — or apply a preset to set the sending-engine parameters in one step.';
        }

        const chips = unresolved.slice(0, 4).map(c => `
            <button type="button" class="deliv-issue-chip is-${c.status}" onclick="Deliverability.focusCheck(${App.escapeHtml(JSON.stringify(String(c.id)))})">
                <span class="deliv-issue-dot" aria-hidden="true"></span>${App.escapeHtml(c.name)}
            </button>`).join('');
        const more = unresolved.length > 4
            ? `<button type="button" class="deliv-issue-more" onclick="Deliverability.selectSection('attention')">+${unresolved.length - 4} more</button>`
            : '';

        const profile = newsletter
            ? `<span class="deliv-profile-pill is-mode"><span class="deliv-profile-dot" aria-hidden="true"></span>Newsletter mode</span>`
            : `<span class="deliv-profile-pill ${isOptimal ? 'matched' : 'unmatched'}"><span class="deliv-profile-dot" aria-hidden="true"></span>${isOptimal ? 'Cold-email profile matched' : 'Cold-email profile not matched'}</span>`;

        const stat = (cls, n, label) => `
            <div class="deliv-stat ${cls} ${n ? '' : 'is-zero'}">
                <span class="deliv-stat-num">${n}</span>
                <span class="deliv-stat-label">${label}</span>
            </div>`;

        const checkedAt = this.checkedAt
            ? this.checkedAt.toLocaleTimeString(undefined, { hour: 'numeric', minute: '2-digit' })
            : '';

        return `
            <section class="card deliv-hero ${tone}" aria-labelledby="delivVerdict">
                <div class="deliv-hero-main">
                    ${this.ringHtml(score, scored.length)}
                    <div class="deliv-hero-copy">
                        <div class="deliv-hero-top">
                            <span class="deliv-eyebrow">Deliverability score</span>
                            ${profile}
                        </div>
                        <h2 class="deliv-verdict" id="delivVerdict">${verdict}</h2>
                        <p class="deliv-verdict-note">${note}</p>
                        ${chips ? `<div class="deliv-issues" aria-label="Checks that need a decision">${chips}${more}</div>` : ''}
                    </div>
                    <div class="deliv-hero-meta">
                        <span class="deliv-live"><span class="deliv-live-dot" aria-hidden="true"></span>Read live from Listmonk</span>
                        ${checkedAt ? `<span class="deliv-checked">Checked ${App.escapeHtml(checkedAt)}</span>` : ''}
                        <button type="button" class="btn btn-sm btn-secondary" onclick="Deliverability.refresh()" ${this.isLoading ? 'disabled' : ''}>
                            ${_dSvgIcon('refresh')}
                            Refresh
                        </button>
                    </div>
                </div>

                <div class="deliv-stats">
                    ${stat('is-pass', passing, 'Passing')}
                    ${stat('is-warn', warning.length, warning.length === 1 ? 'Warning' : 'Warnings')}
                    ${stat('is-fail', failing.length, 'Failing')}
                    ${stat('is-info', infoCount, 'Info only')}
                </div>

                ${this.presetsHtml(isOptimal && !newsletter)}
            </section>
        `;
    },

    /** Circular gauge. The arc is drawn with stroke-dasharray, so 0 and 100 are exact. */
    ringHtml(score, scoredCount) {
        const r = 52;
        const c = 2 * Math.PI * r;
        const pct = Math.max(0, Math.min(100, score));
        const dash = (c * pct) / 100;
        return `
            <div class="deliv-ring" role="img" aria-label="Score ${pct} out of 100">
                <svg viewBox="0 0 120 120" aria-hidden="true">
                    <defs>
                        <linearGradient id="delivRingGrad" x1="0" y1="0" x2="1" y2="1">
                            <stop offset="0%" class="deliv-ring-stop-a"/>
                            <stop offset="100%" class="deliv-ring-stop-b"/>
                        </linearGradient>
                    </defs>
                    <circle class="deliv-ring-track" cx="60" cy="60" r="${r}"/>
                    ${pct > 0 ? `<circle class="deliv-ring-arc" cx="60" cy="60" r="${r}"
                        stroke-dasharray="${dash.toFixed(2)} ${c.toFixed(2)}" transform="rotate(-90 60 60)"/>` : ''}
                </svg>
                <div class="deliv-ring-value">
                    <span class="deliv-score-value">${scoredCount ? pct : '—'}</span>
                    <span class="deliv-score-max">/ 100</span>
                </div>
            </div>
        `;
    },

    presetsHtml(coldActive) {
        return `
            <div class="deliv-presets">
                <div class="deliv-presets-head">
                    <span class="deliv-eyebrow">One-click presets</span>
                    <p class="deliv-preset-hint">Presets set the sending engine and tracking in one step. They never touch the footer link, bounce mailboxes or upload types.</p>
                </div>
                <div class="deliv-preset-grid">
                    <article class="deliv-preset ${coldActive ? 'is-active' : ''}">
                        <div class="deliv-preset-icon">${_dSvgIcon('send')}</div>
                        <div class="deliv-preset-body">
                            <div class="deliv-preset-title">
                                Cold email
                                ${coldActive ? `<span class="deliv-preset-badge">${_dSvgIcon('check')}Active</span>` : ''}
                            </div>
                            <p>Tracking off, one worker, small batches, public pages off. Reads like a personal email.</p>
                        </div>
                        ${coldActive
                            ? ''
                            : `<button type="button" class="btn btn-sm btn-primary" onclick="Deliverability.applyPreset('cold_email')">Apply</button>`}
                    </article>
                    <article class="deliv-preset">
                        <div class="deliv-preset-icon">${_dSvgIcon('mail')}</div>
                        <div class="deliv-preset-body">
                            <div class="deliv-preset-title">Newsletter</div>
                            <p>Open &amp; click tracking on, two workers, batches of 1,000 for opted-in lists.</p>
                        </div>
                        <button type="button" class="btn btn-sm btn-secondary" onclick="Deliverability.applyPreset('newsletter')">Apply</button>
                    </article>
                </div>
            </div>
        `;
    },

    /* ── Section menu ────────────────────────────────────────────────────── */

    /** Passed/scored tally of a group; a group with no scored rule counts its rows. */
    groupTally(items) {
        const scoredItems = items.filter(c => c.status !== 'info');
        const passed = scoredItems.filter(c => c.status === 'pass').length;
        const status = scoredItems.some(c => c.status === 'fail') ? 'fail'
            : (scoredItems.some(c => c.status === 'warn') ? 'warn'
                : (scoredItems.length ? 'pass' : 'info'));
        return { scored: scoredItems.length, passed, total: items.length, status };
    },

    navHtml(m) {
        const item = (id, icon, label, count, countCls = '') => {
            const active = this.section === id;
            return `
                <button type="button" class="deliv-nav-item ${active ? 'is-active' : ''}" data-section="${App.escapeHtml(id)}"
                    ${active ? 'aria-current="true"' : ''} onclick="Deliverability.selectSection(${App.escapeHtml(JSON.stringify(id))})">
                    <span class="deliv-nav-icon" aria-hidden="true">${icon}</span>
                    <span class="deliv-nav-label">${App.escapeHtml(label)}</span>
                    <span class="deliv-nav-dirty" title="Unsaved changes" aria-hidden="true"></span>
                    ${count !== '' ? `<span class="deliv-nav-count ${countCls}">${count}</span>` : ''}
                </button>`;
        };

        let html = '<div class="deliv-nav-inner">';
        if (m.unresolved.length) {
            html += item('attention', _dSvgIcon('alert'), 'Needs attention', m.unresolved.length,
                m.failing.length ? 'is-fail' : 'is-warn');
        }
        html += item('all', _dSvgIcon('list'), 'All checks', m.checks.length);
        if (m.buckets.length) html += `<div class="deliv-nav-title">Audit</div>`;
        m.buckets.forEach(b => {
            const t = this.groupTally(b.items);
            const count = t.scored ? `${t.passed}/${t.scored}` : `${t.total}`;
            html += item(b.group.id, _dGroupIcon(b.group.id), b.group.label, count, `is-${t.status}`);
        });
        const campaigns = Array.isArray((this.formats || {}).campaigns) ? this.formats.campaigns.length : '';
        html += `<div class="deliv-nav-title">Campaigns</div>`;
        html += item('format', _dSvgIcon('mail'), 'Email format', campaigns);
        html += `
            <p class="deliv-nav-help">
                Edits are staged until you press <b>Save changes</b>. Info rows give context and never affect the score.
            </p>
        </div>`;
        return html;
    },

    selectSection(id) {
        const m = this._model || this.model();
        this.section = id;
        this.section = this.resolveSection(m);
        App.syncUrl('deliverability', { section: this.section });
        // An input holding an unparseable value is about to leave the DOM;
        // its staged value was already dropped, so only the flag remains.
        this.invalid = new Set();

        document.querySelectorAll('.deliv-nav-item').forEach(btn => {
            const on = btn.dataset.section === this.section;
            btn.classList.toggle('is-active', on);
            if (on) btn.setAttribute('aria-current', 'true');
            else btn.removeAttribute('aria-current');
        });
        const panel = document.getElementById('delivPanel');
        if (panel) panel.innerHTML = this.panelHtml(m);
        this.refreshDirty();

        // On narrow screens the menu sits above the panel: bring the panel
        // into view when its top has scrolled away.
        if (panel && panel.getBoundingClientRect().top < 0) {
            panel.scrollIntoView({ block: 'start', behavior: _dMotion() ? 'smooth' : 'auto' });
        }
        const activeBtn = document.querySelector('.deliv-nav-item.is-active');
        if (activeBtn && activeBtn.scrollIntoView && window.matchMedia('(max-width: 1100px)').matches) {
            activeBtn.scrollIntoView({ block: 'nearest', inline: 'center', behavior: _dMotion() ? 'smooth' : 'auto' });
        }
    },

    /** Jump from a hero chip to its row in the attention list and flash it. */
    focusCheck(id) {
        this.selectSection('attention');
        const row = document.querySelector(`.deliv-check[data-check="${CSS.escape(id)}"]`);
        if (!row) return;
        row.scrollIntoView({ block: 'center', behavior: _dMotion() ? 'smooth' : 'auto' });
        row.classList.remove('is-flash');
        void row.offsetWidth;
        row.classList.add('is-flash');
        setTimeout(() => row.classList.remove('is-flash'), 1800);
    },

    /* ── Panels ──────────────────────────────────────────────────────────── */

    panelHtml(m) {
        switch (this.section) {
            case 'format':
                return this.formatHtml();
            case 'attention':
                return this.attentionHtml(m);
            case 'all':
                if (!m.buckets.length) return this.emptyAuditHtml();
                return m.buckets.map(b => this.groupHtml(b.group, b.items)).join('');
            default: {
                const bucket = m.buckets.find(b => b.group.id === this.section);
                return bucket ? this.groupHtml(bucket.group, bucket.items) : this.emptyAuditHtml();
            }
        }
    },

    emptyAuditHtml() {
        return `<section class="card deliv-panel-card"><div class="empty-state"><h2>No checks reported</h2><p>Listmonk returned an empty audit.</p></div></section>`;
    },

    panelHeadHtml({ icon, title, blurb, tally, tone = '' }) {
        return `
            <header class="deliv-panel-head">
                <span class="deliv-panel-icon ${tone}" aria-hidden="true">${icon}</span>
                <div class="deliv-panel-titles">
                    <h2 class="deliv-panel-title">${App.escapeHtml(title)}</h2>
                    ${blurb ? `<p class="deliv-panel-blurb">${blurb}</p>` : ''}
                </div>
                ${tally || ''}
            </header>
        `;
    },

    attentionHtml(m) {
        const rows = m.unresolved.map(c => this.checkRowHtml(c, m.groupOf[c.id])).join('');
        return `
            <section class="card deliv-panel-card">
                ${this.panelHeadHtml({
                    icon: _dSvgIcon('alert'),
                    title: 'Needs attention',
                    blurb: 'Failing checks first, then warnings. Change a setting on its row and save once from the bar below.',
                    tone: m.failing.length ? 'is-fail' : 'is-warn',
                    tally: `<div class="deliv-panel-tally ${m.failing.length ? 'is-fail' : 'is-warn'}">
                                <span class="deliv-panel-tally-num">${m.unresolved.length}</span>
                                <span class="deliv-panel-tally-label">to review</span>
                            </div>`,
                })}
                <ul class="deliv-checks">${rows}</ul>
            </section>
        `;
    },

    groupHtml(group, items) {
        // Keep the server's order inside a group: related controls (the
        // header and the footer link) must stay next to each other, and rows
        // must not jump around while their toggles are being flipped.
        const t = this.groupTally(items);
        const pct = t.scored ? Math.round((t.passed / t.scored) * 100) : 0;
        const tally = t.scored
            ? `<div class="deliv-panel-tally is-${t.status}">
                   <span class="deliv-panel-tally-num">${t.passed}<small>/${t.scored}</small></span>
                   <span class="deliv-panel-tally-label">passing</span>
                   <span class="deliv-panel-bar" aria-hidden="true"><i style="width:${pct}%"></i></span>
               </div>`
            : `<div class="deliv-panel-tally is-info">
                   <span class="deliv-panel-tally-num">${t.total}</span>
                   <span class="deliv-panel-tally-label">informational</span>
               </div>`;
        const rows = items.map(c => this.checkRowHtml(c)).join('');

        return `
            <section class="card deliv-panel-card" data-group="${App.escapeHtml(group.id || '')}">
                ${this.panelHeadHtml({
                    icon: _dGroupIcon(group.id),
                    title: group.label,
                    blurb: group.blurb ? App.escapeHtml(group.blurb) : '',
                    tally,
                    tone: t.status === 'fail' || t.status === 'warn' ? `is-${t.status}` : '',
                })}
                <ul class="deliv-checks">${rows}</ul>
                ${this.notesHtml(group.id)}
            </section>
        `;
    },

    checkRowHtml(check, group = null) {
        const meta = _dStatusMeta(check.status);
        const severity = check.status === 'fail' || check.status === 'warn' ? this.severityLabel(check.impact) : '';
        const target = check.status === 'pass' || check.status === 'info' ? '' : this.targetLabel(check);
        const controls = DELIV_CONTROLS[check.id] || [];
        const chips = this.valueChips(check);
        const extra = this.rowExtraHtml(check);

        return `
            <li class="deliv-check is-${check.status}" data-check="${App.escapeHtml(check.id)}">
                <span class="deliv-check-mark" aria-hidden="true">${meta.glyph}</span>
                <div class="deliv-check-main">
                    ${group ? `<span class="deliv-check-group">${App.escapeHtml(group.label)}</span>` : ''}
                    <div class="deliv-check-title">
                        <span class="deliv-check-name">${App.escapeHtml(check.name)}</span>
                        ${severity ? `<span class="deliv-check-sev is-${check.status}" title="${App.escapeHtml(check.impact)}">${severity}</span>` : ''}
                        <span class="deliv-dirty-pill">Unsaved</span>
                        <span class="sr-only">${meta.label}</span>
                    </div>
                    <p class="deliv-check-desc">${App.escapeHtml(check.description)}</p>
                    ${chips.length || target ? `
                        <div class="deliv-chips">
                            ${this.chipsHtml(chips, check.status === 'fail' ? 'bad' : '')}
                            ${target ? `<span class="deliv-chip-label">recommended</span>${this.chipsHtml([target], 'target')}` : ''}
                        </div>` : ''}
                    ${extra}
                </div>
                <div class="deliv-check-controls">
                    ${controls.length
                        ? controls.map(ctl => this.controlHtml(ctl)).join('')
                        : `<span class="deliv-readonly">${_dSvgIcon('lock')}Read-only</span>`}
                </div>
            </li>
        `;
    },

    /** Row-specific context that a bare toggle cannot convey. */
    rowExtraHtml(check) {
        const raw = (this.checklistData || {}).raw_settings || {};
        switch (check.id) {
            case 'unsubscribe_header':
                return `<p class="deliv-check-note">${_dSvgIcon('info')}<span>Invisible in the message body: Gmail, Yahoo and Apple Mail show it as an <b>Unsubscribe</b> button next to the sender. The visible link is the next setting.</span></p>`;
            case 'unsubscribe_link': {
                const link = raw.unsubscribe_link || {};
                const rows = Array.isArray(link.templates) ? link.templates : [];
                if (link.state === 'unknown') {
                    return `<p class="deliv-check-note is-warn">${_dSvgIcon('alert')}<span>Templates could not be read${link.error ? `: ${App.escapeHtml(link.error)}` : ''}.</span></p>`;
                }
                if (!rows.length) return '';
                const stateLabel = { on: 'Link shown', off: 'Link hidden', mixed: 'Partly hidden', none: 'No link in template' };
                const list = rows.map(t => `
                    <li>
                        <span class="deliv-tpl-name">${App.escapeHtml(t.name)}${t.is_default ? ' <em>default</em>' : ''}</span>
                        <span class="deliv-tpl-state is-${App.escapeHtml(t.state)}">${stateLabel[t.state] || App.escapeHtml(t.state)}</span>
                    </li>`).join('');
                const hasVisual = rows.some(t => t.type === 'campaign_visual' && t.state !== 'none');
                return `
                    <details class="deliv-tpl-details">
                        <summary>${rows.length} campaign template${rows.length === 1 ? '' : 's'}</summary>
                        <ul class="deliv-tpl-list">${list}</ul>
                        <p class="deliv-tpl-foot">
                            Running campaigns pick up the change after a pause and resume.
                            ${hasVisual ? ' Re-saving a visual template in ListMonk\'s visual editor brings its link back.' : ''}
                            Templates without a link are never edited — add <code>{{ UnsubscribeURL }}</code> in Templates.
                        </p>
                    </details>
                `;
            }
            case 'bounce_feedback':
                return `<p class="deliv-check-note">${_dSvgIcon('info')}<span>Bounce mailboxes and provider webhooks are configured in ListMonk → Settings → Bounces.</span></p>`;
            default: {
                // Server-provided next steps (sending reputation: removal
                // requests, manual lookups). External, so a new tab.
                const links = Array.isArray(check.links) ? check.links : [];
                const safe = links.filter(l => /^https:\/\//.test(String(l.url || '')));
                const steps = (Array.isArray(check.steps) ? check.steps : [])
                    .map(t => String(t ?? '').trim()).filter(Boolean);
                if (!safe.length && !steps.length) return '';
                const linkHtml = safe.map(l =>
                    `<a href="${App.escapeHtml(l.url)}" target="_blank" rel="noopener noreferrer">${App.escapeHtml(l.label)} ↗</a>`).join(' · ');
                if (!steps.length) {
                    return `<p class="deliv-check-note is-warn">${_dSvgIcon('info')}<span>${linkHtml}</span></p>`;
                }
                return `
                    <div class="deliv-check-note is-warn deliv-check-steps">
                        ${_dSvgIcon('info')}
                        <div>
                            <strong>What to do</strong>
                            <ol>${steps.map(t => `<li>${App.escapeHtml(t)}</li>`).join('')}</ol>
                            ${linkHtml ? `<p>${linkHtml}</p>` : ''}
                        </div>
                    </div>`;
            }
        }
    },

    chipsHtml(chips, tone) {
        return chips.map(chip => {
            const label = typeof chip === 'string' ? chip : chip.label;
            const chipTone = (typeof chip === 'object' && chip.tone) ? chip.tone : tone;
            const title = (typeof chip === 'object' && chip.title) ? ` title="${App.escapeHtml(chip.title)}"` : '';
            const cls = ['deliv-chip', chipTone ? `is-${chipTone}` : '', (typeof chip === 'object' && chip.wide) ? 'is-wide' : '']
                .filter(Boolean).join(' ');
            return `<span class="${cls}"${title}>${App.escapeHtml(label)}</span>`;
        }).join('');
    },

    /**
     * Facts the control itself cannot show. A toggle already says on/off and a
     * number input already shows the batch size, so those rows get no chip;
     * derived readings (msgs/day, mailbox counts, template coverage) do.
     */
    valueChips(check) {
        const raw = (this.checklistData || {}).raw_settings || {};
        const v = check.current_value;

        switch (check.id) {
            case 'bounce_feedback': {
                // Lead with what makes the check pass: "0 of 1 mailbox
                // enabled" under a green tick read as a contradiction when a
                // provider webhook is what delivers the bounces.
                const active = Number(raw.bounce_active_mailboxes) || 0;
                const total = Number(raw.bounce_mailboxes_total) || 0;
                const provider = !!raw.bounce_provider_enabled;
                const chips = [];
                if (provider) chips.push('Bounces arrive via provider webhook');
                if (active) chips.push(`${active} bounce mailbox${active === 1 ? '' : 'es'} enabled`);
                if (!provider && !active) {
                    chips.push(total ? `${total} bounce mailbox${total === 1 ? '' : 'es'}, none enabled` : 'No bounce mailbox or webhook');
                } else if (total > active) {
                    chips.push(`${total - active} more mailbox${total - active === 1 ? '' : 'es'} configured but off (not needed)`);
                }
                return chips;
            }
            case 'send_cadence': {
                const perDay = Number(raw.send_cadence_per_day);
                if (Number.isFinite(perDay) && perDay > 0) {
                    return [`≈${App.formatNumber(perDay)} msgs/day`];
                }
                return [{ label: String(v), wide: true, title: String(v) }];
            }
            case 'unsubscribe_link': {
                const counts = (raw.unsubscribe_link || {}).counts || {};
                const withLink = (counts.on || 0) + (counts.off || 0) + (counts.mixed || 0);
                if (!withLink) return [];
                const hidden = (counts.off || 0) + (counts.mixed || 0);
                return [hidden
                    ? { label: `Hidden in ${hidden} of ${withLink} templates`, tone: 'warn' }
                    : `Shown in ${withLink} of ${withLink} templates`];
            }
            case 'hidden_text': {
                const hits = Array.isArray(raw.hidden_text_templates) ? raw.hidden_text_templates : [];
                return hits.map(h => ({
                    label: `${h.name}: ~${App.formatNumber(h.chars)} hidden chars`,
                    tone: 'bad', wide: true, title: String(h.name || ''),
                }));
            }
            case 'sliding_window_rate': {
                const eff = Number(raw.sliding_window_effective_rate);
                const rate = Number(raw.sliding_window_rate);
                if (raw.sliding_window && eff && eff !== rate) {
                    return [{ label: `Really sends ${eff} per ${raw.sliding_window_duration}`, tone: 'warn', wide: true }];
                }
                return [];
            }
            case 'upload_extensions': {
                const ext = Array.isArray(raw.upload_extensions) ? raw.upload_extensions : [];
                return ext.includes('*') ? [{ label: '* wildcard allowed', tone: 'bad' }] : [];
            }
            default: {
                // Checks that describe their own facts (sending reputation).
                const own = Array.isArray(check.chips) ? check.chips : [];
                if (own.length) {
                    return own.map(c => ({ label: String(c.label ?? ''), tone: c.tone || undefined, wide: !!c.wide, title: String(c.label ?? '') }));
                }
                // No itemised facts (DKIM selector, DMARC policy …): the
                // measured value is the fact, so never leave the row bare.
                const value = check.group === 'reputation' && typeof check.current_value === 'string'
                    ? check.current_value.trim() : '';
                return value ? [{ label: value, wide: value.length > 24, title: value }] : [];
            }
        }
    },

    /** The value a failing check should hold, in the control's vocabulary. */
    targetLabel(check) {
        const ctl = (DELIV_CONTROLS[check.id] || [])[0];
        const rec = check.recommended;
        if (typeof rec === 'boolean') {
            // Inverted controls ("Open & click tracking" for disable_tracking)
            // must recommend the toggle position, not the raw setting.
            const on = ctl && ctl.invert ? !rec : rec;
            return on ? 'On' : 'Off';
        }
        const text = String(rec ?? '').trim();
        if (!text) return '';
        return { label: text, wide: text.length > 24, title: text };
    },

    severityLabel(impact) {
        const text = String(impact || '').trim().toLowerCase();
        if (text.startsWith('critical')) return 'critical';
        if (text.startsWith('high')) return 'high';
        if (text.startsWith('medium')) return 'medium';
        if (text.startsWith('low')) return 'low';
        return '';
    },

    /* ── Controls ────────────────────────────────────────────────────────── */

    /** The value currently stored in Listmonk for a control key. */
    originalValue(key) {
        const raw = (this.checklistData || {}).raw_settings || {};
        if (key === '@unsubscribe_link') {
            // 'mixed' maps to null so either toggle position is a real change
            // (otherwise "hide everywhere" could never be staged from mixed).
            const state = (raw.unsubscribe_link || {}).state;
            return state === 'on' ? true : (state === 'off' ? false : null);
        }
        const field = DELIV_RAW_FIELDS[key];
        return field ? raw[field] : undefined;
    },

    /** Staged value if any, otherwise the stored one. */
    value(key) {
        return Object.prototype.hasOwnProperty.call(this.pending, key) ? this.pending[key] : this.originalValue(key);
    },

    controlDisabled(ctl) {
        if (this.isSaving) return true;
        if (ctl.key === '@unsubscribe_link') {
            const state = (((this.checklistData || {}).raw_settings || {}).unsubscribe_link || {}).state;
            return state === 'none' || state === 'unknown' || !state;
        }
        if (ctl.dependsOn) return this.value(ctl.dependsOn) !== true;
        return false;
    },

    controlHtml(ctl) {
        const id = `deliv-ctl-${ctl.key.replace(/[^a-z0-9]/gi, '-')}`;
        const disabled = this.controlDisabled(ctl) ? 'disabled' : '';
        const value = this.value(ctl.key);
        const keyAttr = App.escapeHtml(ctl.key);

        switch (ctl.type) {
            case 'toggle': {
                const on = ctl.invert ? value !== true : value === true;
                return `
                    <label class="deliv-field deliv-field-toggle" for="${id}">
                        <span class="deliv-field-label">${App.escapeHtml(ctl.label)}</span>
                        <span class="deliv-toggle-state" data-state-for="${keyAttr}">${on ? 'On' : 'Off'}</span>
                        <span class="toggle-switch">
                            <input type="checkbox" id="${id}" role="switch" data-key="${keyAttr}" ${on ? 'checked' : ''} ${disabled}
                                onchange="Deliverability.onToggle(this)">
                            <span class="toggle-slider"></span>
                        </span>
                    </label>`;
            }
            case 'number':
                return `
                    <label class="deliv-field" for="${id}">
                        <span class="deliv-field-label">${App.escapeHtml(ctl.label)}</span>
                        <input type="number" class="deliv-input" id="${id}" data-key="${keyAttr}" inputmode="numeric"
                            min="${ctl.min}" max="${ctl.max}" step="1" value="${App.escapeHtml(value ?? '')}" ${disabled}
                            oninput="Deliverability.onNumber(this)">
                    </label>`;
            case 'duration':
                return `
                    <label class="deliv-field" for="${id}">
                        <span class="deliv-field-label">${App.escapeHtml(ctl.label)}</span>
                        <input type="text" class="deliv-input is-short" id="${id}" data-key="${keyAttr}" placeholder="1h"
                            spellcheck="false" autocomplete="off" value="${App.escapeHtml(value ?? '')}" ${disabled}
                            oninput="Deliverability.onDuration(this)">
                    </label>`;
            case 'select': {
                const opts = ctl.options.map(([v, label]) =>
                    `<option value="${App.escapeHtml(v)}" ${v === value ? 'selected' : ''}>${App.escapeHtml(label)}</option>`).join('');
                const unset = ctl.options.some(([v]) => v === value) ? '' : '<option value="" selected disabled>Not set</option>';
                return `
                    <label class="deliv-field" for="${id}">
                        <span class="deliv-field-label">${App.escapeHtml(ctl.label)}</span>
                        <select class="deliv-input" id="${id}" data-key="${keyAttr}" ${disabled}
                            onchange="Deliverability.onSelect(this)">${unset}${opts}</select>
                    </label>`;
            }
            case 'extensions': {
                const list = Array.isArray(value) ? value : [];
                const restricted = !list.includes('*');
                const explicit = list.filter(e => e !== '*').join(', ');
                return `
                    <label class="deliv-field deliv-field-toggle" for="${id}-restrict">
                        <span class="deliv-field-label">Restrict to listed types</span>
                        <span class="deliv-toggle-state" data-state-for="upload.restrict">${restricted ? 'On' : 'Off'}</span>
                        <span class="toggle-switch">
                            <input type="checkbox" id="${id}-restrict" role="switch" ${restricted ? 'checked' : ''} ${disabled}
                                onchange="Deliverability.onExtensions()">
                            <span class="toggle-slider"></span>
                        </span>
                    </label>
                    <label class="deliv-field is-stacked" for="${id}">
                        <span class="deliv-field-label">Allowed extensions</span>
                        <input type="text" class="deliv-input is-wide" id="${id}" data-key="${keyAttr}"
                            placeholder="jpg, png, gif, pdf" spellcheck="false" autocomplete="off"
                            value="${App.escapeHtml(explicit)}" ${disabled}
                            oninput="Deliverability.onExtensions()">
                    </label>`;
            }
            default:
                return '';
        }
    },

    /** Stage `value` for `key`, or un-stage it when it equals the stored value. */
    stage(key, value) {
        const original = this.originalValue(key);
        const same = Array.isArray(value)
            ? JSON.stringify(value) === JSON.stringify(original)
            : value === original;
        if (same) delete this.pending[key];
        else this.pending[key] = value;
        this.refreshDirty();
    },

    setInvalid(input, key, message) {
        if (message) this.invalid.add(key);
        else this.invalid.delete(key);
        input.classList.toggle('is-invalid', !!message);
        input.setAttribute('aria-invalid', message ? 'true' : 'false');
        input.title = message || '';
    },

    onToggle(input) {
        const key = input.dataset.key;
        const ctl = _dFindControl(key);
        const value = ctl && ctl.invert ? !input.checked : input.checked;
        const state = document.querySelector(`[data-state-for="${CSS.escape(key)}"]`);
        if (state) state.textContent = input.checked ? 'On' : 'Off';
        this.stage(key, value);
    },

    onNumber(input) {
        const key = input.dataset.key;
        const ctl = _dFindControl(key);
        const text = input.value.trim();
        const num = Number(text);
        if (!text || !Number.isInteger(num) || num < ctl.min || num > ctl.max) {
            this.setInvalid(input, key, `Enter a whole number from ${ctl.min} to ${App.formatNumber(ctl.max)}`);
            delete this.pending[key];
            this.refreshDirty();
            return;
        }
        this.setInvalid(input, key, '');
        this.stage(key, num);
    },

    onDuration(input) {
        const key = input.dataset.key;
        const text = input.value.trim().toLowerCase();
        if (!/^(?:\d+(?:h|m|s))+$/.test(text) || !/[1-9]/.test(text)) {
            this.setInvalid(input, key, 'Use a duration like 1h, 30m or 1h30m');
            delete this.pending[key];
            this.refreshDirty();
            return;
        }
        this.setInvalid(input, key, '');
        this.stage(key, text);
    },

    onSelect(input) {
        this.stage(input.dataset.key, input.value);
    },

    onExtensions() {
        const key = 'upload.extensions';
        const input = document.querySelector(`input[data-key="${key}"]`);
        const restrict = document.getElementById(`${input.id}-restrict`);
        const state = document.querySelector('[data-state-for="upload.restrict"]');
        if (state) state.textContent = restrict.checked ? 'On' : 'Off';

        const exts = [];
        for (const part of input.value.split(/[\s,;]+/)) {
            const ext = part.trim().toLowerCase().replace(/^\./, '');
            if (!ext || ext === '*') continue;
            if (!/^[a-z0-9]{1,10}$/.test(ext)) {
                this.setInvalid(input, key, `"${ext}" is not a file extension`);
                delete this.pending[key];
                this.refreshDirty();
                return;
            }
            if (!exts.includes(ext)) exts.push(ext);
        }
        if (!restrict.checked) exts.push('*');
        if (!exts.length) {
            this.setInvalid(input, key, 'List at least one file type');
            delete this.pending[key];
            this.refreshDirty();
            return;
        }
        this.setInvalid(input, key, '');
        this.stage(key, exts);
    },

    /** Sync row markers, dependent fields and the save bar with `pending`. */
    refreshDirty() {
        document.querySelectorAll('.deliv-check[data-check]').forEach(row => {
            const keys = (DELIV_CONTROLS[row.dataset.check] || []).map(c => c.key);
            const dirty = keys.some(k => k in this.pending || this.invalid.has(k));
            row.classList.toggle('is-dirty', dirty);
        });
        // Staged edits can sit in a section that is not on screen: the menu
        // marks every section holding one.
        const m = this._model;
        if (m) {
            const sections = new Set();
            Object.entries(DELIV_CONTROLS).forEach(([checkId, ctls]) => {
                if (!ctls.some(c => c.key in this.pending || this.invalid.has(c.key))) return;
                const group = m.groupOf[checkId];
                if (!group) return;
                sections.add(group.id).add('all');
                if (m.unresolved.some(c => c.id === checkId)) sections.add('attention');
            });
            document.querySelectorAll('.deliv-nav-item').forEach(btn => {
                btn.classList.toggle('is-dirty', sections.has(btn.dataset.section));
            });
        }
        // Fields that only apply while a parent toggle is on (sliding window).
        Object.values(DELIV_CONTROLS).flat().filter(c => c.dependsOn).forEach(ctl => {
            const el = document.querySelector(`[data-key="${CSS.escape(ctl.key)}"]`);
            if (el) el.disabled = this.controlDisabled(ctl);
        });

        const bar = document.getElementById('delivSavebar');
        if (!bar) return;
        const keys = Object.keys(this.pending);
        const count = keys.length;
        if (!count && !this.invalid.size) {
            bar.hidden = true;
            bar.innerHTML = '';
            return;
        }
        const summary = keys.slice(0, 3).map(k => App.escapeHtml(this.changeLabel(k))).join(' · ');
        const more = count > 3 ? ` · +${count - 3} more` : '';
        const blocked = this.invalid.size > 0;
        bar.hidden = false;
        bar.innerHTML = `
            <div class="deliv-savebar-text">
                <strong>${count ? `${count} unsaved ${count === 1 ? 'change' : 'changes'}` : 'Fix the highlighted field'}</strong>
                <span>${blocked ? 'A field holds an invalid value.' : `${summary}${more}`}</span>
            </div>
            <div class="deliv-savebar-actions">
                <button type="button" class="btn btn-secondary btn-sm" onclick="Deliverability.discard()" ${this.isSaving ? 'disabled' : ''}>Discard</button>
                <button type="button" class="btn btn-primary btn-sm" onclick="Deliverability.save()" ${this.isSaving || blocked || !count ? 'disabled' : ''}>
                    ${this.isSaving ? '<span class="spinner-sm" aria-hidden="true"></span> Saving…' : 'Save changes'}
                </button>
            </div>
        `;
    },

    /** Human description of one staged change ("Batch size → 50"). */
    changeLabel(key) {
        const ctl = _dFindControl(key);
        const value = this.pending[key];
        const label = ctl ? ctl.label : key;
        if (key === 'upload.extensions') {
            return value.includes('*') ? 'Uploads: any file type' : `Uploads: ${value.join(', ')}`;
        }
        if (typeof value === 'boolean') {
            const on = ctl && ctl.invert ? !value : value;
            return `${label} ${on ? 'on' : 'off'}`;
        }
        if (ctl && ctl.type === 'select') {
            const opt = ctl.options.find(([v]) => v === value);
            return `${label}: ${opt ? opt[1] : value}`;
        }
        return `${label} → ${typeof value === 'number' ? App.formatNumber(value) : value}`;
    },

    async refresh() {
        if (Object.keys(this.pending).length) {
            const ok = await App.confirm('Discard unsaved changes?', 'Refreshing reloads the settings from Listmonk and drops the changes you have not saved yet.');
            if (!ok) return;
        }
        // Also re-run the sending-reputation DNS checks (15-minute cache).
        this._fresh = true;
        await this.render({ keepSection: true });
    },

    discard() {
        this.pending = {};
        this.invalid = new Set();
        this.renderPage();
    },

    async save() {
        if (this.isSaving) return;
        const pending = { ...this.pending };
        const linkChange = Object.prototype.hasOwnProperty.call(pending, '@unsubscribe_link') ? pending['@unsubscribe_link'] : null;
        delete pending['@unsubscribe_link'];
        const settingsKeys = Object.keys(pending);
        if (!settingsKeys.length && linkChange === null) return;

        // Opt-out removals get spelled out: they are legal requirements, not
        // just a score. Everything else is listed so nothing saves unseen.
        const warnings = [];
        if (linkChange === false) warnings.push('The footer unsubscribe link will disappear from every campaign template. CAN-SPAM, GDPR and CASL require a visible opt-out in commercial email.');
        if (pending['privacy.unsubscribe_header'] === false) warnings.push('Gmail and Yahoo require the one-click List-Unsubscribe header from bulk senders.');
        if (Array.isArray(pending['upload.extensions']) && pending['upload.extensions'].includes('*')) warnings.push('Any file type (including .html and .js) will be accepted as an upload.');
        const changes = Object.keys(this.pending).map(k => `• ${this.changeLabel(k)}`).join('\n');
        const reload = settingsKeys.length ? '\n\nListmonk reloads its configuration after saving, which takes a few seconds.' : '';
        const confirmed = App.confirm(
            'Save deliverability changes',
            `${changes}${warnings.length ? `\n\n${warnings.join('\n')}` : ''}${reload}`
        );
        // App.confirm fills the dialog synchronously; keep the line breaks of
        // the change list (the shared dialog renders a single paragraph).
        const para = document.querySelector('#modalBody p');
        if (para) para.style.whiteSpace = 'pre-line';
        if (!(await confirmed)) return;

        this.isSaving = true;
        this.setControlsDisabled(true);
        this.refreshDirty();
        let checklist = null;
        let failed = false;
        const notes = [];
        try {
            // Templates first: a settings PUT makes ListMonk restart itself,
            // and a template write landing in that window got a 502.
            if (linkChange !== null) {
                const res = await API.put('/api/deliverability/unsubscribe-link', { enabled: linkChange }, API.LONG_TIMEOUT);
                checklist = res.checklist || checklist;
                delete this.pending['@unsubscribe_link'];
                const n = (res.updated || []).length;
                notes.push(`Footer link ${linkChange ? 'restored in' : 'hidden in'} ${n} template${n === 1 ? '' : 's'}`);
                if (res.errors) {
                    const names = (res.failed || []).map(f => f.name || f.id).join(', ');
                    App.toast(`${res.errors} template${res.errors === 1 ? '' : 's'} could not be updated: ${names}`, 'error');
                }
            }
            if (settingsKeys.length) {
                const res = await API.put('/api/deliverability/settings', pending, API.LONG_TIMEOUT);
                checklist = res.checklist || checklist;
                settingsKeys.forEach(k => delete this.pending[k]);
            }
        } catch (err) {
            // API.request already surfaced the server message. Changes that
            // did save were removed from `pending`; the rest stay staged.
            failed = true;
            console.error('Deliverability save failed:', err);
        } finally {
            this.isSaving = false;
        }

        if (App.currentPage !== 'deliverability') return;
        if (checklist) {
            this.checklistData = checklist;
            this.checkedAt = new Date();
        }
        if (!failed) {
            App.toast(notes.length ? `Saved. ${notes.join('. ')}.` : 'Deliverability settings saved', 'success');
        }
        this.invalid = new Set();
        this.renderPage();
    },

    setControlsDisabled(disabled) {
        document.querySelectorAll('.deliv-check-controls input, .deliv-check-controls select').forEach(el => {
            el.disabled = disabled;
        });
    },

    /* ── Email format ────────────────────────────────────────────────────── */

    formatHtml() {
        const data = this.formats || {};
        const rows = Array.isArray(data.campaigns) ? data.campaigns : [];
        let body;
        if (data.error) {
            body = `<p class="deliv-check-note is-warn deliv-format-empty">${_dSvgIcon('alert')}<span>Campaigns could not be read: ${App.escapeHtml(data.error)}</span></p>`;
        } else if (!rows.length) {
            body = `<p class="deliv-format-empty">No draft, scheduled, paused or running campaigns. Finished campaigns keep the format they were sent with.</p>`;
        } else {
            body = `<ul class="deliv-format-list">${rows.map(r => this.formatRowHtml(r)).join('')}</ul>`;
        }
        const plain = rows.filter(r => r.format === 'plain' || r.format === 'text').length;
        const tally = rows.length
            ? `<div class="deliv-panel-tally is-info">
                   <span class="deliv-panel-tally-num">${plain}<small>/${rows.length}</small></span>
                   <span class="deliv-panel-tally-label">plain text</span>
               </div>`
            : '';
        return `
            <section class="card deliv-panel-card deliv-format" id="delivFormat">
                ${this.panelHeadHtml({
                    icon: _dSvgIcon('mail'),
                    title: 'Email format',
                    blurb: 'Choose per campaign how the message is built. Applies immediately — no save needed.',
                    tally,
                })}
                <div class="deliv-format-compare">
                    <div class="deliv-format-option">
                        <span class="deliv-format-option-icon" aria-hidden="true">${_dSvgIcon('layout')}</span>
                        <div><b>HTML design</b><p>Your branded template: logo, layout, colours.</p></div>
                    </div>
                    <div class="deliv-format-option is-recommended">
                        <span class="deliv-format-option-icon" aria-hidden="true">${_dSvgIcon('type')}</span>
                        <div><b>Plain text <em>best for cold outreach</em></b><p>The same message the way a typed email looks, plus a text-only part.</p></div>
                    </div>
                </div>
                ${body}
                <p class="deliv-format-foot">
                    Switching keeps the list, subject and schedule. The first switch to plain text builds a plain version of the campaign's template (named “… · plain”); each campaign remembers both versions, so you can switch back and forth.
                </p>
            </section>
        `;
    },

    formatRowHtml(row) {
        const name = App.escapeHtml(row.name || `Campaign ${row.id}`);
        const busy = this.switchingFormat === row.id;
        let control;
        if (row.format === 'text' || row.format === 'visual') {
            control = `<span class="deliv-format-static">${row.format === 'text' ? 'Text only' : 'Visual design'}</span>`;
        } else {
            const disabled = !row.can_switch || busy || this.switchingFormat !== null;
            const btn = (fmt, label) => {
                const active = row.format === fmt;
                return `<button type="button" class="deliv-seg-btn ${active ? 'is-active' : ''}" aria-pressed="${active}"
                            ${disabled ? 'disabled' : ''} onclick="Deliverability.switchFormat(${Number(row.id)}, '${fmt}')">
                            ${busy && !active ? '<span class="spinner-sm" aria-hidden="true"></span>' : ''}${label}
                        </button>`;
            };
            control = `<div class="deliv-seg" role="group" aria-label="Email format for ${name}">${btn('html', 'HTML design')}${btn('plain', 'Plain text')}</div>`;
        }
        const meta = row.reason
            ? `<span class="deliv-format-reason">${App.escapeHtml(row.reason)}</span>`
            : (row.template_name ? `Template: ${App.escapeHtml(row.template_name)}` : 'No template');
        return `
            <li class="deliv-format-row ${busy ? 'is-busy' : ''}" data-campaign="${Number(row.id)}" ${busy ? 'aria-busy="true"' : ''}>
                <div class="deliv-format-main">
                    <div class="deliv-format-title">
                        <span class="deliv-format-name">${name}</span>
                        ${App.statusBadge(row.status)}
                    </div>
                    <p class="deliv-format-meta">${meta}</p>
                </div>
                ${control}
            </li>
        `;
    },

    /** Re-render only the format card, so staged audit edits are untouched. */
    refreshFormatCard() {
        const card = document.getElementById('delivFormat');
        if (card) card.outerHTML = this.formatHtml();
    },

    async switchFormat(campaignId, fmt) {
        const rows = (this.formats || {}).campaigns || [];
        const row = rows.find(r => r.id === campaignId);
        if (!row || row.format === fmt || this.switchingFormat !== null) return;

        this.switchingFormat = campaignId;
        this.refreshFormatCard();
        try {
            const res = await API.put(`/api/deliverability/email-format/${campaignId}`, { format: fmt }, API.LONG_TIMEOUT);
            Object.assign(row, {
                format: res.format,
                template_id: res.template_id,
                template_name: res.template_name,
            });
            App.toast(`“${row.name}” now sends as ${fmt === 'plain' ? 'plain text' : 'HTML design'}${res.template_name ? ` (${res.template_name})` : ''}.`, 'success');
        } catch (err) {
            // API.request already surfaced the server message.
            console.error('Email format switch failed:', err);
        } finally {
            this.switchingFormat = null;
        }
        if (App.currentPage === 'deliverability') this.refreshFormatCard();
    },

    /* ── Field notes ─────────────────────────────────────────────────────── */

    /** "Why it matters" notes shown at the foot of the group they explain. */
    notesHtml(groupId) {
        const notes = DELIV_NOTES[groupId];
        if (!notes || !notes.length) return '';
        return `
            <div class="deliv-why">
                <div class="deliv-why-title">${_dSvgIcon('book')}Why it matters</div>
                <div class="deliv-why-grid">
                    ${notes.map(n => `
                        <article class="deliv-note">
                            <h3>${n.title}</h3>
                            <p>${n.body}</p>
                        </article>`).join('')}
                </div>
            </div>
        `;
    },

    /* ── Actions ─────────────────────────────────────────────────────────── */

    async applyPreset(mode) {
        const modeName = mode === 'cold_email' ? 'Cold Email Deliverability' : 'Newsletter Marketing';
        const staged = Object.keys(this.pending).length;
        const ok = await App.confirm(
            `Apply ${modeName} Preset`,
            `This will automatically update your Listmonk engine settings for ${modeName}.${staged ? ' Your unsaved changes on this page will be discarded.' : ''} Are you sure you want to proceed?`
        );
        if (!ok) return;

        App.showProgress('Applying Preset', `Updating Listmonk settings for ${modeName}…`);
        try {
            await API.post('/api/deliverability/apply-preset', { mode }, API.LONG_TIMEOUT);
            App.closeModal();
            App.toast(`Successfully applied ${modeName} preset!`, 'success');
            await this.render({ keepSection: true });
        } catch (err) {
            App.closeModal();
            // API.request already surfaced the server message.
            console.error('Apply preset failed:', err);
        }
    },
};

/* ── Control map (module scope: no per-render allocation) ─────────────────── */

/**
 * check id → controls rendered on that row. `key` is the ListMonk setting the
 * backend whitelists (EDITABLE_SETTINGS); `@unsubscribe_link` is the template
 * toggle. `invert` shows the feature rather than the "disable_*" flag, so a
 * toggle that is on always means the thing is happening.
 */
const DELIV_CONTROLS = {
    disable_tracking: [{ type: 'toggle', key: 'privacy.disable_tracking', label: 'Open & click tracking', invert: true }],
    individual_tracking: [{ type: 'toggle', key: 'privacy.individual_tracking', label: 'Per-subscriber tracking' }],
    unsubscribe_header: [{ type: 'toggle', key: 'privacy.unsubscribe_header', label: 'List-Unsubscribe header' }],
    unsubscribe_link: [{ type: 'toggle', key: '@unsubscribe_link', label: 'Footer link in templates' }],
    batch_size: [{ type: 'number', key: 'app.batch_size', label: 'Messages per batch', min: 1, max: 10000 }],
    concurrency: [{ type: 'number', key: 'app.concurrency', label: 'Workers', min: 1, max: 100 }],
    send_cadence: [
        { type: 'number', key: 'app.message_rate', label: 'Max messages / second', min: 1, max: 10000 },
        { type: 'toggle', key: 'app.message_sliding_window', label: 'Sliding window limit' },
        { type: 'number', key: 'app.message_sliding_window_rate', label: 'Messages per window', min: 1, max: 1000000, dependsOn: 'app.message_sliding_window' },
        { type: 'duration', key: 'app.message_sliding_window_duration', label: 'Window length', dependsOn: 'app.message_sliding_window' },
    ],
    bounce_feedback: [{ type: 'toggle', key: 'bounce.enabled', label: 'Bounce processing' }],
    hard_bounce_action: [{
        type: 'select', key: 'bounce.actions.hard', label: 'On hard bounce',
        options: [['blocklist', 'Blocklist'], ['unsubscribe', 'Unsubscribe'], ['delete', 'Delete subscriber'], ['none', 'Do nothing']],
    }],
    public_archive: [{ type: 'toggle', key: 'app.enable_public_archive', label: 'Public archive' }],
    public_subscription_page: [{ type: 'toggle', key: 'app.enable_public_subscription_page', label: 'Public sign-up page' }],
    upload_extensions: [{ type: 'extensions', key: 'upload.extensions', label: 'Upload types' }],
};

/** Setting key → field of the checklist's `raw_settings` holding its value. */
const DELIV_RAW_FIELDS = {
    'privacy.disable_tracking': 'disable_tracking',
    'privacy.individual_tracking': 'individual_tracking',
    'privacy.unsubscribe_header': 'unsubscribe_header',
    'app.enable_public_archive': 'public_archive',
    'app.enable_public_subscription_page': 'public_subscription_page',
    'app.batch_size': 'batch_size',
    'app.concurrency': 'concurrency',
    'app.message_rate': 'message_rate',
    'app.message_sliding_window': 'sliding_window',
    'app.message_sliding_window_rate': 'sliding_window_rate',
    'app.message_sliding_window_duration': 'sliding_window_duration',
    'bounce.enabled': 'bounce_enabled',
    'bounce.actions.hard': 'hard_bounce_action',
    'upload.extensions': 'upload_extensions',
};

/** Background for a group's rules (static, trusted markup). */
const DELIV_NOTES = {
    privacy: [
        {
            title: 'Tracking pixels cost more than they return',
            body: 'Google and Microsoft weigh 1&times;1 pixels and redirect wrappers heavily. In cold outreach the open rate is the wrong prize: removing them makes the message indistinguishable from a personal reply, which is what primary-inbox placement rewards.',
        },
        {
            title: 'Header and footer are two different exits',
            body: 'The <code>List-Unsubscribe</code> header is what mail clients turn into their own one-click button — Gmail and Yahoo require it from bulk senders. The footer link is the one written into your template. A prospect who finds neither presses <em>Report spam</em>, and complaints are what sink a sending domain.',
        },
    ],
    sending: [
        {
            title: 'A steady drip beats a fast burst',
            body: 'Small batches through a single worker spread sends evenly, so no provider sees a spike. A spike is the most common trigger for temporary throttling, and a throttled domain keeps losing deliverability after the campaign ends.',
        },
    ],
};

function _dFindControl(key) {
    for (const list of Object.values(DELIV_CONTROLS)) {
        const hit = list.find(c => c.key === key);
        if (hit) return hit;
    }
    return null;
}

/* ── Presentation helpers ─────────────────────────────────────────────────── */

const DELIVERABILITY_GLYPHS = {
    pass: '<svg aria-hidden="true" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><polyline points="20 6 9 17 4 12"/></svg>',
    warn: '<svg aria-hidden="true" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M10.29 3.86 1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z"/><line x1="12" y1="9" x2="12" y2="13"/><line x1="12" y1="17" x2="12.01" y2="17"/></svg>',
    fail: '<svg aria-hidden="true" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"/><line x1="15" y1="9" x2="9" y2="15"/><line x1="9" y1="9" x2="15" y2="15"/></svg>',
    info: '<svg aria-hidden="true" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"/><line x1="12" y1="16" x2="12" y2="12"/><line x1="12" y1="8" x2="12.01" y2="8"/></svg>',
};

const DELIVERABILITY_ICONS = {
    list: '<svg aria-hidden="true" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="14" height="14" stroke-linecap="round" stroke-linejoin="round"><line x1="8" y1="6" x2="21" y2="6"/><line x1="8" y1="12" x2="21" y2="12"/><line x1="8" y1="18" x2="21" y2="18"/><line x1="3" y1="6" x2="3.01" y2="6"/><line x1="3" y1="12" x2="3.01" y2="12"/><line x1="3" y1="18" x2="3.01" y2="18"/></svg>',
    mail: '<svg aria-hidden="true" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="14" height="14" stroke-linecap="round" stroke-linejoin="round"><rect x="2" y="4" width="20" height="16" rx="2"/><path d="m22 7-10 6L2 7"/></svg>',
    lock: '<svg aria-hidden="true" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="14" height="14" stroke-linecap="round" stroke-linejoin="round"><rect x="4" y="11" width="16" height="10" rx="2"/><path d="M8 11V7a4 4 0 0 1 8 0v4"/></svg>',
    layout: '<svg aria-hidden="true" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="14" height="14" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="3" width="18" height="18" rx="2"/><line x1="3" y1="9" x2="21" y2="9"/><line x1="9" y1="21" x2="9" y2="9"/></svg>',
    type: '<svg aria-hidden="true" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="14" height="14" stroke-linecap="round" stroke-linejoin="round"><polyline points="4 7 4 4 20 4 20 7"/><line x1="9" y1="20" x2="15" y2="20"/><line x1="12" y1="4" x2="12" y2="20"/></svg>',
    book: '<svg aria-hidden="true" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="14" height="14" stroke-linecap="round" stroke-linejoin="round"><path d="M2 4h6a4 4 0 0 1 4 4v13a3 3 0 0 0-3-3H2z"/><path d="M22 4h-6a4 4 0 0 0-4 4v13a3 3 0 0 1 3-3h7z"/></svg>',
    eyeOff: '<svg aria-hidden="true" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="14" height="14" stroke-linecap="round" stroke-linejoin="round"><path d="M9.88 9.88a3 3 0 1 0 4.24 4.24"/><path d="M10.73 5.08A10.4 10.4 0 0 1 12 5c7 0 10 7 10 7a13.2 13.2 0 0 1-1.67 2.68"/><path d="M6.61 6.61A13.5 13.5 0 0 0 2 12s3 7 10 7a9.7 9.7 0 0 0 5.39-1.61"/><line x1="2" y1="2" x2="22" y2="22"/></svg>',
    gauge: '<svg aria-hidden="true" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="14" height="14" stroke-linecap="round" stroke-linejoin="round"><path d="m12 14 4-4"/><path d="M3.34 19a10 10 0 1 1 17.32 0"/></svg>',
    sparkle: '<svg aria-hidden="true" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="14" height="14" stroke-linecap="round" stroke-linejoin="round"><path d="M12 3l1.9 5.8L20 10.7l-6.1 1.9L12 18.5l-1.9-5.9L4 10.7l6.1-1.9z"/><path d="M19 17v4"/><path d="M17 19h4"/></svg>',
    shield: '<svg aria-hidden="true" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="14" height="14" stroke-linecap="round" stroke-linejoin="round"><path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z"/><path d="m9 12 2 2 4-4"/></svg>',
    globe: '<svg aria-hidden="true" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="14" height="14" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"/><line x1="2" y1="12" x2="22" y2="12"/><path d="M12 2a15.3 15.3 0 0 1 4 10 15.3 15.3 0 0 1-4 10 15.3 15.3 0 0 1-4-10 15.3 15.3 0 0 1 4-10z"/></svg>',
    refresh: '<svg aria-hidden="true" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="14" height="14" stroke-linecap="round" stroke-linejoin="round"><polyline points="23 4 23 10 17 10"/><path d="M20.49 15a9 9 0 1 1-2.12-9.36L23 10"/></svg>',
    check: '<svg aria-hidden="true" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" width="14" height="14" stroke-linecap="round" stroke-linejoin="round"><polyline points="20 6 9 17 4 12"/></svg>',
    send: '<svg aria-hidden="true" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="14" height="14" stroke-linecap="round" stroke-linejoin="round"><polygon points="22 2 15 22 11 13 2 9 22 2"/><line x1="22" y1="2" x2="11" y2="13"/></svg>',
    alert: '<svg aria-hidden="true" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="16" height="16" stroke-linecap="round" stroke-linejoin="round"><path d="M10.29 3.86 1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z"/><line x1="12" y1="9" x2="12" y2="13"/><line x1="12" y1="17" x2="12.01" y2="17"/></svg>',
    info: '<svg aria-hidden="true" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="14" height="14" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"/><line x1="12" y1="16" x2="12" y2="12"/><line x1="12" y1="8" x2="12.01" y2="8"/></svg>',
};

function _dSvgIcon(name) {
    return DELIVERABILITY_ICONS[name] || '';
}

/** Menu / panel icon for an audit group id. */
function _dGroupIcon(groupId) {
    const map = { privacy: 'eyeOff', sending: 'gauge', hygiene: 'sparkle', reputation: 'shield', surface: 'globe' };
    return _dSvgIcon(map[groupId] || 'list');
}

/** False when the operator asked for reduced motion. */
function _dMotion() {
    return !(window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches);
}

function _dStatusMeta(status) {
    switch (status) {
        case 'pass':
            return { label: 'Passing', glyph: DELIVERABILITY_GLYPHS.pass };
        case 'warn':
            return { label: 'Needs attention', glyph: DELIVERABILITY_GLYPHS.warn };
        case 'fail':
            return { label: 'Failing', glyph: DELIVERABILITY_GLYPHS.fail };
        default:
            return { label: 'Informational', glyph: DELIVERABILITY_GLYPHS.info };
    }
}
