/**
 * Deliverability & Safety Pre-Flight Module for ListMonk Pro.
 * Provides pre-flight audits, scorecards, and 1-click preset switching.
 *
 * The audit renders as four check groups (the taxonomy is served by the API's
 * `groups` field) rather than one flat table. A passing check is deliberately
 * quiet — no green pill, no repeated target — so the page only raises its voice
 * for the checks that actually need a decision.
 */
const Deliverability = {
    checklistData: null,
    isLoading: false,
    // Local clock reading of the last successful fetch. Rendered next to the
    // refresh control so an operator knows whether the audit is stale.
    checkedAt: null,

    async render() {
        // applyPreset() re-renders after an await; if the user navigated away
        // in the meantime, painting over the new page would be wrong.
        if (App.currentPage !== 'deliverability') return;
        this._seq = App.seq();
        this.isLoading = true;
        App.setContent(this.loadingHtml());

        try {
            this.checklistData = await API.get('/api/deliverability/checklist');
            this.checkedAt = new Date();
            if (App.stale(this._seq)) return;
            this.renderPage();
        } catch (err) {
            console.error('Deliverability error:', err);
            if (App.stale(this._seq)) return;
            App.setContent(`<div class="empty-state"><h3>Failed to load deliverability checklist</h3><p>${App.escapeHtml(err?.message || '')}</p></div>`);
        } finally {
            this.isLoading = false;
        }
    },

    loadingHtml() {
        const block = `<div class="deliv-skel-row"><span class="deliv-skel deliv-skel-mark"></span><span class="deliv-skel deliv-skel-line"></span><span class="deliv-skel deliv-skel-chip"></span></div>`;
        return `
            <div class="card deliv-summary" aria-busy="true">
                <div class="deliv-summary-score">
                    <span class="deliv-skel deliv-skel-eyebrow"></span>
                    <span class="deliv-skel deliv-skel-score"></span>
                    <span class="deliv-skel deliv-skel-meter"></span>
                </div>
                <div class="deliv-summary-body">
                    <span class="deliv-skel deliv-skel-heading"></span>
                    <span class="deliv-skel deliv-skel-copy"></span>
                    <span class="deliv-skel deliv-skel-copy short"></span>
                </div>
            </div>
            <div class="card deliv-audit" aria-busy="true">
                ${block.repeat(5)}
            </div>
        `;
    },

    renderPage() {
        if (this._seq !== undefined && App.stale(this._seq)) return;
        const data = this.checklistData || {};
        const checks = Array.isArray(data.checks) ? data.checks : [];
        const groups = Array.isArray(data.groups) ? data.groups : [];
        const score = Number.isFinite(data.score) ? data.score : 0;

        const scored = checks.filter(c => c.status !== 'info');
        const passing = scored.filter(c => c.status === 'pass').length;
        const failing = scored.filter(c => c.status === 'fail');
        const warning = scored.filter(c => c.status === 'warn');
        const unresolved = failing.concat(warning);
        const isOptimal = score === 100 && scored.length > 0;

        // Groups arrive with label/blurb; a check whose group id is unknown
        // (older API, new rule) still renders instead of vanishing.
        const order = groups.length ? groups : [{ id: null, label: 'Checks', blurb: '' }];
        const seen = new Set();
        const buckets = order.map(g => {
            const items = g.id
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

        let html = '';

        if (data.error) {
            html += `
                <div class="notice notice-warning deliv-error-notice">
                    ${svgIcon('alert')}
                    <div>
                        <strong>Listmonk settings could not be read.</strong>
                        The audit below is empty — fix the connection and refresh.
                        <div class="deliv-error-detail">${App.escapeHtml(data.error)}</div>
                    </div>
                </div>
            `;
        }

        html += this.summaryHtml({ score, scored, passing, unresolved, isOptimal, checks });
        html += this.auditHtml({ buckets, passing, scored, unresolved });
        html += this.notesHtml();

        App.setContent(html);
    },

    /* ── Summary ─────────────────────────────────────────────────────────── */

    summaryHtml({ score, scored, passing, unresolved, isOptimal, checks }) {
        const infoCount = checks.filter(c => c.status === 'info').length;

        const ticks = scored.map(c => {
            const meta = statusMeta(c.status);
            return `<span class="deliv-tick is-${c.status}" title="${App.escapeHtml(c.name)} — ${meta.label}"></span>`;
        }).join('');

        let verdict;
        let note;
        if (!scored.length) {
            verdict = 'No settings to audit';
            note = 'Listmonk returned no readable settings, so no checks could be scored.';
        } else if (isOptimal) {
            verdict = 'Everything scored is in place';
            note = 'Every setting this audit scores matches the cold-outreach profile: no tracking pixels, no bulk-unsubscribe header, and cadence paced by hand.';
        } else {
            verdict = `${unresolved.length} of ${scored.length} scored checks need a decision`;
            note = 'The preset fixes the engine parameters it owns. Settings Listmonk manages elsewhere — bounce mailboxes, upload types — are listed in the audit below and stay manual.';
        }

        const unresolvedNames = unresolved.slice(0, 3).map(c => App.escapeHtml(c.name));
        const more = unresolved.length > 3 ? ` +${unresolved.length - 3} more` : '';

        return `
            <section class="card deliv-summary ${isOptimal ? 'is-optimal' : 'needs-work'}">
                <div class="deliv-summary-score">
                    <div class="deliv-eyebrow">Deliverability score</div>
                    <div class="deliv-score-line">
                        <span class="deliv-score-value">${score}</span>
                        <span class="deliv-score-max">/ 100</span>
                    </div>
                    <div class="deliv-meter" aria-hidden="true">${ticks}</div>
                    <div class="deliv-score-caption">
                        <strong>${passing}</strong> of ${scored.length} scored checks passing
                        ${infoCount ? `<span class="deliv-score-note">${infoCount} informational ${infoCount === 1 ? 'check' : 'checks'} reported, not scored</span>` : ''}
                    </div>
                </div>

                <div class="deliv-summary-body">
                    <div class="deliv-profile-pill ${isOptimal ? 'matched' : 'unmatched'}">
                        <span class="deliv-profile-dot" aria-hidden="true"></span>
                        ${isOptimal ? 'Cold-email profile matched' : 'Cold-email profile not matched'}
                    </div>
                    <h2 class="deliv-verdict">${verdict}</h2>
                    <p class="deliv-verdict-note">${note}</p>
                    ${unresolved.length ? `<p class="deliv-verdict-list">${unresolvedNames.join(' · ')}${more}</p>` : ''}

                    <div class="deliv-preset-row">
                        ${isOptimal
                            ? `<button type="button" class="btn btn-secondary" disabled>
                                   ${svgIcon('check')}
                                   Cold-email preset active
                               </button>`
                            : `<button type="button" class="btn btn-success" onclick="Deliverability.applyPreset('cold_email')">
                                   ${svgIcon('send')}
                                   Apply cold-email preset
                               </button>`}
                        <button type="button" class="btn btn-secondary" onclick="Deliverability.applyPreset('newsletter')">
                            Switch to newsletter
                        </button>
                    </div>
                </div>
            </section>
        `;
    },

    /* ── Audit ───────────────────────────────────────────────────────────── */

    auditHtml({ buckets, passing, scored, unresolved }) {
        let html = `
            <section class="card deliv-audit">
                <header class="card-header deliv-audit-head">
                    <div>
                        <h3 class="card-title">Pre-flight audit</h3>
                        <p class="deliv-sub">
                            Read live from Listmonk${this.checkedAt ? ` · checked ${this.checkedAt.toLocaleTimeString('en-US', { hour: 'numeric', minute: '2-digit' })}` : ''}
                        </p>
                    </div>
                    <div class="page-actions">
                        <span class="deliv-tally">
                            <span><b class="is-pass">${passing}</b> passing</span>
                            <span><b class="${unresolved.length ? 'is-attention' : ''}">${unresolved.length}</b> attention</span>
                        </span>
                        <button type="button" class="btn btn-sm btn-secondary" onclick="Deliverability.render()" ${this.isLoading ? 'disabled' : ''}>
                            ${svgIcon('refresh')}
                            Refresh
                        </button>
                    </div>
                </header>
        `;

        if (!buckets.some(b => b.items.length)) {
            html += `<div class="empty-state"><h3>No checks reported</h3><p>Listmonk returned an empty audit.</p></div>`;
        } else {
            buckets.filter(b => b.items.length).forEach(b => {
                html += this.groupHtml(b.group, b.items);
            });
        }

        html += `
                <p class="deliv-legend">
                    Informational rows are context, not verdicts — they never affect the score.
                </p>
            </section>
        `;
        return html;
    },

    groupHtml(group, items) {
        // Loud first: a failing check should never sit below four passing ones.
        const rank = { fail: 0, warn: 1, info: 2, pass: 3 };
        const sorted = items.slice().sort((a, b) => (rank[a.status] ?? 9) - (rank[b.status] ?? 9));
        const scoredItems = items.filter(c => c.status !== 'info');
        const needsWork = scoredItems.some(c => c.status === 'fail' || c.status === 'warn');
        const passed = scoredItems.filter(c => c.status === 'pass').length;

        // The count is rendered "passed / scored". A group with no scored rules
        // (all informational) shows the raw item count instead of a fake 0/0.
        const total = scoredItems.length || items.length;

        const rows = sorted.map(c => this.checkRowHtml(c)).join('');

        return `
            <div class="deliv-group">
                <div class="deliv-group-rail">
                    <h4 class="deliv-group-label">${App.escapeHtml(group.label)}</h4>
                    ${group.blurb ? `<p class="deliv-group-blurb">${App.escapeHtml(group.blurb)}</p>` : ''}
                    <span class="deliv-group-count ${needsWork ? 'has-attention' : ''}">
                        ${scoredItems.length ? passed : ''} ${scoredItems.length ? '/' : ''} ${total}
                    </span>
                </div>
                <ul class="deliv-checks">${rows}</ul>
            </div>
        `;
    },

    checkRowHtml(check) {
        const meta = statusMeta(check.status);
        const severity = check.status === 'fail' || check.status === 'warn' ? this.severityLabel(check.impact) : '';
        const target = check.status === 'pass' || check.status === 'info' ? '' : this.targetLabel(check);

        return `
            <li class="deliv-check is-${check.status}">
                <span class="deliv-check-mark" aria-hidden="true">${meta.glyph}</span>
                <div class="deliv-check-main">
                    <span class="deliv-check-name">${App.escapeHtml(check.name)}</span>
                    <p class="deliv-check-desc">${App.escapeHtml(check.description)}</p>
                </div>
                <div class="deliv-check-value">
                    <div class="deliv-chips">
                        ${this.chipsHtml(this.valueChips(check), check.status === 'fail' ? 'bad' : '')}
                        ${target ? `<span class="deliv-arrow" aria-hidden="true">&rarr;</span>${this.chipsHtml([target], 'target')}` : ''}
                    </div>
                    <span class="deliv-check-sev" ${severity ? `title="${App.escapeHtml(check.impact)}"` : ''}>${severity}</span>
                    <span class="sr-only">${meta.label}</span>
                </div>
            </li>
        `;
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
     * Turn a check's raw value into discrete, readable facts. Booleans stay
     * "on"/"off" so the current reading and its target are never rendered in
     * different vocabularies; counts get their unit attached, because "100"
     * alone in a Deliverability audit could mean per-batch or per-second.
     */
    valueChips(check) {
        const raw = (this.checklistData || {}).raw_settings || {};
        const v = check.current_value;

        switch (check.id) {
            case 'batch_size':
                return [`${App.formatNumber(v)} per batch`];
            case 'concurrency':
                return [`${App.formatNumber(v)} ${Number(v) === 1 ? 'worker' : 'workers'}`];
            case 'public_archive':
                return [v === true ? 'Public page live' : 'Off'];
            case 'bounce_feedback': {
                const chips = [raw.bounce_enabled ? 'Bounce handling on' : 'Bounce handling off'];
                const active = Number(raw.bounce_active_mailboxes) || 0;
                const total = Number(raw.bounce_mailboxes_total) || 0;
                chips.push(`${active} of ${total} mailbox${total === 1 ? '' : 'es'} enabled`);
                chips.push(raw.bounce_provider_enabled ? 'Provider webhook on' : 'No provider webhook');
                return chips;
            }
            case 'hard_bounce_action': {
                const action = String(v || '').toLowerCase();
                if (action === 'blocklist') return ['Auto-blocklist'];
                if (!v || action === 'unset') return ['Not set'];
                return [String(v)];
            }
            case 'send_cadence': {
                const perDay = Number(raw.send_cadence_per_day);
                if (Number.isFinite(perDay) && perDay > 0) {
                    return [`≈${App.formatNumber(perDay)} msgs/day`];
                }
                return [{ label: String(v), wide: true, title: String(v) }];
            }
            case 'upload_extensions': {
                const ext = Array.isArray(raw.upload_extensions) ? raw.upload_extensions : [];
                if (!ext.length) return ['Not set'];
                const chips = [`${ext.length} type${ext.length === 1 ? '' : 's'} allowed`];
                if (ext.includes('*')) chips.push({ label: '* wildcard', tone: 'bad' });
                chips[0] = { label: chips[0], title: ext.join(', ') };
                return chips;
            }
            default:
                if (v === true) return ['On'];
                if (v === false) return ['Off'];
                if (v === null || v === undefined || v === '') return ['Not set'];
                return [String(v)];
        }
    },

    /** The value a failing check should hold, in the same vocabulary as above. */
    targetLabel(check) {
        const rec = check.recommended;
        if (rec === true) return 'On';
        if (rec === false) return 'Off';
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

    /* ── Field notes ─────────────────────────────────────────────────────── */

    notesHtml() {
        return `
            <section class="card deliv-notes">
                <h3 class="card-title">Why these rules exist</h3>
                <div class="deliv-notes-grid">
                    <article class="deliv-note">
                        <span class="deliv-note-index">01</span>
                        <h4>Tracking pixels cost more than they return</h4>
                        <p>Google and Microsoft weigh 1&times;1 pixels and redirect wrappers heavily. In cold outreach the open rate is the wrong prize: removing them makes the message indistinguishable from a personal reply, which is what primary-inbox placement rewards.</p>
                    </article>
                    <article class="deliv-note">
                        <span class="deliv-note-index">02</span>
                        <h4>The unsubscribe header is a bulk-mail label</h4>
                        <p>Gmail and Outlook read <code>List-Unsubscribe</code> as a declaration of bulk mail and sort accordingly. A plain-text opt-out line does the same job for a prospect without handing the mailbox provider that label.</p>
                    </article>
                    <article class="deliv-note">
                        <span class="deliv-note-index">03</span>
                        <h4>A steady drip beats a fast burst</h4>
                        <p>Batches of 100 at a single worker spread sends evenly, so no provider sees a spike. A spike is the most common trigger for temporary throttling, and a throttled domain keeps losing deliverability after the campaign ends.</p>
                    </article>
                </div>
            </section>
        `;
    },

    /* ── Actions ─────────────────────────────────────────────────────────── */

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
            // API.request already surfaced the server message.
            console.error('Apply preset failed:', err);
        }
    },
};

/* ── Presentation helpers (module scope: no per-render allocation) ─────────── */

const DELIVERABILITY_GLYPHS = {
    pass: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><polyline points="20 6 9 17 4 12"/></svg>',
    warn: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M10.29 3.86 1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z"/><line x1="12" y1="9" x2="12" y2="13"/><line x1="12" y1="17" x2="12.01" y2="17"/></svg>',
    fail: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"/><line x1="15" y1="9" x2="9" y2="15"/><line x1="9" y1="9" x2="15" y2="15"/></svg>',
    info: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"/><line x1="12" y1="16" x2="12" y2="12"/><line x1="12" y1="8" x2="12.01" y2="8"/></svg>',
};

const DELIVERABILITY_ICONS = {
    refresh: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="14" height="14" stroke-linecap="round" stroke-linejoin="round"><polyline points="23 4 23 10 17 10"/><path d="M20.49 15a9 9 0 1 1-2.12-9.36L23 10"/></svg>',
    check: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" width="14" height="14" stroke-linecap="round" stroke-linejoin="round"><polyline points="20 6 9 17 4 12"/></svg>',
    send: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="14" height="14" stroke-linecap="round" stroke-linejoin="round"><polygon points="22 2 15 22 11 13 2 9 22 2"/><line x1="22" y1="2" x2="11" y2="13"/></svg>',
    alert: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="16" height="16" stroke-linecap="round" stroke-linejoin="round"><path d="M10.29 3.86 1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z"/><line x1="12" y1="9" x2="12" y2="13"/><line x1="12" y1="17" x2="12.01" y2="17"/></svg>',
};

function svgIcon(name) {
    return DELIVERABILITY_ICONS[name] || '';
}

function statusMeta(status) {
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