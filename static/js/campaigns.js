/**
 * Campaigns page - CRUD, status control, analytics, preview.
 */
const Campaigns = {
    page: 1,
    query: '',
    statusFilter: '',
    perPage: 50,
    analyticsChart: null,
    _rows: [],

    async render() {
        const seq = App.seq();
        this._detailId = null;
        // Deep-linkable state: #/campaigns?page=2&q=welcome&status=running
        const rq = App.routeQuery();
        if (rq.get('page')) this.page = Math.max(1, parseInt(rq.get('page'), 10) || 1);
        if (rq.get('q') !== null) this.query = rq.get('q') || '';
        if (rq.get('status') !== null) this.statusFilter = rq.get('status') || '';
        const isCold = App.isCold();
        try {
            let params = `page=${this.page}&per_page=${this.perPage}`;
            // Free text goes through the backend's safe ILIKE builder
            // (`search`); `query` is ListMonk's raw-SQL passthrough.
            if (this.query) params += `&search=${encodeURIComponent(this.query)}`;
            if (this.statusFilter) params += `&status=${this.statusFilter}`;

            // Replies per campaign (Cold Email): best effort, a missing
            // mailbox must not hide the campaign table.
            const [result, replyStats] = await Promise.all([
                API.get(`/api/campaigns?${params}`),
                isCold ? API.get('/api/replies/stats', true).catch(() => null) : Promise.resolve(null),
            ]);
            if (App.stale(seq)) return;
            const data = result?.data || {};
            const campaigns = data.results || [];
            const total = data.total || 0;
            // A bulk delete can strand the view past the last page; clamp and
            // re-fetch instead of painting a false "no campaigns" page.
            const clamped = App.clampPage(this.page, total, this.perPage);
            if (clamped !== this.page) { this.page = clamped; return this.render(); }
            this._rows = campaigns;
            const repliesBy = replyStats?.by_campaign || null;

            App.selection.register('campaigns', {
                noun: 'campaign',
                pageValues: this.pageIds(),
                actions: [
                    { label: 'Archive', onclick: 'Campaigns.bulkArchive()' },
                    { label: 'Export Selected', onclick: 'Campaigns.exportSelected()' },
                    { label: 'Delete', onclick: 'Campaigns.bulkDelete()', danger: true },
                ],
            });

            // Engagement columns follow the mode: Cold Email has no tracking
            // pixel (opens are meaningless) but reads replies from the inbox.
            const engageHead = isCold ? '<th>Replies</th>' : '<th>Opens</th>';
            const colCount = 10;

            let html = `
                <div class="search-bar">
                    <div class="search-input">
                        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="11" cy="11" r="8"/><line x1="21" y1="21" x2="16.65" y2="16.65"/></svg>
                        <input type="search" id="campSearch" placeholder="Search campaigns…"
                            aria-label="Search campaigns"
                            value="${App.escapeHtml(this.query)}" oninput="Campaigns.searchLive()" onkeydown="if(event.key==='Enter')Campaigns.search()">
                    </div>
                    <select id="campStatusFilter" aria-label="Filter campaigns by status" onchange="Campaigns.filterStatus(this.value)" style="width:auto">
                        <option value="" ${!this.statusFilter ? 'selected' : ''}>All Status</option>
                        <option value="draft" ${this.statusFilter === 'draft' ? 'selected' : ''}>Draft</option>
                        <option value="running" ${this.statusFilter === 'running' ? 'selected' : ''}>Running</option>
                        <option value="finished" ${this.statusFilter === 'finished' ? 'selected' : ''}>Finished</option>
                        <option value="paused" ${this.statusFilter === 'paused' ? 'selected' : ''}>Paused</option>
                        <option value="cancelled" ${this.statusFilter === 'cancelled' ? 'selected' : ''}>Cancelled</option>
                        <option value="scheduled" ${this.statusFilter === 'scheduled' ? 'selected' : ''}>Scheduled</option>
                    </select>
                    <span class="search-count">${App.formatNumber(total)} campaign${total === 1 ? '' : 's'}</span>
                    <div class="page-actions">
                        <button class="btn btn-primary" onclick="Campaigns.showCreate()">+ New Campaign</button>
                        ${App.moreMenu([
                            { label: 'Export all (CSV)', onclick: 'Campaigns.exportAll()' },
                            { label: 'Open Analytics', onclick: "location.hash='#/analytics'" },
                        ], 'More campaign actions')}
                    </div>
                </div>
                ${App.selection.bar('campaigns')}
                <div class="table-wrapper" tabindex="0" role="region" aria-label="Scrollable data table"><table class="campaigns-table">
                    <thead><tr>
                        <th class="col-select">${App.selection.headbox('campaigns', 'Select all campaigns on this page')}</th>
                        <th>ID</th><th>Name</th><th>Status</th><th>Subject</th><th>Lists</th><th>Sent</th>${engageHead}<th>Bounces</th><th>Created</th><th class="cell-actions">Actions</th>
                    </tr></thead><tbody>`;

            if (!campaigns.length) {
                html += `<tr><td colspan="${colCount + 1}"><div class="empty-state"><h2>No campaigns found</h2>
                    ${this.query || this.statusFilter ? '<p>Try clearing the search or status filter.</p>' : '<p><button class="btn btn-primary" onclick="Campaigns.showCreate()">+ Create your first campaign</button></p>'}
                </div></td></tr>`;
            }

            campaigns.forEach(c => {
                const listNames = (c.lists || []).map(l => `<span class="tag" title="${App.escapeHtml(l.name)}">${App.escapeHtml(l.name)}</span>`).join('') || '-';
                // "Sent" must be the real dispatched count. ListMonk exposes
                // `sent` (delivered to the SMTP server) and `to_send` (audience
                // size); showing `to_send` here reported the whole list as sent.
                const sent = c.sent || 0;
                const audience = c.to_send || 0;
                const live = c.status === 'running' || c.status === 'paused';
                const pct = audience ? Math.min(100, Math.round(sent / audience * 100)) : 0;
                const sentCell = live && audience
                    ? `<div class="cell-progress" title="${App.formatNumber(sent)} of ${App.formatNumber(audience)} sent">
                            <span>${App.formatNumber(sent)} <small>/ ${App.formatNumber(audience)}</small></span>
                            <div class="mini-bar"><div style="width:${pct}%"></div></div>
                       </div>`
                    : `<span title="${audience ? `Audience: ${App.formatNumber(audience)}` : ''}">${App.formatNumber(sent)}</span>`;
                const rate = (n) => sent ? ` <small class="cell-sub">${(n / sent * 100).toFixed(1)}%</small>` : '';
                let engage;
                if (isCold) {
                    const n = repliesBy ? (repliesBy[String(c.id)] || 0) : null;
                    engage = n === null ? '<span class="cell-sub" title="Reply inbox not available">–</span>'
                        : n ? `<a href="#/replies?campaign=${c.id}" title="Open the replies to this campaign">${App.formatNumber(n)}</a>${rate(n)}` : '0';
                } else {
                    engage = `${App.formatNumber(c.views || 0)}${rate(c.views || 0)}`;
                }
                const bounces = c.bounces || 0;
                const bounceCell = bounces
                    ? `<a href="#/bounces?campaign=${c.id}" title="Open this campaign's bounces">${App.formatNumber(bounces)}</a>${rate(bounces)}`
                    : '0';
                const canArchive = ['draft', 'finished', 'cancelled', 'paused'].includes(c.status);
                let primary = '';
                if (c.status === 'draft') primary = `<button class="btn btn-sm btn-primary" onclick="Campaigns.changeStatus(${c.id},'running')">Start</button>`;
                else if (c.status === 'running') primary = `<button class="btn btn-sm btn-warning-outline" onclick="Campaigns.changeStatus(${c.id},'paused')">Pause</button>`;
                else if (c.status === 'paused') primary = `<button class="btn btn-sm btn-primary" onclick="Campaigns.changeStatus(${c.id},'running')">Resume</button>`;
                else if (c.status === 'scheduled') primary = `<button class="btn btn-sm" onclick="Campaigns.changeStatus(${c.id},'draft')">Unschedule</button>`;
                const menu = App.moreMenu([
                    { label: 'Test send', onclick: `Campaigns.showTestSend(${c.id})` },
                    { label: 'Analytics', onclick: `Campaigns.viewAnalytics(${c.id})` },
                    { label: 'Preview', onclick: `Campaigns.preview(${c.id})` },
                    { label: 'Edit', onclick: `Campaigns.showEdit(${c.id})`, hidden: !['draft', 'paused', 'scheduled'].includes(c.status) },
                    { label: 'Archive', onclick: `Campaigns.archive(${c.id})`, hidden: !canArchive },
                    { label: 'Delete…', onclick: `Campaigns.remove(${c.id})`, danger: true },
                ], `More actions for ${c.name}`);
                html += `<tr class="${App.selection.has('campaigns', c.id) ? 'row-selected' : ''}">
                    <td class="col-select">${App.selection.rowbox('campaigns', c.id, `Select ${c.name}`)}</td>
                    <td>${c.id}</td>
                    <td><a class="cell-link cell-clamp cell-clamp-lg" href="javascript:void(0)" onclick="Campaigns.showDetail(${c.id})" title="${App.escapeHtml(c.name)}">${App.escapeHtml(c.name)}</a><span class="phone-only">${App.statusBadge(c.status)}</span></td>
                    <td>${App.statusBadge(c.status)}</td>
                    <td${Campaigns._subjectTooltip(c.subject)}><span class="cell-clamp">${App.escapeHtml(c.subject) || '-'}</span>${Campaigns._subjectBadge(c.subject)}</td>
                    <td>${listNames}</td>
                    <td>${sentCell}</td>
                    <td>${engage}</td>
                    <td>${bounceCell}</td>
                    <td>${App.formatDate(c.created_at)}</td>
                    <td class="cell-actions">
                        <div class="action-btns">
                            ${primary}
                            <button class="btn btn-sm" onclick="Campaigns.showDetail(${c.id})">View</button>
                            ${menu}
                        </div>
                    </td>
                </tr>`;
            });

            html += '</tbody></table></div>';
            html += App.renderPagination(this.page, total, this.perPage, 'Campaigns.goToPage');
            if (App.stale(seq)) return;
            App.syncUrl('campaigns', {
                page: this.page, q: this.query, status: this.statusFilter,
            });
            const keepFocus = document.activeElement?.id === 'campSearch';
            App.setContent(html);
            if (keepFocus) {
                const box = document.getElementById('campSearch');
                if (box) { box.focus(); box.setSelectionRange(box.value.length, box.value.length); }
            }
            // Numbers move while a campaign sends: refresh in place.
            if (campaigns.some(c => c.status === 'running')) {
                App.autoRefresh('campaigns', () => { App.bumpSeq(); this.render(); }, 10000);
            } else {
                App.stopAutoRefresh();
            }
        } catch {
            if (App.stale(seq)) return;
            App.setContent('<div class="empty-state"><h2>Failed to load campaigns</h2></div>');
        }
    },

    searchLive: App.debounce(() => Campaigns.search(), 400),

    /** IDs (as strings) of the rows currently on screen. */
    pageIds() {
        return this._rows.map(c => String(c.id));
    },

    _selectedIds() {
        return App.selection.values('campaigns').map(Number).filter(Boolean);
    },

    search() {
        this.query = document.getElementById('campSearch')?.value || '';
        this.page = 1;
        App.bumpSeq();
        this.render();
    },

    filterStatus(status) {
        this.statusFilter = status;
        this.page = 1;
        App.bumpSeq();
        this.render();
    },

    goToPage(p) { this.page = p; App.bumpSeq(); this.render(); },

    // ── Subject cell helpers ─────────────────────────────
    // ListMonk stores the subject with Go template placeholders intact, so a
    // personalised campaign literally shows `{{ .Subscriber.Attribs.x }}`.
    // Flag that (and expose the raw source) instead of letting it look like a
    // broken subject line.
    _isTemplateSubject(subject) {
        return typeof subject === 'string' && subject.includes('{{');
    },

    _subjectBadge(subject) {
        if (!this._isTemplateSubject(subject)) return '';
        return ' <span class="tag" title="Personalised per recipient when the campaign sends" style="font-size:0.65rem;vertical-align:middle">tpl</span>';
    },

    _subjectTooltip(subject) {
        if (!this._isTemplateSubject(subject)) return '';
        return ` title="Raw subject template: ${App.escapeHtml(subject)}"`;
    },

    /** List checkboxes with their subscriber counts, so the audience size
        is visible while picking. */
    _listChecks(lists, name, selectedIds = []) {
        return lists.map(l => {
            const n = l.subscriber_count;
            const count = typeof n === 'number' ? ` <small class="cell-sub">${App.formatNumber(n)}</small>` : '';
            return `<label class="checkbox-label"><input type="checkbox" name="${name}" value="${l.id}" data-count="${typeof n === 'number' ? n : ''}" ${selectedIds.includes(l.id) ? 'checked' : ''} onchange="Campaigns._syncAudience('${name}')"><span>${App.escapeHtml(l.name)}${count}</span></label>`;
        }).join('');
    },

    _syncAudience(name) {
        const el = document.getElementById(`${name}Audience`);
        if (!el) return;
        const boxes = Array.from(document.querySelectorAll(`input[name="${name}"]:checked`));
        const known = boxes.every(b => b.dataset.count !== '');
        const sum = boxes.reduce((a, b) => a + (parseInt(b.dataset.count, 10) || 0), 0);
        el.textContent = !boxes.length ? 'No list selected'
            : `${boxes.length} list${boxes.length === 1 ? '' : 's'} · ${known ? 'up to ' : ''}${App.formatNumber(sum)} ${App.term('subscriber', { plural: sum !== 1 })}${known ? ' (people on several lists are sent one email)' : ''}`;
    },

    /** <input type=datetime-local> value ("YYYY-MM-DDTHH:MM") from an ISO date. */
    _localInput(iso) {
        if (!iso) return '';
        const d = new Date(iso);
        if (isNaN(d)) return '';
        const pad = (n) => String(n).padStart(2, '0');
        return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}`;
    },

    /** ISO timestamp (with offset) ListMonk accepts for send_at, or null. */
    _sendAt(id) {
        const v = document.getElementById(id)?.value;
        if (!v) return null;
        const d = new Date(v);
        return isNaN(d) ? null : d.toISOString();
    },

    _scheduleField(id, value) {
        return `<div class="form-group">
            <label for="${id}">Send at (optional)</label>
            <input type="datetime-local" id="${id}" value="${this._localInput(value)}">
            <p class="field-hint">Leave empty to start it by hand. With a time set, use <strong>Save &amp; schedule</strong>: ListMonk starts it then
                (your browser's time zone, ${App.escapeHtml(Intl.DateTimeFormat().resolvedOptions().timeZone || 'local')}). The send window and warm-up still apply.</p>
        </div>`;
    },

    _bodyField(id, value, rows = 12) {
        return `<div class="form-group">
            <div class="label-row">
                <label for="${id}">Body</label>
                <button type="button" class="btn btn-sm" onclick="Campaigns.previewDraft()">Preview</button>
            </div>
            <textarea id="${id}" class="code-input" rows="${rows}" spellcheck="false" placeholder="Email body content">${App.escapeHtml(value || '')}</textarea>
        </div>`;
    },

    async showCreate() {
        // Lists with their subscriber counts (the non-minimal listing).
        let lists = [];
        let listsFailed = false;
        try {
            const res = await API.get('/api/lists?per_page=100');
            lists = res?.data?.results || [];
        } catch {
            listsFailed = true;
        }

        // Fetch templates for selection
        let templates = [];
        let templatesFailed = false;
        try {
            const res = await API.get('/api/templates');
            templates = (res?.data || []).filter(t => (t.type || 'campaign') === 'campaign');
        } catch {
            templatesFailed = true;
        }

        // Surface a failed dependency instead of rendering an empty picker
        // that silently makes the form impossible to submit.
        if (listsFailed) App.toast('Could not load lists — the campaign cannot be created until this succeeds.', 'error');
        if (templatesFailed) App.toast('Could not load templates — you can still save without one.', 'error');

        const listOptions = this._listChecks(lists, 'campLists');

        const tplOptions = templates.map(t =>
            `<option value="${t.id}" ${t.is_default ? 'selected' : ''}>${App.escapeHtml(t.name)}</option>`
        ).join('');

        this._draftCtx = { mode: 'create' };
        App.setContent(`
            <div class="inline-form">
                <h2 style="margin-bottom:16px">Create Campaign</h2>
                <div class="form-grid">
                    <div class="form-group">
                        <label>Name *</label>
                        <input type="text" id="campName" placeholder="Campaign name">
                    </div>
                    <div class="form-group">
                        <label>Subject *</label>
                        <input type="text" id="campSubject" placeholder="Quick question for {{ .Subscriber.Attribs.company_name }}">
                    </div>
                </div>
                <p class="field-hint">Subjects and body can use ListMonk placeholders. For cold outreach the per-lead
                    copy already stored on the subscriber works well:
                    <code>{{ .Subscriber.Attribs.email_1_subject }}</code> (subject),
                    <code>{{ .Subscriber.Attribs.company_name }}</code>,
                    <code>{{ .Subscriber.Name }}</code>.</p>
                <div class="form-group">
                    <label>${App.term('list', { plural: true, cap: true })} *</label>
                    <div class="checkbox-group">${listOptions || '<span style="color:var(--text-muted)">No lists available</span>'}</div>
                    <p class="field-hint" id="campListsAudience">No list selected</p>
                </div>
                <div class="form-grid">
                    <div class="form-group">
                        <label>Template</label>
                        <select id="campTemplate">${tplOptions}</select>
                    </div>
                    <div class="form-group">
                        <label>Content Type</label>
                        <select id="campContentType">
                            <option value="richtext">Rich Text</option>
                            <option value="html">HTML</option>
                            <option value="markdown">Markdown</option>
                            <option value="plain">Plain Text</option>
                        </select>
                    </div>
                </div>
                ${this._bodyField('campBody', '')}
                <div class="form-group">
                    <label>Plain-text alternative body (recommended)</label>
                    <textarea id="campAltBody" class="code-input" rows="5" placeholder="Text-only version delivered alongside the HTML. Mail clients and spam filters both weight a matching text part."></textarea>
                </div>
                <div class="form-grid">
                    <div class="form-group"><label>Tags (comma-separated)</label><input type="text" id="campTags" placeholder="newsletter, promo"></div>
                    ${this._scheduleField('campSendAt', null)}
                </div>
                <div class="form-actions">
                    <button class="btn btn-primary" onclick="Campaigns.create(false)">Save draft</button>
                    <button class="btn" onclick="Campaigns.create(true)">Save &amp; schedule</button>
                    <button class="btn btn-secondary" onclick="Campaigns.render()">Cancel</button>
                </div>
                <div id="draftPreview"></div>
            </div>
        `);
    },

    async create(schedule = false) {
        const name = document.getElementById('campName').value.trim();
        const subject = document.getElementById('campSubject').value.trim();
        const templateId = parseInt(document.getElementById('campTemplate').value);
        const contentType = document.getElementById('campContentType').value;
        const body = document.getElementById('campBody').value;
        const altbody = document.getElementById('campAltBody').value;
        const tags = document.getElementById('campTags').value.split(',').map(t => t.trim()).filter(Boolean);
        const sendAt = this._sendAt('campSendAt');

        const selectedLists = Array.from(document.querySelectorAll('input[name="campLists"]:checked'))
            .map(cb => parseInt(cb.value));

        if (!name || !subject) { App.toast('Name and subject are required', 'error'); return; }
        if (!selectedLists.length) { App.toast('Select at least one list', 'error'); return; }
        if (schedule && !sendAt) { App.toast('Pick a "Send at" time to schedule the campaign', 'error'); return; }
        if (sendAt && new Date(sendAt) <= new Date()) { App.toast('"Send at" must be in the future', 'error'); return; }

        try {
            const payload = {
                name, subject, body, altbody, content_type: contentType,
                type: 'regular', lists: selectedLists, tags,
            };
            if (templateId) payload.template_id = templateId;
            if (sendAt) payload.send_at = sendAt;
            const res = await API.post('/api/campaigns', payload);
            const id = res?.data?.id;
            if (schedule && id) {
                try {
                    await API.put(`/api/campaigns/${id}/status`, { status: 'scheduled' });
                    App.toast(`Campaign scheduled for ${App.formatDateTime(sendAt)}`, 'success');
                } catch { /* saved as a draft; the toast already explained */ }
            } else {
                App.toast('Campaign saved as a draft. Preview it or send a test before starting.', 'success');
            }
            // Land on the new campaign: Preview / Test Send / Start live there.
            if (id) this.showDetail(id); else this.render();
        } catch {
            // API.request already surfaced the server message.
        }
    },

    /** Preview of the body being edited. A saved campaign is rendered by
        ListMonk with its template and a sample subscriber (unsaved edits
        included); a new one has no id yet, so its body is shown as written. */
    async previewDraft() {
        const box = document.getElementById('draftPreview');
        if (!box) return;
        const ctx = this._draftCtx || {};
        const bodyEl = document.getElementById(ctx.mode === 'edit' ? 'editCampBody' : 'campBody');
        const body = bodyEl ? bodyEl.value : '';
        const contentType = (document.getElementById(ctx.mode === 'edit' ? 'editCampContentType' : 'campContentType') || {}).value || 'richtext';
        const tplEl = document.getElementById(ctx.mode === 'edit' ? 'editCampTemplate' : 'campTemplate');
        const templateId = tplEl ? parseInt(tplEl.value, 10) || null : null;
        box.innerHTML = '<div class="loading-spinner">Rendering preview…</div>';
        box.scrollIntoView({ behavior: 'smooth', block: 'start' });
        let html = null;
        let note = '';
        if (ctx.mode === 'edit' && ctx.id) {
            try {
                const res = await API.request('POST', `/api/campaigns/${ctx.id}/preview`,
                    { body, content_type: contentType, template_id: templateId }, false, true);
                html = res?.html ?? null;
                note = 'Rendered by ListMonk with the template and a sample subscriber, including your unsaved changes.';
            } catch {
                note = 'ListMonk could not render this (check the body for template errors). Showing the body as written.';
            }
        } else {
            note = 'The body as written. Placeholders like {{ .Subscriber.Name }} and the template are applied after you save; use Preview on the saved draft for the exact email.';
        }
        if (html === null) {
            html = (contentType === 'plain' || contentType === 'markdown')
                ? `<pre style="white-space:pre-wrap;font:14px/1.5 system-ui,sans-serif;margin:16px">${App.escapeHtml(body)}</pre>`
                : body;
        }
        box.innerHTML = `
            <div class="draft-preview">
                <div class="label-row"><strong>Preview</strong><button type="button" class="btn btn-sm" onclick="document.getElementById('draftPreview').innerHTML=''">Close</button></div>
                <p class="field-hint">${App.escapeHtml(note)}</p>
                <iframe class="preview-frame" sandbox="" srcdoc="${App.escapeHtml(html)}"></iframe>
            </div>`;
    },

    _detailId: null,

    async showDetail(id) {
        const seq = App.bumpSeq();
        const isCold = App.isCold();
        try {
            const [result, unsubRes, replyRes, formatRes] = await Promise.allSettled([
                API.get(`/api/campaigns/${id}`),
                API.get(`/api/unsubscribes/campaign/${id}`, true),
                isCold ? API.get(`/api/replies/stats?campaign_id=${id}`, true) : Promise.resolve(null),
                // HTML design / plain text switch (same endpoint as the
                // Deliverability page), shown with the campaign it changes.
                API.get('/api/deliverability/email-format', true),
            ]);
            if (App.stale(seq)) return;

            const c = result.status === 'fulfilled' ? (result.value?.data || {}) : {};
            if (!c.id) throw new Error('Campaign not found');
            this._detailId = c.id;
            // Test Send looks the campaign up in _rows; a deep link to the
            // detail view has no table loaded.
            if (!this._rows.some(r => r.id === c.id)) this._rows = [...this._rows, c];

            const unsubData = unsubRes.status === 'fulfilled' ? (unsubRes.value?.data || {}) : {};
            const unsubTotal = unsubData.total || 0;
            const unsubEmail = unsubData.email_count || 0;
            const unsubLink = unsubData.link_count || 0;
            const replies = replyRes.status === 'fulfilled' ? replyRes.value : null;
            const fmtRow = formatRes.status === 'fulfilled'
                ? ((formatRes.value?.campaigns || []).find(r => r.id === c.id) || null) : null;
            let formatCell = '';
            if (fmtRow) {
                if (fmtRow.format === 'text' || fmtRow.format === 'visual') {
                    formatCell = fmtRow.format === 'text' ? 'Text only' : 'Visual design';
                } else {
                    const dis = !fmtRow.can_switch || this._switchingFormat;
                    const b = (f, l) => `<button type="button" class="deliv-seg-btn ${fmtRow.format === f ? 'is-active' : ''}" aria-pressed="${fmtRow.format === f}" ${dis ? 'disabled' : ''} onclick="Campaigns.switchFormat(${c.id}, '${f}', '${fmtRow.format}')">${l}</button>`;
                    formatCell = `<div class="deliv-seg" role="group" aria-label="Email format">${b('html', 'HTML design')}${b('plain', 'Plain text')}</div>
                        ${fmtRow.reason ? `<div class="cell-sub" style="margin-top:4px">${App.escapeHtml(fmtRow.reason)}</div>` : ''}`;
                }
            }

            const lists = (c.lists || []).map(l => `<a class="tag" href="#/subscribers?list=${l.id}">${App.escapeHtml(l.name)}</a>`).join('') || '-';
            const tags = (c.tags || []).map(t => `<span class="tag">${App.escapeHtml(t)}</span>`).join('') || '-';

            const pctOf = (n) => c.sent ? ((n || 0) / c.sent * 100).toFixed(1) : '0';
            // ListMonk rejects updates for anything but draft/paused/scheduled
            // ("canEditCampaign"), so offering Edit on a running/finished
            // campaign only led to a 400 after the form was filled out.
            const canEdit = ['draft', 'paused', 'scheduled'].includes(c.status);
            const canArchive = ['draft', 'finished', 'cancelled', 'paused'].includes(c.status);

            let primary = '';
            if (c.status === 'draft') {
                primary = c.send_at
                    ? `<button class="btn btn-sm btn-primary" onclick="Campaigns.changeStatus(${c.id},'scheduled')">Schedule</button>
                       <button class="btn btn-sm" onclick="Campaigns.changeStatus(${c.id},'running')">Start now</button>`
                    : `<button class="btn btn-sm btn-primary" onclick="Campaigns.changeStatus(${c.id},'running')">Start</button>`;
            } else if (c.status === 'running') {
                primary = `<button class="btn btn-sm btn-warning-outline" onclick="Campaigns.changeStatus(${c.id},'paused')">Pause</button>`;
            } else if (c.status === 'paused') {
                primary = `<button class="btn btn-sm btn-primary" onclick="Campaigns.changeStatus(${c.id},'running')">Resume</button>`;
            } else if (c.status === 'scheduled') {
                primary = `<button class="btn btn-sm" onclick="Campaigns.changeStatus(${c.id},'draft')">Unschedule</button>`;
            }

            // Live progress while sending: ListMonk's running stats carry the
            // send rate, so the card can say how long is left.
            const live = c.status === 'running' || c.status === 'paused';
            const audience = c.to_send || 0;
            const pct = audience ? Math.min(100, Math.round((c.sent || 0) / audience * 100)) : 0;
            let progress = '';
            if (live && audience) {
                let rateLine = '';
                if (c.status === 'running') {
                    try {
                        const rs = await API.get(`/api/campaigns/running/stats?campaign_id=${c.id}`, true);
                        if (App.stale(seq)) return;
                        const st = (rs?.data || []).find(x => x.id === c.id);
                        const rate = st?.rate || 0;
                        if (rate > 0) {
                            const left = Math.max(0, audience - (st.sent || c.sent || 0));
                            const secs = left / rate;
                            const eta = secs < 90 ? `${Math.ceil(secs)} s` : secs < 5400 ? `${Math.ceil(secs / 60)} min` : `${(secs / 3600).toFixed(1)} h`;
                            rateLine = ` · ${rate.toFixed(1)} msg/s · about ${eta} left`;
                        }
                    } catch { /* rate is optional */ }
                }
                progress = `
                    <div class="card campaign-progress">
                        <div class="campaign-progress-head">
                            <strong>${c.status === 'running' ? 'Sending' : 'Paused'}</strong>
                            <span>${App.formatNumber(c.sent || 0)} of ${App.formatNumber(audience)} sent (${pct}%)${rateLine}</span>
                        </div>
                        <div class="progress-track"><div class="progress-fill${c.status === 'paused' ? ' is-paused' : ''}" style="width:${pct}%"></div></div>
                        ${c.status === 'running' ? '<p class="cell-sub" style="margin:8px 0 0">Updates every 10 seconds.</p>' : ''}
                    </div>`;
            }

            const engagementCard = isCold
                ? `<a class="stat-card stat-card-link" href="#/replies?campaign=${c.id}" style="border-left:3px solid var(--success)">
                        <div class="stat-label">Replies</div>
                        <div class="stat-value">${replies ? App.formatNumber(replies.human_replies || 0) : '–'}</div>
                        <div class="stat-sub">${replies
                            ? `${replies.positive || 0} positive · ${replies.reply_rate || 0}% reply rate`
                            : 'Reply inbox not available'}</div>
                   </a>`
                : `<div class="stat-card" style="border-left:3px solid var(--success)">
                        <div class="stat-label">Opens</div>
                        <div class="stat-value">${App.formatNumber(c.views || 0)}</div>
                        <div class="stat-sub">${pctOf(c.views)}% open rate</div>
                   </div>
                   <div class="stat-card" style="border-left:3px solid var(--warning)">
                        <div class="stat-label">Clicks</div>
                        <div class="stat-value">${App.formatNumber(c.clicks || 0)}</div>
                        <div class="stat-sub">${pctOf(c.clicks)}% CTR</div>
                   </div>`;

            App.setContent(`
                <div class="inline-form">
                    <div class="detail-head">
                        <div>
                            <button class="btn btn-sm btn-secondary" onclick="Campaigns._detailId=null;Campaigns.render()">← Back</button>
                            <h2>${App.escapeHtml(c.name)}</h2>
                        </div>
                        <div class="action-btns">
                            ${primary}
                            ${canEdit ? `<button class="btn btn-sm" onclick="Campaigns.showEdit(${c.id})">Edit</button>` : ''}
                            <button class="btn btn-sm" onclick="Campaigns.preview(${c.id})">Preview</button>
                            <button class="btn btn-sm" onclick="Campaigns.showTestSend(${c.id})">Test Send</button>
                            ${App.moreMenu([
                                { label: 'View analytics', onclick: `Campaigns.viewAnalytics(${c.id})` },
                                { label: 'Export engagement CSV', onclick: `Campaigns.exportCampaign(${c.id})` },
                                { label: 'Archive', onclick: `Campaigns.archive(${c.id})`, hidden: !canArchive },
                                { label: 'Delete…', onclick: `Campaigns.remove(${c.id})`, danger: true },
                            ], 'More campaign actions')}
                        </div>
                    </div>
                    ${progress}
                    <div class="stats-grid">
                        <div class="stat-card"><div class="stat-label">Status</div><div style="margin-top:4px">${App.statusBadge(c.status)}</div>
                            ${c.status === 'scheduled' || (c.status === 'draft' && c.send_at) ? `<div class="stat-sub">Send at ${App.escapeHtml(App.formatDateTime(c.send_at))}</div>` : ''}</div>
                        <div class="stat-card accent"><div class="stat-label">Audience</div><div class="stat-value">${App.formatNumber(c.to_send || 0)}</div></div>
                        <div class="stat-card success"><div class="stat-label">Sent</div><div class="stat-value">${App.formatNumber(c.sent || 0)}</div></div>
                        ${engagementCard}
                        <a class="stat-card stat-card-link" href="#/bounces?campaign=${c.id}" style="border-left:3px solid var(--danger)">
                            <div class="stat-label">Bounces</div>
                            <div class="stat-value">${App.formatNumber(c.bounces || 0)}</div>
                            <div class="stat-sub">${pctOf(c.bounces)}% bounce rate</div>
                        </a>
                        <a class="stat-card stat-card-link" href="#/unsubscribes?campaign=${c.id}" style="border-left:3px solid var(--accent)">
                            <div class="stat-label">Unsubscribes</div>
                            <div class="stat-value">${App.formatNumber(unsubTotal)}</div>
                            <div class="stat-sub">${unsubEmail} email · ${unsubLink} link</div>
                        </a>
                    </div>
                    <table class="detail-table">
                        <tr><td>Subject</td><td>${App.escapeHtml(c.subject) || '-'}</td></tr>
                        <tr><td>${App.term('list', { plural: true, cap: true })}</td><td>${lists}</td></tr>
                        <tr><td>Tags</td><td>${tags}</td></tr>
                        <tr><td>Content Type</td><td>${App.escapeHtml(c.content_type || '-')}</td></tr>
                        ${formatCell ? `<tr><td>Email format</td><td>${formatCell}</td></tr>` : ''}
                        <tr><td>Created</td><td>${App.formatDateTime(c.created_at)}</td></tr>
                        <tr><td>Updated</td><td>${App.formatDateTime(c.updated_at)}</td></tr>
                        <tr><td>Started</td><td>${App.formatDateTime(c.started_at)}</td></tr>
                        ${c.send_at ? `<tr><td>Scheduled for</td><td>${App.formatDateTime(c.send_at)}</td></tr>` : ''}
                    </table>
                </div>
            `);
            if (c.status === 'running') {
                App.autoRefresh('campaigns', () => {
                    if (this._detailId === c.id) this.showDetail(c.id);
                }, 10000);
            } else {
                App.stopAutoRefresh();
            }
        } catch (err) {
            // Errors thrown locally (e.g. "Campaign not found") were never
            // toasted by API.request — surface them instead of failing silently.
            if (!err?.handled) App.toast(err?.message || 'Failed to load campaign', 'error');
        }
    },

    async switchFormat(id, fmt, current) {
        if (fmt === current || this._switchingFormat) return;
        this._switchingFormat = true;
        try {
            const res = await API.put(`/api/deliverability/email-format/${id}`, { format: fmt }, API.LONG_TIMEOUT);
            App.toast(`Now sends as ${fmt === 'plain' ? 'plain text' : 'HTML design'}${res?.template_name ? ` (${res.template_name})` : ''}.`, 'success');
        } catch { /* API.request already surfaced the server message */ }
        this._switchingFormat = false;
        if (this._detailId === id) this.showDetail(id);
    },

    viewAnalytics(id) {
        // Jump to analytics page with this campaign pre-selected
        Analytics.selectedCampaignId = id;
        Analytics.fromDate = '';
        Analytics.toDate = '';
        window.location.hash = '#/analytics';
    },

    async exportCampaign(id) {
        // Cold email mode does not track clicks, so no clicker file is exported there.
        const types = App.currentMode === 'cold_email'
            ? ['views', 'bounces']
            : ['views', 'clicks', 'bounces'];
        let exported = 0;

        for (const type of types) {
            try {
                const result = await API.download(`/api/campaigns/${id}/subscribers/${type}/export`);
                if (result.blob) {
                    API.downloadBlob(result.blob, `campaign_${id}_${type}.csv`);
                    exported++;
                }
            } catch {}
        }

        if (exported > 0) {
            App.toast(`Exported ${exported} subscriber list(s)`, 'success');
        } else {
            App.toast('No data to export', 'error');
        }
    },

    async showEdit(id) {
        const seq = App.bumpSeq();
        try {
            const [campRes, listsRes, tplRes] = await Promise.all([
                API.get(`/api/campaigns/${id}`),
                API.get('/api/lists?per_page=100'),
                API.get('/api/templates', true).catch(() => null),
            ]);
            if (App.stale(seq)) return;

            const c = campRes?.data || {};
            const allLists = listsRes?.data?.results || [];
            const selectedIds = (c.lists || []).map(l => l.id);
            const templates = (tplRes?.data || []).filter(t => (t.type || 'campaign') === 'campaign');
            // Keep the full record: ListMonk's PUT replaces the whole list
            // set, so memberships the checkboxes cannot show (lists beyond
            // the fetched page) must be echoed back on save.
            this._editing = c;
            this._draftCtx = { mode: 'edit', id: c.id };

            const listOptions = this._listChecks(allLists, 'editCampLists', selectedIds);
            const tplOptions = templates.map(t =>
                `<option value="${t.id}" ${t.id === c.template_id ? 'selected' : ''}>${App.escapeHtml(t.name)}</option>`
            ).join('');
            const ctypes = [['richtext', 'Rich Text'], ['html', 'HTML'], ['markdown', 'Markdown'], ['plain', 'Plain Text']];
            const visual = c.content_type === 'visual';

            App.setContent(`
                <div class="inline-form">
                    <h2 style="margin-bottom:16px">Edit Campaign #${c.id}</h2>
                    <div class="form-grid">
                        <div class="form-group"><label>Name</label><input type="text" id="editCampName" value="${App.escapeHtml(c.name || '')}"></div>
                        <div class="form-group"><label>Subject</label><input type="text" id="editCampSubject" value="${App.escapeHtml(c.subject || '')}"></div>
                    </div>
                    <div class="form-group">
                        <label>${App.term('list', { plural: true, cap: true })}</label>
                        <div class="checkbox-group">${listOptions}</div>
                        <p class="field-hint" id="editCampListsAudience"></p>
                    </div>
                    ${visual ? '' : `<div class="form-grid">
                        <div class="form-group">
                            <label>Template</label>
                            <select id="editCampTemplate">${tplOptions}</select>
                        </div>
                        <div class="form-group">
                            <label>Content Type</label>
                            <select id="editCampContentType">${ctypes.map(([v, l]) => `<option value="${v}" ${c.content_type === v ? 'selected' : ''}>${l}</option>`).join('')}</select>
                        </div>
                    </div>`}
                    ${this._bodyField('editCampBody', c.body)}
                    <div class="form-group">
                        <label>Plain-text alternative body</label>
                        <textarea id="editCampAltBody" class="code-input" rows="5" placeholder="Text-only version delivered alongside the HTML.">${App.escapeHtml(c.altbody || '')}</textarea>
                    </div>
                    <div class="form-grid">
                        <div class="form-group"><label>Tags</label><input type="text" id="editCampTags" value="${App.escapeHtml((c.tags || []).join(', '))}"></div>
                        ${this._scheduleField('editCampSendAt', c.send_at)}
                    </div>
                    <div class="form-actions">
                        <button class="btn btn-primary" onclick="Campaigns.update(${c.id})">Save</button>
                        ${c.status === 'draft' ? `<button class="btn" onclick="Campaigns.update(${c.id}, true)">Save &amp; schedule</button>` : ''}
                        <button class="btn btn-secondary" onclick="Campaigns.showDetail(${c.id})">Cancel</button>
                    </div>
                    <div id="draftPreview"></div>
                </div>
            `);
            this._syncAudience('editCampLists');
        } catch {
            // API.request already surfaced the server message.
        }
    },

    async update(id, schedule = false) {
        const name = document.getElementById('editCampName').value.trim();
        const subject = document.getElementById('editCampSubject').value.trim();
        const body = document.getElementById('editCampBody').value;
        const altbody = document.getElementById('editCampAltBody').value;
        const tags = document.getElementById('editCampTags').value.split(',').map(t => t.trim()).filter(Boolean);
        const sendAt = this._sendAt('editCampSendAt');
        const boxes = Array.from(document.querySelectorAll('input[name="editCampLists"]'));
        const checked = boxes.filter(cb => cb.checked).map(cb => parseInt(cb.value));
        // Merge back any stored membership the form could not render, so a
        // save never silently detaches a list beyond the fetched page.
        const renderedIds = new Set(boxes.map(cb => parseInt(cb.value)));
        const stored = (this._editing && this._editing.id === id)
            ? (this._editing.lists || []).map(l => l.id)
            : [];
        const lists = [...checked, ...stored.filter(lid => !renderedIds.has(lid))];
        if (schedule && !sendAt) { App.toast('Pick a "Send at" time to schedule the campaign', 'error'); return; }
        if (schedule && new Date(sendAt) <= new Date()) { App.toast('"Send at" must be in the future', 'error'); return; }

        const payload = { name, subject, body, altbody, lists, tags, send_at: sendAt };
        const tpl = document.getElementById('editCampTemplate');
        const ctype = document.getElementById('editCampContentType');
        if (tpl && tpl.value) payload.template_id = parseInt(tpl.value, 10);
        if (ctype) payload.content_type = ctype.value;
        try {
            await API.put(`/api/campaigns/${id}`, payload);
            if (schedule) {
                try {
                    await API.put(`/api/campaigns/${id}/status`, { status: 'scheduled' });
                    App.toast(`Campaign scheduled for ${App.formatDateTime(sendAt)}`, 'success');
                } catch { /* saved; the status error was toasted */ }
            } else {
                App.toast('Campaign updated', 'success');
            }
            this.showDetail(id);
        } catch {
            // API.request already surfaced the server message.
        }
    },

    async changeStatus(id, status) {
        // Correct English for both the confirm question and the toast:
        // "pauseded" shipped because the past tense was built by appending
        // "ed" to the raw status.
        const verbs = {
            running: ['start', 'started'], paused: ['pause', 'paused'], cancelled: ['cancel', 'cancelled'],
            scheduled: ['schedule', 'scheduled'], draft: ['unschedule', 'unscheduled'],
        };
        const [action, past] = verbs[status] || [status, `${status}d`];
        if (await App.confirm('Change Status', `Are you sure you want to ${action} this campaign?`)) {
            try {
                await API.put(`/api/campaigns/${id}/status`, { status });
                App.toast(`Campaign ${past}`, 'success');
                // Stay where the action was taken (detail view or table).
                if (this._detailId === id) this.showDetail(id); else this.render();
            } catch {
                // API.request already surfaced the server message.
            }
        }
    },

    async preview(id) {
        const seq = App.seq();
        try {
            const result = await API.get(`/api/campaigns/${id}/preview`);
            if (App.stale(seq)) return;
            App.setContent(`
                <div style="margin-bottom:12px">
                    <button class="btn btn-secondary" onclick="Campaigns.showDetail(${id})">Back to Campaign</button>
                </div>
                <iframe class="preview-frame" sandbox="" srcdoc="${App.escapeHtml(result.html)}"></iframe>
            `);
        } catch {
            // API.request already surfaced the server message.
        }
    },

    async remove(id) {
        if (await App.confirm('Delete Campaign', 'Are you sure you want to delete this campaign?')) {
            try {
                await API.del(`/api/campaigns/${id}`);
                App.toast('Campaign deleted', 'success');
                App.selection.clear('campaigns');
                this.render();
            } catch {
                // API.request already surfaced the server message.
            }
        }
    },

    async archive(id) {
        if (!await App.confirm('Archive Campaign',
                'Archive this campaign? It stays in the database and is hidden from the active list.')) return;
        try {
            await API.put(`/api/campaigns/${id}/archive`);
            App.toast('Campaign archived', 'success');
            this.render();
        } catch {
            // API.request already surfaced the server message.
        }
    },

    /** Send a test copy of the campaign to an arbitrary address. */
    showTestSend(id) {
        const camp = this._rows.find(c => c.id === id) || {};
        // showForm sets the title via textContent — escaping here would
        // show the entities literally.
        App.showForm(`Test Send: ${camp.name || '#' + id}`, `
            <p style="color:var(--text-secondary)">ListMonk renders the campaign with that ${App.term('subscriber')}'s data and delivers it to the address below. The address must already exist as a ${App.term('subscriber')} in ListMonk.</p>
            <div class="form-group" style="margin-top:12px">
                <label>Recipient email (existing ${App.term('subscriber')})</label>
                <input type="email" id="testSendEmail" placeholder="you@example.com" value="${App.escapeHtml(this._lastTestEmail || '')}">
            </div>
        `, 'Send test', () => this.sendTest(id));
    },

    /** Returns false to keep the dialog open on a validation/send failure. */
    async sendTest(id) {
        const email = document.getElementById('testSendEmail')?.value.trim();
        if (!email) { App.toast('Enter a recipient email', 'error'); return false; }
        // Cheap client-side shape check: the server validates too, but a typo
        // otherwise costs a round-trip and a generic upstream error.
        if (!/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(email)) {
            App.toast('That does not look like a valid email address', 'error');
            return false;
        }
        try {
            await API.post(`/api/campaigns/${id}/test`, { emails: [email] });
            this._lastTestEmail = email;
            App.toast(`Test email sent to ${email}`, 'success');
            return true;
        } catch {
            // API.request already surfaced the server message.
            return false;
        }
    },

    // ── Bulk actions ─────────────────────────────────────
    async bulkArchive() {
        const ids = this._selectedIds();
        if (!ids.length) return;
        if (!await App.confirm('Archive Campaigns',
                `Archive ${ids.length} selected campaign(s)?`)) return;
        try {
            const result = await API.put('/api/campaigns/archive', { ids }, API.LONG_TIMEOUT);
            const errors = result?.errors || 0;
            App.toast(`Archived ${result?.archived ?? ids.length} campaign(s)${errors ? `, ${errors} failed` : ''}`,
                errors ? 'info' : 'success');
            App.selection.clear('campaigns');
            this.render();
        } catch {
            // API.request already surfaced the server message.
        }
    },

    async bulkDelete() {
        const ids = this._selectedIds();
        if (!ids.length) return;
        if (!await App.confirm('Delete Campaigns',
                `Permanently delete ${ids.length} selected campaign(s) and all their stats? This cannot be undone.`)) return;
        try {
            const params = ids.map(id => `ids=${id}`).join('&');
            await API.del(`/api/campaigns?${params}`, API.LONG_TIMEOUT);
            App.toast(`${ids.length} campaign(s) deleted`, 'success');
            App.selection.clear('campaigns');
            this.render();
        } catch {
            // API.request already surfaced the server message.
        }
    },

    /** CSV of the selected campaigns loaded on this page. */
    exportSelected() {
        const selected = this._selectedIds();
        const rows = this._rows.filter(c => selected.includes(c.id));
        if (!rows.length) { App.toast('No selected rows on this page', 'error'); return; }

        const header = ['id', 'name', 'subject', 'status', 'lists', 'sent', 'views', 'clicks', 'bounces', 'created_at'];
        const csvRows = rows.map(c => [
            c.id, c.name, c.subject, c.status,
            (c.lists || []).map(l => l.name).join('; '),
            c.sent || 0, c.views || 0, c.clicks || 0, c.bounces || 0, c.created_at || '',
        ]);
        const csv = [header, ...csvRows]
            .map(cols => cols.map(v => App.csvCell(v)).join(','))
            .join('\n');

        API.downloadBlob(new Blob([csv], { type: 'text/csv' }), 'selected_campaigns.csv');
        App.toast(`Exported ${rows.length} row(s)`, 'success');
    },

    // ── Export ────────────────────────────────────────────
    async exportAll() {
        try {
            const result = await API.download('/api/campaigns/export-all');
            if (result.blob) {
                API.downloadBlob(result.blob, 'campaigns_export.csv');
                App.toast('Campaigns exported', 'success');
            }
        } catch {
            // API.request already surfaced the server message.
        }
    },
};
