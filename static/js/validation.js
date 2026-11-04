/**
 * Email Validation page.
 *
 *  1. Quick check   - paste addresses, see a verdict and the reasons.
 *  2. Scan          - validate every enabled subscriber (or one list) in the
 *                     background; progress is polled while a job runs.
 *  3. Results       - the problem addresses, filterable, with bulk actions.
 *
 * Verdicts: invalid (can never receive mail), risky (disposable / role
 * account / likely typo), unknown (a DNS lookup failed - says nothing about
 * the address). The page never changes ListMonk on its own: blocklisting is an
 * explicit action on the rows the user selected.
 *
 * The page shell is painted once. The scan card and the results region are
 * repainted in place (see _paintScan / _loadResults), so the quick-check box
 * and the search field keep what was typed while a scan reports progress.
 */
const Validation = {
    page: 1,
    perPage: 25,
    verdict: '',
    search: '',
    total: 0,
    counts: { all: 0, invalid: 0, risky: 0, unknown: 0 },
    status: null,
    lists: null,
    _rows: [],
    _poll: null,
    _resSeq: 0,
    _pageSeq: 0,
    // Mirror of MAX_PAGE in app/routers/email_validation.py: a deep link past
    // the backend bound would otherwise answer 422.
    MAX_PAGE: 100000,
    VERDICTS: ['invalid', 'risky', 'unknown'],

    REASONS: {
        syntax: 'Bad format',
        no_domain: 'Domain does not exist',
        no_mx: 'No mail server',
        null_mx: 'Accepts no email',
        hard_bounce: 'Hard bounced',
        disposable: 'Disposable provider',
        role: 'Role account',
        typo: 'Possible typo',
        dns_unavailable: 'DNS lookup failed',
    },

    reasonLabel(code) {
        return this.REASONS[code] || code;
    },

    verdictBadge(verdict) {
        const cls = { valid: 'success', invalid: 'danger', risky: 'warning', unknown: 'default' }[verdict] || 'default';
        const label = { valid: 'Valid', invalid: 'Invalid', risky: 'Risky', unknown: 'Unknown' }[verdict] || verdict;
        return `<span class="badge badge-${cls}">${App.escapeHtml(label)}</span>`;
    },

    _reasonChips(reasons) {
        return `<span class="valid-reasons">${(reasons || []).map(r =>
            `<span class="badge badge-default">${App.escapeHtml(this.reasonLabel(r))}</span>`).join('')}</span>`;
    },

    // ── Page ─────────────────────────────────────────────

    async render() {
        const seq = App.seq();
        this._pageSeq = seq;
        // Deep-linkable state: #/validation?verdict=invalid&page=2&q=acme
        const rq = App.routeQuery();
        if (rq.get('page')) this.page = Math.max(1, parseInt(rq.get('page'), 10) || 1);
        if (rq.get('verdict') !== null) {
            this.verdict = this.VERDICTS.includes(rq.get('verdict')) ? rq.get('verdict') : '';
        }
        if (rq.get('q') !== null) this.search = rq.get('q') || '';
        this.page = Math.min(Math.max(1, this.page), this.MAX_PAGE);
        this.stopPoll();

        try {
            const [status, lists] = await Promise.all([
                API.get('/api/email-validation/status'),
                this._getLists(),
            ]);
            if (App.stale(seq)) return;
            this.status = status;
            this.lists = lists;
        } catch (err) {
            if (App.stale(seq)) return;
            App.setContent(`<div class="empty-state"><h2>Failed to load email validation</h2><p>${App.escapeHtml(err.message || '')}</p></div>`);
            return;
        }

        App.setContent(`
            <div class="card" style="margin-bottom:20px">
                <div class="card-header"><h2 class="card-title">Check an email</h2></div>
                <p style="font-size:0.85rem;color:var(--text-muted);margin:0 0 12px">
                    Paste one address or many (one per line, or separated by commas). Nothing is sent:
                    each address is checked for a valid format, a domain that exists with a mail server,
                    throwaway providers, shared inboxes like <code>info@</code> and likely typos.
                </p>
                <textarea id="valInput" rows="3" aria-label="Email addresses to check"
                    placeholder="jane@example.com&#10;sales@acme.io"></textarea>
                <div class="form-actions">
                    <button class="btn btn-primary" id="valCheckBtn" onclick="Validation.check()">Check</button>
                </div>
                <div id="valCheckResults" aria-live="polite"></div>
            </div>
            <div class="card" style="margin-bottom:20px" id="valScan"></div>
            <div id="valResults"></div>
        `);
        this._paintScan();
        await this._loadResults();
        if (App.stale(seq)) return;
        if (this.status?.job?.running) this._startPoll();
    },

    async _getLists() {
        if (this.lists) return this.lists;
        try {
            const res = await API.get('/api/lists?per_page=200&minimal=true', true);
            return res?.data?.results || [];
        } catch {
            return [];
        }
    },

    // ── Quick check ──────────────────────────────────────

    async check() {
        const input = document.getElementById('valInput');
        const out = document.getElementById('valCheckResults');
        if (!input || !out) return;
        const emails = [...new Set(input.value.split(/[\s,;]+/).map(s => s.trim()).filter(Boolean))];
        if (!emails.length) { App.toast('Enter at least one email address', 'error'); return; }
        if (emails.length > 100) {
            App.toast('Check at most 100 addresses at a time. Use Scan subscribers for a whole list.', 'error');
            return;
        }
        const btn = document.getElementById('valCheckBtn');
        btn.disabled = true;
        btn.textContent = 'Checking…';
        try {
            const res = await API.post('/api/email-validation/check', { emails }, 60000);
            if (App.currentPage !== 'validation') return;
            this._paintCheck(out, res.results || []);
        } catch {
            // API.request already surfaced the server message.
        } finally {
            btn.disabled = false;
            btn.textContent = 'Check';
        }
    },

    _paintCheck(el, results) {
        const tally = { valid: 0, invalid: 0, risky: 0, unknown: 0 };
        results.forEach(r => { if (r.verdict in tally) tally[r.verdict]++; });
        const summary = ['valid', 'invalid', 'risky', 'unknown']
            .filter(v => tally[v]).map(v => `${tally[v]} ${v}`).join(' · ');
        const rows = results.map(r => `
            <div class="valid-result">
                ${this.verdictBadge(r.verdict)}
                <span class="valid-email">${App.escapeHtml(r.email || '')}</span>
                ${r.reasons?.length ? this._reasonChips(r.reasons) : ''}
                ${r.detail && !r.suggestion ? `<span class="valid-detail">${App.escapeHtml(r.detail)}</span>` : ''}
                ${r.suggestion ? `<span class="valid-suggest">Did you mean <strong>${App.escapeHtml(r.suggestion)}</strong>?</span>` : ''}
            </div>`).join('');
        el.innerHTML = `
            <p style="font-size:0.85rem;color:var(--text-secondary);margin:16px 0 4px">${App.escapeHtml(summary)}</p>
            ${rows}`;
    },

    // ── Scan card ────────────────────────────────────────

    _paintScan() {
        const el = document.getElementById('valScan');
        if (el) el.innerHTML = this._scanHtml();
    },

    _scanHtml() {
        const st = this.status || {};
        const job = st.job || {};
        const last = st.last_scan || null;
        const running = !!job.running;
        const n = (v) => App.formatNumber(v || 0);
        // While a job runs the numbers are live; otherwise they are the last scan's.
        const src = running ? job : (last || {});

        const cards = `
            <div class="stats-grid valid-stats" style="margin-bottom:16px">
                <div class="stat-card accent"><div class="stat-label">Checked</div><div class="stat-value">${n(src.checked)}</div></div>
                <div class="stat-card success"><div class="stat-label">Valid</div><div class="stat-value" style="color:var(--emerald)">${n(src.valid)}</div></div>
                <div class="stat-card danger"><div class="stat-label">Invalid</div><div class="stat-value" style="color:var(--ruby)">${n(src.invalid)}</div></div>
                <div class="stat-card warning"><div class="stat-label">Risky</div><div class="stat-value" style="color:var(--lemon)">${n(src.risky)}</div></div>
                <div class="stat-card info"><div class="stat-label">Unknown</div><div class="stat-value">${n(src.unknown)}</div></div>
            </div>`;

        let body;
        if (running) {
            const pct = job.total > 0 ? Math.min(100, Math.round(job.checked / job.total * 100)) : 0;
            const what = job.kind === 'cleanup' ? 'Scheduled cleanup is running' : 'Scanning subscribers';
            body = `
                <div class="progress-bar" role="progressbar" aria-label="Scan progress" aria-valuemin="0" aria-valuemax="100" aria-valuenow="${pct}">
                    <div class="progress-fill" style="width:${pct}%"></div>
                </div>
                <p style="font-size:0.85rem;color:var(--text-muted);margin:10px 0 0">
                    ${App.escapeHtml(what)}: ${n(job.checked)} of ${job.total ? n(job.total) : '…'} checked
                    <span style="opacity:.8">(${App.escapeHtml(job.phase || '')})</span>
                </p>
                <div class="form-actions">
                    <button class="btn" onclick="Validation.cancelScan()" ${job.cancel_requested ? 'disabled' : ''}>${job.cancel_requested ? 'Stopping…' : 'Cancel'}</button>
                </div>`;
        } else {
            const listOptions = (this.lists || []).map(l =>
                `<option value="${l.id}">${App.escapeHtml(l.name)}</option>`).join('');
            body = `
                <div class="search-bar" style="margin-bottom:0">
                    <select id="valListSelect" aria-label="Which subscribers to scan" style="width:auto;min-width:200px">
                        <option value="0">All subscribers</option>
                        ${listOptions}
                    </select>
                    <button class="btn btn-primary" onclick="Validation.startScan()">Scan subscribers</button>
                    <span style="font-size:0.85rem;color:var(--text-muted)">${this._lastScanText(last)}</span>
                </div>`;
        }

        return `
            <div class="card-header"><h2 class="card-title">Scan subscribers</h2></div>
            ${cards}
            ${body}
            <p style="font-size:0.8rem;color:var(--text-muted);margin:14px 0 0">
                A scan only reports: it changes nothing in ListMonk. Dead addresses are listed below for you to blocklist,
                or let <strong>Settings → Email Cleanup</strong> do it on a schedule.
            </p>`;
    },

    _lastScanText(last) {
        if (!last) return 'No scan yet.';
        const when = App.escapeHtml(App.formatDateTime(last.at));
        if (last.status === 'failed') return `Last scan failed (${when}): ${App.escapeHtml(last.message || '')}`;
        if (last.status === 'cancelled') return `Last scan stopped early (${when}).`;
        const kind = last.kind === 'cleanup' ? 'cleanup' : 'scan';
        return `Last ${kind}: ${when}.`;
    },

    async startScan() {
        const sel = document.getElementById('valListSelect');
        const listId = sel ? parseInt(sel.value, 10) || 0 : 0;
        try {
            const res = await API.post('/api/email-validation/scan', listId ? { list_id: listId } : {});
            this.status = { ...(this.status || {}), job: res.job };
            this._paintScan();
            this._startPoll();
        } catch {
            // API.request already surfaced the server message (e.g. 409 busy).
        }
    },

    async cancelScan() {
        try {
            await API.post('/api/email-validation/scan/cancel');
            App.toast('Stopping after the current batch…', 'info');
        } catch {
            // Already surfaced.
        }
    },

    _startPoll() {
        if (this._poll) return;
        this._poll = setInterval(() => this._tick(), 2000);
    },

    stopPoll() {
        if (this._poll) {
            clearInterval(this._poll);
            this._poll = null;
        }
    },

    async _tick() {
        if (App.currentPage !== 'validation') { this.stopPoll(); return; }
        try {
            // silent: a toast every 2s during an outage would stack up.
            const st = await API.get('/api/email-validation/status', true);
            if (App.currentPage !== 'validation') return;
            this.status = st;
            this._paintScan();
            if (!st.job.running) {
                this.stopPoll();
                this._announce(st.last_scan);
                this._loadResults();
            }
        } catch {
            // Keep polling; the next tick may succeed.
        }
    },

    _announce(last) {
        if (!last) return;
        if (last.status === 'failed') App.toast(last.message || 'Scan failed', 'error');
        else if (last.status === 'cancelled') App.toast('Scan stopped', 'info');
        else App.toast(`Scan finished: ${App.formatNumber(last.checked)} checked, ${App.formatNumber(last.invalid)} invalid`, 'success');
    },

    // ── Results ──────────────────────────────────────────

    async _loadResults() {
        const el = document.getElementById('valResults');
        if (!el) return;
        const seq = ++this._resSeq;
        const pageSeq = this._pageSeq;
        const params = new URLSearchParams({ page: String(this.page), per_page: String(this.perPage) });
        if (this.verdict) params.set('verdict', this.verdict);
        if (this.search) params.set('search', this.search);

        let res;
        try {
            res = await API.get(`/api/email-validation/results?${params}`);
        } catch (err) {
            if (seq !== this._resSeq || App.stale(pageSeq)) return;
            el.innerHTML = `<div class="empty-state"><h2>Failed to load results</h2><p>${App.escapeHtml(err.message || '')}</p></div>`;
            return;
        }
        if (seq !== this._resSeq || App.stale(pageSeq)) return;

        // A bulk action can strand the view past the last page; clamp and re-fetch.
        const clamped = App.clampPage(this.page, res.total || 0, this.perPage);
        if (clamped !== this.page) { this.page = clamped; return this._loadResults(); }

        this.total = res.total || 0;
        this.counts = res.counts || this.counts;
        this._rows = res.results || [];
        App.selection.register('validation', {
            noun: 'address',
            plural: 'addresses',
            pageValues: this.pageIds(),
            actions: [
                { label: 'Blocklist Selected', onclick: 'Validation.blocklistSelected()', danger: true },
                { label: 'Dismiss Selected', onclick: 'Validation.dismissSelected()' },
                { label: 'Export Selected', onclick: 'Validation.exportSelected()' },
            ],
        });
        el.innerHTML = this._resultsHtml();
        App.labelControls(el);
        App.syncUrl('validation', { page: this.page, verdict: this.verdict, q: this.search });
    },

    _resultsHtml() {
        const c = this.counts || {};
        const tab = (v, label, count) => `
            <button class="settings-tab ${this.verdict === v ? 'active' : ''}" onclick="Validation.setVerdict('${v}')">
                ${label} <span class="valid-tab-count">${App.formatNumber(count || 0)}</span>
            </button>`;

        let html = `
            <div class="settings-tabs">
                ${tab('', 'All issues', c.all)}
                ${tab('invalid', 'Invalid', c.invalid)}
                ${tab('risky', 'Risky', c.risky)}
                ${tab('unknown', 'Unknown', c.unknown)}
            </div>
            <div class="search-bar">
                <div class="search-input">
                    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="11" cy="11" r="8"/><line x1="21" y1="21" x2="16.65" y2="16.65"/></svg>
                    <input type="search" id="valSearch" placeholder="Search by email or name…"
                        aria-label="Search results by email or name"
                        value="${App.escapeHtml(this.search)}" onkeydown="if(event.key==='Enter')Validation.doSearch()">
                </div>
                <button class="btn" onclick="Validation.doSearch()">Search</button>
                <span style="color:var(--text-secondary);font-size:0.9rem">${App.formatNumber(this.total)} address${this.total === 1 ? '' : 'es'}</span>
                <div class="page-actions">
                    <button class="btn" onclick="Validation.exportAll()">Export CSV</button>
                </div>
            </div>
            ${App.selection.bar('validation')}`;

        if (!this._rows.length) {
            const filtered = this.verdict || this.search;
            html += `<div class="card"><div class="empty-state">
                <h2>${filtered ? 'No addresses match' : 'No problem addresses yet'}</h2>
                <p>${filtered ? 'Try another filter or search.' : 'Scan your subscribers above, or check an address, to find dead ones.'}</p>
            </div></div>`;
            return html;
        }

        html += `<div class="table-wrapper" tabindex="0" role="region" aria-label="Scrollable data table"><table>
            <thead><tr>
                <th class="col-select">${App.selection.headbox('validation', 'Select all addresses on this page')}</th>
                <th>Email</th><th>Verdict</th><th>Why</th><th>Checked</th><th class="cell-actions">Actions</th>
            </tr></thead><tbody>`;
        this._rows.forEach(r => {
            const id = r.subscriber_id;
            html += `<tr class="${App.selection.has('validation', id) ? 'row-selected' : ''}">
                <td class="col-select">${App.selection.rowbox('validation', id, `Select ${r.email}`)}</td>
                <td><strong class="cell-email" title="${App.escapeHtml(r.email)}">${App.escapeHtml(r.email)}</strong>
                    ${r.name ? `<div style="font-size:0.8rem;color:var(--text-muted)">${App.escapeHtml(r.name)}</div>` : ''}</td>
                <td>${this.verdictBadge(r.verdict)}</td>
                <td>${this._reasonChips(r.reasons)}
                    ${r.suggestion ? `<div class="valid-suggest">Did you mean <strong>${App.escapeHtml(r.suggestion)}</strong>?</div>` : ''}
                    ${r.detail && !r.suggestion ? `<div class="valid-detail">${App.escapeHtml(r.detail)}</div>` : ''}</td>
                <td>${App.formatDate(r.checked_at)}</td>
                <td class="cell-actions"><div class="action-btns">
                    <button class="btn btn-sm btn-danger" onclick="Validation.blocklistOne(${id})">Blocklist</button>
                    <button class="btn btn-sm" onclick="Validation.dismissOne(${id})">Dismiss</button>
                </div></td>
            </tr>`;
        });
        html += '</tbody></table></div>';
        html += App.renderPagination(this.page, this.total, this.perPage, 'Validation.goToPage');
        return html;
    },

    // ── Filters and paging ───────────────────────────────

    setVerdict(v) {
        this.verdict = this.VERDICTS.includes(v) ? v : '';
        this.page = 1;
        App.selection.clear('validation');
        this._loadResults();
    },

    doSearch() {
        this.search = (document.getElementById('valSearch')?.value || '').trim();
        this.page = 1;
        App.selection.clear('validation');
        this._loadResults();
    },

    goToPage(p) {
        this.page = p;
        this._loadResults();
    },

    /** IDs (as strings) of the result rows currently on screen. */
    pageIds() {
        return (this._rows || []).map(r => String(r.subscriber_id));
    },

    _selectedIds() {
        return App.selection.values('validation').map(Number).filter(Boolean);
    },

    // ── Actions ──────────────────────────────────────────

    blocklistSelected() { return this._blocklist(this._selectedIds()); },
    blocklistOne(id) { return this._blocklist([id]); },
    dismissSelected() { return this._dismiss(this._selectedIds()); },
    dismissOne(id) { return this._dismiss([id]); },

    async _blocklist(ids) {
        if (!ids.length) return;
        const n = ids.length;
        if (!await App.confirm('Blocklist Addresses',
                `Blocklist ${n} address${n === 1 ? '' : 'es'}? ListMonk stops mailing them. Nothing is deleted, and you can unblock them later from the Leads page.`)) return;
        try {
            const res = await API.put('/api/email-validation/results/blocklist', { ids }, API.LONG_TIMEOUT);
            App.showResult('Blocklist — Done', `
                <ul style="line-height:1.8">
                    <li>Blocklisted: <strong>${res?.blocklisted ?? 0}</strong></li>
                    ${res?.skipped ? `<li>Skipped (no longer in the results): <strong>${res.skipped}</strong></li>` : ''}
                    ${res?.errors ? `<li style="color:var(--ruby)">Errors: <strong>${res.errors}</strong>${res.failed?.[0]?.error ? ' (' + App.escapeHtml(res.failed[0].error) + ')' : ''}</li>` : ''}
                </ul>`);
            App.selection.clear('validation');
            await this._refreshAll();
        } catch {
            // API.request already surfaced the server message.
        }
    },

    async _dismiss(ids) {
        if (!ids.length) return;
        try {
            const res = await API.del(`/api/email-validation/results?${ids.map(i => `ids=${i}`).join('&')}`);
            App.toast(`Dismissed ${res?.dismissed ?? 0}. They return if a later scan flags them again.`, 'success');
            App.selection.clear('validation');
            await this._refreshAll();
        } catch {
            // Already surfaced.
        }
    },

    /** Re-read the counts and the current results page after a change. */
    async _refreshAll() {
        try {
            this.status = await API.get('/api/email-validation/status', true);
            this._paintScan();
        } catch {
            // The results below still refresh.
        }
        await this._loadResults();
    },

    async exportAll() {
        try {
            const result = await API.download('/api/email-validation/results/export'
                + (this.verdict ? `?verdict=${encodeURIComponent(this.verdict)}` : ''));
            API.downloadBlob(result.blob, 'email_validation_results.csv');
            App.toast('Export downloaded', 'success');
        } catch {
            // Already surfaced (e.g. 404 when there is nothing to export).
        }
    },

    /** CSV of the selected rows on this page. */
    exportSelected() {
        const selected = this._selectedIds();
        const rows = (this._rows || []).filter(r => selected.includes(r.subscriber_id));
        if (!rows.length) { App.toast('No selected rows on this page', 'error'); return; }
        const header = ['email', 'name', 'verdict', 'reasons', 'suggestion', 'checked_at'];
        const csvRows = rows.map(r => [
            r.email || '', r.name || '', r.verdict || '', (r.reasons || []).join('; '),
            r.suggestion || '', r.checked_at || '',
        ]);
        const csv = [header, ...csvRows]
            .map(cols => cols.map(v => App.csvCell(v)).join(','))
            .join('\n');
        API.downloadBlob(new Blob([csv], { type: 'text/csv' }), 'selected_email_validation.csv');
        App.toast(`Exported ${rows.length} row(s)`, 'success');
    },
};
