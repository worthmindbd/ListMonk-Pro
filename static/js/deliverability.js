/**
 * Deliverability & Safety Module for ListMonk Pro.
 * Pre-flight audit, scorecard, 1-click presets — and a control on every row.
 *
 * The audit renders as check groups (the taxonomy is served by the API's
 * `groups` field). Each row carries the control that changes its setting:
 * edits are staged locally and written together from the sticky save bar,
 * because every ListMonk settings write reloads the ListMonk app. A passing
 * check stays quiet so colour is only spent on the rows that need a decision.
 *
 * Two unsubscribe controls exist on purpose: the List-Unsubscribe *header*
 * (a ListMonk setting, invisible in the message body) and the footer *link*
 * (markup in the campaign templates, toggled via /unsubscribe-link).
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
    // Keys whose input currently holds an unparseable value (blocks Save).
    invalid: new Set(),

    async render() {
        // applyPreset()/save() re-render after an await; if the user navigated
        // away in the meantime, painting over the new page would be wrong.
        if (App.currentPage !== 'deliverability') return;
        this._seq = App.seq();
        this.isLoading = true;
        this.pending = {};
        this.invalid = new Set();
        App.setContent(this.loadingHtml());

        try {
            // silent: an upstream outage (now a 502) renders in this page's
            // own error state below — a toast on top would say it twice.
            this.checklistData = await API.get('/api/deliverability/checklist', true);
            this.checkedAt = new Date();
            if (App.stale(this._seq)) return;
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
                ${block.repeat(6)}
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
                    ${_dSvgIcon('alert')}
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
        html += `<div class="deliv-savebar" id="delivSavebar" role="region" aria-label="Unsaved deliverability changes" hidden></div>`;

        App.setContent(html);
        this.refreshDirty();
    },

    /* ── Summary ─────────────────────────────────────────────────────────── */

    summaryHtml({ score, scored, passing, unresolved, isOptimal, checks }) {
        const infoCount = checks.filter(c => c.status === 'info').length;

        const ticks = scored.map(c => {
            const meta = _dStatusMeta(c.status);
            return `<span class="deliv-tick is-${c.status}" title="${App.escapeHtml(c.name)} — ${meta.label}"></span>`;
        }).join('');

        let verdict;
        let note;
        if (!scored.length) {
            verdict = 'No settings to audit';
            note = 'Listmonk returned no readable settings, so no checks could be scored.';
        } else if (isOptimal) {
            verdict = 'Everything scored is in place';
            note = 'Every setting this audit scores matches the cold-outreach profile: no tracking pixels, a working opt-out in header and footer, and cadence paced by hand.';
        } else {
            verdict = `${unresolved.length} of ${scored.length} scored checks need a decision`;
            note = 'Change any setting with the control on its row, then save once — or apply a preset to set the sending-engine parameters in one step.';
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
                                   ${_dSvgIcon('check')}
                                   Cold-email preset active
                               </button>`
                            : `<button type="button" class="btn btn-success" onclick="Deliverability.applyPreset('cold_email')">
                                   ${_dSvgIcon('send')}
                                   Apply cold-email preset
                               </button>`}
                        <button type="button" class="btn btn-secondary" onclick="Deliverability.applyPreset('newsletter')">
                            Switch to newsletter
                        </button>
                    </div>
                    <p class="deliv-preset-hint">Presets change tracking, batching, concurrency and public pages. They never touch the footer link, bounce mailboxes or upload types.</p>
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
                        <h2 class="card-title">Settings &amp; pre-flight audit</h2>
                        <p class="deliv-sub">
                            Read live from Listmonk${this.checkedAt ? ` · checked ${this.checkedAt.toLocaleTimeString(undefined, { hour: 'numeric', minute: '2-digit' })}` : ''}
                        </p>
                    </div>
                    <div class="page-actions">
                        <span class="deliv-tally">
                            <span><b class="is-pass">${passing}</b> passing</span>
                            <span><b class="${unresolved.length ? 'is-attention' : ''}">${unresolved.length}</b> attention</span>
                        </span>
                        <button type="button" class="btn btn-sm btn-secondary" onclick="Deliverability.refresh()" ${this.isLoading ? 'disabled' : ''}>
                            ${_dSvgIcon('refresh')}
                            Refresh
                        </button>
                    </div>
                </header>
        `;

        if (!buckets.some(b => b.items.length)) {
            html += `<div class="empty-state"><h2>No checks reported</h2><p>Listmonk returned an empty audit.</p></div>`;
        } else {
            buckets.filter(b => b.items.length).forEach(b => {
                html += this.groupHtml(b.group, b.items);
            });
        }

        html += `
                <p class="deliv-legend">
                    Changes are staged until you press <b>Save changes</b>. Informational rows are context, not verdicts — they never affect the score.
                </p>
            </section>
        `;
        return html;
    },

    groupHtml(group, items) {
        // Keep the server's order inside a group: related controls (the
        // header and the footer link) must stay next to each other, and rows
        // must not jump around while their toggles are being flipped.
        const scoredItems = items.filter(c => c.status !== 'info');
        const needsWork = scoredItems.some(c => c.status === 'fail' || c.status === 'warn');
        const passed = scoredItems.filter(c => c.status === 'pass').length;

        // The count is rendered "passed / scored". A group with no scored rules
        // (all informational) shows the raw item count instead of a fake 0/0.
        const total = scoredItems.length || items.length;

        const rows = items.map(c => this.checkRowHtml(c)).join('');

        return `
            <div class="deliv-group">
                <div class="deliv-group-rail">
                    <h3 class="deliv-group-label">${App.escapeHtml(group.label)}</h3>
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
                        : `<span class="deliv-readonly">Read-only</span>`}
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
            default:
                return '';
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
                const active = Number(raw.bounce_active_mailboxes) || 0;
                const total = Number(raw.bounce_mailboxes_total) || 0;
                return [
                    `${active} of ${total} mailbox${total === 1 ? '' : 'es'} enabled`,
                    raw.bounce_provider_enabled ? 'Provider webhook on' : 'No provider webhook',
                ];
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
            case 'upload_extensions': {
                const ext = Array.isArray(raw.upload_extensions) ? raw.upload_extensions : [];
                return ext.includes('*') ? [{ label: '* wildcard allowed', tone: 'bad' }] : [];
            }
            default:
                return [];
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
        await this.render();
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
            if (settingsKeys.length) {
                const res = await API.put('/api/deliverability/settings', pending, API.LONG_TIMEOUT);
                checklist = res.checklist || checklist;
                settingsKeys.forEach(k => delete this.pending[k]);
            }
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

    /* ── Field notes ─────────────────────────────────────────────────────── */

    notesHtml() {
        return `
            <section class="card deliv-notes">
                <h2 class="card-title">Why these rules exist</h2>
                <div class="deliv-notes-grid">
                    <article class="deliv-note">
                        <span class="deliv-note-index">01</span>
                        <h3>Tracking pixels cost more than they return</h3>
                        <p>Google and Microsoft weigh 1&times;1 pixels and redirect wrappers heavily. In cold outreach the open rate is the wrong prize: removing them makes the message indistinguishable from a personal reply, which is what primary-inbox placement rewards.</p>
                    </article>
                    <article class="deliv-note">
                        <span class="deliv-note-index">02</span>
                        <h3>Header and footer are two different exits</h3>
                        <p>The <code>List-Unsubscribe</code> header is what mail clients turn into their own one-click button — Gmail and Yahoo require it from bulk senders. The footer link is the one written into your template. A prospect who finds neither presses <em>Report spam</em>, and complaints are what sink a sending domain.</p>
                    </article>
                    <article class="deliv-note">
                        <span class="deliv-note-index">03</span>
                        <h3>A steady drip beats a fast burst</h3>
                        <p>Batches of 100 at a single worker spread sends evenly, so no provider sees a spike. A spike is the most common trigger for temporary throttling, and a throttled domain keeps losing deliverability after the campaign ends.</p>
                    </article>
                </div>
            </section>
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
            await this.render();
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
    refresh: '<svg aria-hidden="true" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="14" height="14" stroke-linecap="round" stroke-linejoin="round"><polyline points="23 4 23 10 17 10"/><path d="M20.49 15a9 9 0 1 1-2.12-9.36L23 10"/></svg>',
    check: '<svg aria-hidden="true" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" width="14" height="14" stroke-linecap="round" stroke-linejoin="round"><polyline points="20 6 9 17 4 12"/></svg>',
    send: '<svg aria-hidden="true" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="14" height="14" stroke-linecap="round" stroke-linejoin="round"><polygon points="22 2 15 22 11 13 2 9 22 2"/><line x1="22" y1="2" x2="11" y2="13"/></svg>',
    alert: '<svg aria-hidden="true" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="16" height="16" stroke-linecap="round" stroke-linejoin="round"><path d="M10.29 3.86 1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z"/><line x1="12" y1="9" x2="12" y2="13"/><line x1="12" y1="17" x2="12.01" y2="17"/></svg>',
    info: '<svg aria-hidden="true" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="14" height="14" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"/><line x1="12" y1="16" x2="12" y2="12"/><line x1="12" y1="8" x2="12.01" y2="8"/></svg>',
};

function _dSvgIcon(name) {
    return DELIVERABILITY_ICONS[name] || '';
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
