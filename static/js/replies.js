/**
 * Replies & Prospect Feed for ListMonk Pro Cold Outreach.
 * Real-time IMAP reply monitoring, sentiment filtering, and lead tracking.
 *
 * Layout: an inbox. A summary strip (totals + sentiment tabs that double as
 * the sentiment filter), one compact toolbar, a scannable list of replies on
 * the left and the selected reply — message, status switch, actions — pinned on
 * the right. Below 900px the two panes become list → reading navigation.
 *
 * Rendering: renderPage() paints the shell once per visit; every in-page
 * change (filter, search, status, scan, bulk action) re-fetches and calls
 * refresh(), which repaints only the regions that depend on data — so the
 * search box keeps focus and the page keeps its scroll position.
 */
const Replies = {
    page: 1,
    perPage: 20,
    sentiment: '',
    status: '',
    campaignId: 0,
    query: '',
    stats: null,
    repliesData: null,
    isScanning: false,
    // Reply currently shown in the reading pane (string id) and, on narrow
    // screens, whether the reading pane replaces the list.
    activeId: null,
    reading: false,
    // Automation panel (the old row of toggles) is collapsed by default.
    autoOpen: false,
    // Auto-blocklist toggles (reply settings); loaded with the page data.
    settings: { auto_blocklist_machine: false, auto_blocklist_ooo: false, auto_tag_leads: true, scan_interval_minutes: 15 },

    /** One entry per classifier output. `cls` sets the --sent colour. */
    SENTIMENTS: {
        positive: { label: 'Interested', long: 'Interested / Lead', cls: 'sent-positive', hint: 'Interested replies and call requests' },
        neutral: { label: 'Neutral', long: 'Neutral / No Signal', cls: 'sent-neutral', hint: 'Ambiguous replies with no clear intent' },
        auto_reply: { label: 'Automated', long: 'Automated / Auto-Reply', cls: 'sent-auto', hint: 'Out-of-office, ticket confirmations and other auto-responses' },
        negative: { label: 'Not interested', long: 'Not Interested', cls: 'sent-negative', hint: 'Unsubscribe requests and rejections' },
    },

    STATUSES: [
        { value: 'unread', label: 'Unread' },
        { value: 'lead', label: 'Hot lead' },
        { value: 'replied', label: 'Replied' },
        { value: 'archived', label: 'Archived' },
    ],

    ICONS: {
        search: '<circle cx="11" cy="11" r="8"/><line x1="21" y1="21" x2="16.65" y2="16.65"/>',
        refresh: '<polyline points="23 4 23 10 17 10"/><path d="M20.49 15a9 9 0 1 1-2.12-9.36L23 10"/>',
        sliders: '<line x1="4" y1="21" x2="4" y2="14"/><line x1="4" y1="10" x2="4" y2="3"/><line x1="12" y1="21" x2="12" y2="12"/><line x1="12" y1="8" x2="12" y2="3"/><line x1="20" y1="21" x2="20" y2="16"/><line x1="20" y1="12" x2="20" y2="3"/><line x1="1" y1="14" x2="7" y2="14"/><line x1="9" y1="8" x2="15" y2="8"/><line x1="17" y1="16" x2="23" y2="16"/>',
        copy: '<rect x="9" y="9" width="13" height="13" rx="2" ry="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/>',
        ban: '<circle cx="12" cy="12" r="10"/><line x1="4.93" y1="4.93" x2="19.07" y2="19.07"/>',
        trash: '<polyline points="3 6 5 6 21 6"/><path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/>',
        star: '<polygon points="12 2 15.09 8.26 22 9.27 17 14.14 18.18 21.02 12 17.77 5.82 21.02 7 14.14 2 9.27 8.91 8.26 12 2"/>',
        left: '<polyline points="15 18 9 12 15 6"/>',
        right: '<polyline points="9 18 15 12 9 6"/>',
        x: '<line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/>',
        mail: '<path d="M4 4h16c1.1 0 2 .9 2 2v12c0 1.1-.9 2-2 2H4c-1.1 0-2-.9-2-2V6c0-1.1.9-2 2-2z"/><polyline points="22,6 12,13 2,6"/>',
    },

    icon(name, size = 15, filled = false) {
        return `<svg viewBox="0 0 24 24" fill="${filled ? 'currentColor' : 'none'}" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" width="${size}" height="${size}" aria-hidden="true">${this.ICONS[name] || ''}</svg>`;
    },

    /** Navigation entry (App.pages → Replies.render()). Reads the deep-link
        query exactly once per visit; in-page changes use reload() so a stale
        route query can never override a filter the user just changed. */
    async render() {
        // An action callback (status update, bulk op) can fire after the user
        // navigated away; never repaint over another page.
        if (App.currentPage !== 'replies') return;
        this._seq = App.seq();
        // Deep-linkable state: #/replies?page=2&sentiment=positive&campaign=6
        const rq = App.routeQuery();
        if (rq.get('page')) this.page = Math.max(1, parseInt(rq.get('page'), 10) || 1);
        if (rq.get('q') !== null) this.query = rq.get('q') || '';
        if (rq.get('sentiment') !== null) this.sentiment = rq.get('sentiment') || '';
        if (rq.get('status') !== null) this.status = rq.get('status') || '';
        if (rq.get('campaign') !== null) this.campaignId = parseInt(rq.get('campaign'), 10) || 0;
        this.reading = false;
        this._bindKeys();
        App.setContent('<div class="loading-spinner">Loading prospect replies…</div>');
        await this.reload(true);
    },

    /** Re-fetch and repaint. `withSettings` is only needed on page entry. */
    async reload(withSettings = false) {
        if (App.currentPage !== 'replies') return;
        this._seq = App.seq();
        const seq = this._seq;

        try {
            await Promise.all([
                this.loadStats(),
                this.loadReplies(),
                withSettings ? this.loadSettings() : Promise.resolve(),
            ]);
        } catch (err) {
            console.error('Replies load error:', err);
            if (App.stale(seq)) return;
            // API.request already toasted the server message; with the page
            // already on screen, keep it rather than blanking it.
            if (document.getElementById('repliesPage')) return;
            App.setContent(`<div class="empty-state"><h2>Failed to load replies</h2><p>${App.escapeHtml(err?.message || '')}</p></div>`);
            return;
        }
        if (App.stale(seq)) return;
        if (document.getElementById('repliesPage')) this.refresh();
        else this.renderPage();
    },

    async loadSettings() {
        try {
            const s = await API.get('/api/replies/settings');
            this.settings = {
                auto_blocklist_machine: !!s.auto_blocklist_machine,
                auto_blocklist_ooo: !!s.auto_blocklist_ooo,
                auto_tag_leads: s.auto_tag_leads !== false,
                scan_interval_minutes: Number(s.scan_interval_minutes) || 15,
            };
        } catch {
            // Defaults keep the toggles renderable without a settings file.
        }
    },

    async toggleAutoBlocklist(key, enabled) {
        try {
            await API.put('/api/replies/settings', { [key]: enabled });
            this.settings[key] = enabled;
            App.toast(`Auto-blocklist ${enabled ? 'enabled' : 'disabled'}`, 'success');
        } catch {
            // API.request already surfaced the server message —
            // toasting again here showed the failure twice. Revert the switch.
            this.settings[key] = !enabled;
        }
        this._paintAutomation();
    },

    async toggleAutoTagLeads(enabled) {
        try {
            await API.put('/api/replies/settings', { auto_tag_leads: enabled });
            this.settings.auto_tag_leads = enabled;
            App.toast(`Auto Hot Lead flagging ${enabled ? 'enabled' : 'disabled'}`, 'success');
        } catch {
            // API.request already toasted the server error; revert the switch.
            this.settings.auto_tag_leads = !enabled;
        }
        this._paintAutomation();
    },

    async setScanInterval(minutes) {
        const previous = this.settings.scan_interval_minutes;
        try {
            await API.put('/api/replies/settings', { scan_interval_minutes: minutes });
            this.settings.scan_interval_minutes = minutes;
            App.toast(`Mailbox scanned every ${minutes} min`, 'success');
        } catch {
            this.settings.scan_interval_minutes = previous;
        }
        this._paintAutomation();
    },

    async loadStats() {
        try {
            const params = new URLSearchParams();
            if (this.campaignId) params.append('campaign_id', this.campaignId);
            this.stats = await API.get(`/api/replies/stats?${params.toString()}`);
        } catch (e) {
            this.stats = { total_replies: 0, positive: 0, auto_reply: 0, negative: 0, reply_rate: 0, campaign_options: [] };
        }
    },

    async loadReplies() {
        const params = new URLSearchParams({
            page: this.page,
            per_page: this.perPage,
        });
        if (this.sentiment) params.append('sentiment', this.sentiment);
        if (this.status) params.append('status', this.status);
        if (this.campaignId) params.append('campaign_id', this.campaignId);
        if (this.query) params.append('q', this.query);

        this.repliesData = await API.get(`/api/replies?${params.toString()}`);
    },

    // ── Small helpers ────────────────────────────────────

    _esc(s) { return App.escapeHtml(String(s ?? '')); },

    /** A JS string literal that is safe inside an HTML onclick attribute. */
    _arg(v) { return App.escapeHtml(JSON.stringify(String(v ?? ''))); },

    _sent(key) {
        return this.SENTIMENTS[key] || { label: key || 'Reply', long: key || 'Reply', cls: 'sent-neutral', hint: '' };
    },

    _find(id) {
        if (id === null || id === undefined) return null;
        return (this.repliesData?.results || []).find(r => String(r.id) === String(id)) || null;
    },

    _activeIndex() {
        return this.pageIds().indexOf(String(this.activeId));
    },

    /** Remember where the open reply sits, so that if an action removes it
        from the list the neighbour that takes its place opens next. */
    _markPosition() {
        this._lastIndex = this._activeIndex();
    },

    get hasFilters() {
        return !!(this.query || this.sentiment || this.status || this.campaignId);
    },

    /** Attribution is defined by campaign_id, never by campaign_name alone:
        records written by older versions carry a fabricated
        "Cold Outreach Campaign" label for a reply that matched nothing, and
        a test mail must not look like it belonged to a campaign. The feed
        only contains campaign replies; automated replies carry no campaign
        by design and get no badge at all. */
    _campaign(r) {
        if (!r.campaign_id) return null;
        const label = r.campaign_name || `Campaign #${r.campaign_id}`;
        return { label, title: `Campaign: ${label}`, attributed: true };
    },

    _displayName(r) { return r.from_name || r.from_email || 'Unknown sender'; },

    _initial(r) {
        const first = Array.from(this._displayName(r).trim().replace(/^[^\p{L}\p{N}]+/u, ''))[0];
        return first ? first.toUpperCase() : '?';
    },

    _snippet(r) {
        return String(r.body_preview || r.full_body || '').replace(/\s+/g, ' ').trim().slice(0, 220);
    },

    /** "just now", "5m", "3h", "2d", then a short date. */
    relTime(iso) {
        const t = new Date(iso).getTime();
        if (!iso || Number.isNaN(t)) return '';
        const secs = Math.max(0, Math.round((Date.now() - t) / 1000));
        if (secs < 60) return 'just now';
        const mins = Math.floor(secs / 60);
        if (mins < 60) return `${mins}m`;
        const hrs = Math.floor(mins / 60);
        if (hrs < 24) return `${hrs}h`;
        const days = Math.floor(hrs / 24);
        if (days < 7) return `${days}d`;
        const d = new Date(t);
        const sameYear = d.getFullYear() === new Date().getFullYear();
        return d.toLocaleDateString(undefined, sameYear
            ? { month: 'short', day: 'numeric' }
            : { year: 'numeric', month: 'short', day: 'numeric' });
    },

    /** Replace a region's markup and give new controls their accessible names. */
    _set(id, html) {
        const el = document.getElementById(id);
        if (!el) return null;
        el.innerHTML = html;
        App.labelControls(el);
        return el;
    },

    /** Run a repaint and put keyboard focus back on the control that had it
        (the repaint replaces the element, which would drop focus to <body>). */
    _keepFocus(paint) {
        const fk = document.activeElement?.getAttribute?.('data-fk');
        paint();
        if (!fk) return;
        const next = Array.from(document.querySelectorAll('[data-fk]')).find(el => el.getAttribute('data-fk') === fk);
        if (next && next !== document.activeElement) next.focus({ preventScroll: true });
    },

    _isNarrow() {
        return window.matchMedia('(max-width: 900px)').matches;
    },

    // ── Page shell ───────────────────────────────────────

    /** Keep the view on an existing page. A bulk delete can strand it past
        the last page; clamp and re-fetch instead of painting a false
        "no replies" feed. Returns false when a re-fetch was started. */
    _pageInRange() {
        const total = this.repliesData?.total || 0;
        const clamped = App.clampPage(this.page, total, this.perPage);
        if (total > 0 && clamped !== this.page) {
            this.page = clamped;
            this.reload();
            return false;
        }
        return true;
    },

    /** Choose the open reply, register the bulk bar and mirror the URL. */
    _prepare() {
        const results = this.repliesData?.results || [];
        if (!results.some(r => String(r.id) === String(this.activeId))) {
            // The open reply left the page (deleted, re-filtered): take the
            // one that now sits where it was.
            const i = Math.min(Math.max(this._lastIndex ?? 0, 0), results.length - 1);
            this.activeId = results.length ? String(results[i].id) : null;
            this.reading = false;
        }
        this._lastIndex = undefined;

        App.selection.register('replies', {
            noun: 'reply',
            plural: 'replies',
            pageValues: this.pageIds(),
            actions: [
                { label: 'Mark Hot Lead', onclick: 'Replies.bulkStatus(\'lead\')' },
                { label: 'Mark Replied', onclick: 'Replies.bulkStatus(\'replied\')' },
                { label: 'Archive', onclick: 'Replies.bulkStatus(\'archived\')' },
                { label: 'Mark Unread', onclick: 'Replies.bulkStatus(\'unread\')' },
                { label: 'Blocklist', onclick: 'Replies.bulkBlocklist()' },
                { label: 'Delete', onclick: 'Replies.bulkDelete()', danger: true },
            ],
        });

        App.syncUrl('replies', {
            page: this.page, q: this.query, sentiment: this.sentiment,
            status: this.status, campaign: this.campaignId,
        });
    },

    renderPage() {
        if (this._seq !== undefined && App.stale(this._seq)) return;
        if (!this._pageInRange()) return;
        this._prepare();

        App.setContent(`
            <div class="reply-page ${this.reading ? 'is-reading' : ''}" id="repliesPage">
                <section class="card reply-hero" id="repliesHero" aria-label="Reply summary">${this.renderHero()}</section>

                <div class="card reply-toolbar">
                    <div class="search-input-wrap">
                        ${this.icon('search', 16)}
                        <input type="text" id="repliesSearchInput" class="form-control" placeholder="Search replies…" aria-label="Search sender, email, subject and message" title="Searches sender, email, subject and message" value="${this._esc(this.query)}"
                            oninput="Replies.onSearchInput(this.value)" onkeydown="if(event.key==='Enter')Replies.handleSearch(this.value, true)">
                    </div>
                    <div id="repliesFilters">${this.renderFilters()}</div>
                </div>

                <div id="repliesAuto">${this.renderAutomation()}</div>

                ${App.selection.bar('replies')}

                <div id="repliesBody">${this.renderBody()}</div>
                <div id="repliesPager">${this.renderPager()}</div>
            </div>
        `);
    },

    /** Repaint everything that depends on fetched data, in place. */
    refresh() {
        if (App.currentPage !== 'replies') return;
        if (this._seq !== undefined && App.stale(this._seq)) return;
        if (!document.getElementById('repliesPage')) { this.renderPage(); return; }
        if (!this._pageInRange()) return;
        this._prepare();

        const area = document.getElementById('contentArea');
        const top = area ? area.scrollTop : 0;
        this._keepFocus(() => {
            this._set('repliesHero', this.renderHero());
            this._set('repliesFilters', this.renderFilters());
            this._set('repliesAuto', this.renderAutomation());
            this._set('repliesBody', this.renderBody());
            this._set('repliesPager', this.renderPager());
        });
        this._syncReading();
        App.selection.sync('replies');
        if (area) area.scrollTop = top;
    },

    _paintFilters() {
        if (App.currentPage !== 'replies') return;
        this._keepFocus(() => this._set('repliesFilters', this.renderFilters()));
    },

    _paintAutomation() {
        if (App.currentPage !== 'replies') return;
        this._keepFocus(() => {
            this._set('repliesAuto', this.renderAutomation());
            this._set('repliesFilters', this.renderFilters());
        });
    },

    // ── Summary strip ────────────────────────────────────

    renderHero() {
        const s = this.stats || {};
        const total = s.total_replies || 0;
        const keys = ['positive', 'neutral', 'auto_reply', 'negative'];

        // One segment per non-empty sentiment, proportional to its count. The
        // automated segment is hatched: those replies are excluded from the
        // human reply rate, and the hatching says so without a legend.
        const segs = keys.filter(k => (s[k] || 0) > 0).map(k => {
            const meta = this._sent(k);
            return `<span class="reply-dist-seg ${meta.cls}" style="flex-grow:${s[k]}" title="${this._esc(meta.label)}: ${s[k]}"></span>`;
        }).join('');
        const summary = keys.map(k => `${this._sent(k).label} ${s[k] || 0}`).join(', ');

        const tab = (value, label, count, cls, hint) => {
            const on = this.sentiment === value;
            return `<button type="button" class="reply-tab ${cls} ${on ? 'is-active' : ''} ${count ? '' : 'is-zero'}" aria-pressed="${on}"
                title="${this._esc(hint)}" data-fk="sent-${value || 'all'}" onclick="Replies.filterSentiment('${value}')">
                <span class="reply-tab-dot"></span><span class="reply-tab-label">${this._esc(label)}</span><span class="reply-tab-count">${App.formatNumber(count)}</span>
            </button>`;
        };

        const camp = this.campaignId
            ? (s.campaign_options || []).find(c => c.id === this.campaignId)
            : null;
        const eyebrow = this.campaignId
            ? `Replies · ${camp ? camp.name : `Campaign #${this.campaignId}`}`
            : 'Prospect replies';

        return `
            <div class="reply-hero-figure">
                <div class="reply-eyebrow" title="${this._esc(eyebrow)}">${this._esc(eyebrow)}</div>
                <div class="reply-hero-total">
                    <span class="reply-hero-number">${App.formatNumber(total)}</span>
                    <span class="reply-hero-unit">${total === 1 ? 'reply' : 'replies'}</span>
                </div>
                <div class="reply-hero-caption">Human reply rate <strong>${s.reply_rate || 0}%</strong>
                    <span class="reply-hero-note">excludes ${App.formatNumber(s.auto_reply || 0)} automated</span></div>
            </div>
            <div class="reply-hero-body">
                <div class="reply-dist ${total ? '' : 'is-empty'}" role="img" aria-label="${this._esc(summary)}">${segs}</div>
                <div class="reply-tabs" role="group" aria-label="Filter replies by sentiment">
                    ${tab('', 'All', total, '', 'Every reply')}
                    ${keys.map(k => tab(k, this._sent(k).label, s[k] || 0, this._sent(k).cls, this._sent(k).hint)).join('')}
                </div>
            </div>
        `;
    },

    // ── Toolbar ──────────────────────────────────────────

    renderFilters() {
        const s = this.stats || {};
        const statusBtn = (value, label) => {
            const on = this.status === value;
            return `<button type="button" class="reply-seg-btn ${on ? 'is-active' : ''}" aria-pressed="${on}" data-fk="flt-${value || 'all'}"
                onclick="Replies.filterStatus('${value}')">${this._esc(label)}</button>`;
        };
        const autoOn = [this.settings.auto_blocklist_machine, this.settings.auto_blocklist_ooo, this.settings.auto_tag_leads]
            .filter(Boolean).length;

        return `
            <div class="reply-seg" role="group" aria-label="Filter replies by status">
                ${statusBtn('', 'All')}${statusBtn('unread', 'Unread')}${statusBtn('lead', 'Hot leads')}${statusBtn('replied', 'Replied')}${statusBtn('archived', 'Archived')}
            </div>

            <select class="form-control filter-select" id="campaignFilter" aria-label="Filter replies by campaign" onchange="Replies.filterCampaign(Number(this.value))">
                <option value="0" ${!this.campaignId ? 'selected' : ''}>All campaigns</option>
                ${(s.campaign_options || []).map(c => `<option value="${c.id}" ${this.campaignId === c.id ? 'selected' : ''}>${this._esc(c.name)} (${c.count})</option>`).join('')}
            </select>

            <button type="button" class="btn reply-auto-btn" data-fk="auto" aria-expanded="${this.autoOpen}" aria-controls="repliesAuto" onclick="Replies.toggleAutomation()"
                title="Hot-lead flagging, auto-blocklisting and how often the mailbox is scanned">
                ${this.icon('sliders')}<span>Automation</span>${autoOn ? `<span class="reply-count-chip" aria-label="${autoOn} enabled">${autoOn}</span>` : ''}
            </button>

            <button type="button" class="btn btn-primary reply-scan-btn" id="btnScanInbox" data-fk="scan" onclick="Replies.scanInbox()" ${this.isScanning ? 'disabled' : ''}>
                ${this.isScanning ? '<span class="spinner-sm"></span>' : this.icon('refresh')}
                <span>${this.isScanning ? 'Scanning…' : 'Scan Mailbox'}</span>
            </button>
        `;
    },

    toggleAutomation() {
        this.autoOpen = !this.autoOpen;
        this._paintAutomation();
    },

    renderAutomation() {
        if (!this.autoOpen) return '';
        const st = this.settings;
        const row = (checked, onchange, title, desc) => `
            <label class="switch-row ${checked ? 'on' : ''}">
                <span class="switch">
                    <input type="checkbox" ${checked ? 'checked' : ''} onchange="${onchange}">
                    <span class="slider"></span>
                </span>
                <span class="switch-text">
                    <span class="switch-title">${title}</span>
                    <span class="switch-desc">${desc}</span>
                </span>
            </label>`;
        const intervals = [5, 15, 30, 60, 240];
        if (!intervals.includes(st.scan_interval_minutes)) intervals.push(st.scan_interval_minutes);

        return `
            <section class="card reply-auto" aria-label="Mailbox automation">
                <div class="reply-auto-head">
                    <h3>Mailbox automation</h3>
                    <p>These rules run on every scan — in the background and when you press <strong>Scan Mailbox</strong>.</p>
                </div>
                <div class="reply-auto-grid">
                    ${row(st.auto_tag_leads, 'Replies.toggleAutoTagLeads(this.checked)',
                        'Auto-flag Hot Leads',
                        'Mark replies the classifier reads as interested as Hot Leads. Off: they stay Unread for manual triage.')}
                    ${row(st.auto_blocklist_machine, 'Replies.toggleAutoBlocklist(\'auto_blocklist_machine\', this.checked)',
                        'Auto-blocklist no-reply senders',
                        'Blocklist senders of machine auto-replies (noreply@, notifications@, ticket systems) when a scan detects them.')}
                    ${row(st.auto_blocklist_ooo, 'Replies.toggleAutoBlocklist(\'auto_blocklist_ooo\', this.checked)',
                        'Auto-blocklist out-of-office',
                        'Careful: these are real people who are temporarily away — they may be worth following up with later.')}
                    <div class="switch-row reply-interval">
                        <span class="switch-text">
                            <span class="switch-title">Scan interval</span>
                            <span class="switch-desc">How often the mailbox is scanned for new replies in the background.</span>
                        </span>
                        <select class="form-control filter-select" aria-label="Background mailbox scan interval" onchange="Replies.setScanInterval(Number(this.value))">
                            ${intervals.map(m => `<option value="${m}" ${st.scan_interval_minutes === m ? 'selected' : ''}>Every ${m < 60 ? m + ' min' : (m / 60) + ' h'}</option>`).join('')}
                        </select>
                    </div>
                </div>
            </section>
        `;
    },

    // ── List + reading pane ──────────────────────────────

    renderBody() {
        const replies = this.repliesData?.results || [];
        if (!replies.length) return this.renderEmpty();

        const total = this.repliesData?.total || replies.length;
        const from = (this.page - 1) * this.perPage + 1;
        const to = from + replies.length - 1;

        return `
            <div class="reply-layout ${this.reading ? 'is-reading' : ''}" id="repliesLayout">
                <section class="card reply-list" aria-label="Replies">
                    <div class="reply-list-head">
                        ${App.selection.headbox('replies', 'Select all replies on this page')}
                        <label class="reply-select-all" for="selectAll_replies">Select page</label>
                        <span class="reply-list-range">${App.formatNumber(from)}–${App.formatNumber(to)} of ${App.formatNumber(total)}</span>
                        ${this.hasFilters ? `<button type="button" class="reply-clear" onclick="Replies.clearFilters()">${this.icon('x', 12)} Clear filters</button>` : ''}
                        <span class="reply-kbd-hint" aria-hidden="true"><kbd>J</kbd><kbd>K</kbd> to move</span>
                    </div>
                    <div class="reply-items" id="repliesFeed">
                        ${replies.map(r => this.renderItem(r)).join('')}
                    </div>
                </section>
                <aside class="card reply-detail" id="replyDetail" aria-label="Selected reply">${this.renderDetail()}</aside>
            </div>
        `;
    },

    renderEmpty() {
        return `
            <div class="empty-state card reply-empty">
                <div class="reply-empty-icon">${this.icon('mail', 30)}</div>
                <h2>${this.hasFilters ? 'No replies match these filters' : 'No prospect replies yet'}</h2>
                <p>${this.hasFilters
                    ? 'Nothing in the log matches the current search and filters.'
                    : 'Replies show up here once leads answer your cold outreach. Scan the mailbox to check IMAP for new messages.'}</p>
                ${this.hasFilters
                    ? '<button type="button" class="btn" onclick="Replies.clearFilters()">Clear filters</button>'
                    : '<button type="button" class="btn btn-primary" onclick="Replies.scanInbox()">Scan Mailbox</button>'}
            </div>
        `;
    },

    renderItem(r) {
        const id = String(r.id);
        const sent = this._sent(r.sentiment);
        const camp = this._campaign(r);
        const active = id === String(this.activeId);
        // Automated mail stays "unread" forever; flagging it as new would
        // bury the replies that actually need a human.
        const unread = r.status === 'unread' && r.sentiment !== 'auto_reply';
        const snippet = this._snippet(r);
        const name = this._displayName(r);
        const classes = [
            'reply-item', sent.cls,
            active ? 'is-active' : '',
            unread ? 'is-unread' : '',
            r.status === 'lead' ? 'is-lead' : '',
            r.status === 'archived' ? 'is-archived' : '',
        ].filter(Boolean).join(' ');

        return `
            <article class="${classes}" id="reply_${this._esc(id)}" data-id="${this._esc(id)}">
                <div class="reply-item-check">${App.selection.rowbox('replies', id, `Select reply from ${r.from_email || 'sender'}`)}</div>
                <button type="button" class="reply-item-main" data-fk="item-${this._esc(id)}" ${active ? 'aria-current="true"' : ''} onclick="Replies.open(${this._arg(id)})">
                    <span class="reply-avatar"><span class="reply-avatar-letter">${this._esc(this._initial(r))}</span><span class="reply-avatar-dot" aria-hidden="true"></span></span>
                    <span class="reply-item-body">
                        <span class="reply-item-top">
                            <span class="reply-item-name">${unread ? '<span class="reply-unread-dot" title="Unread"></span><span class="sr-only">Unread. </span>' : ''}<span class="reply-item-name-text">${this._esc(name)}</span></span>
                            <time class="reply-item-time" datetime="${this._esc(r.date || '')}" title="${this._esc(App.formatDateTime(r.date))}">${this._esc(this.relTime(r.date))}</time>
                        </span>
                        <span class="reply-item-subject">${this._esc(r.subject || '(No subject)')}</span>
                        <span class="reply-item-snippet ${snippet ? '' : 'is-empty'}">${snippet ? this._esc(snippet) : '(Empty reply)'}</span>
                        <span class="reply-item-tags">
                            <span class="reply-tag reply-tag-sent">${this._esc(sent.label)}</span>
                            ${r.status === 'lead' ? `<span class="reply-tag reply-tag-lead">${this.icon('star', 10, true)} Hot lead</span>` : ''}
                            ${r.auto_blocklisted ? '<span class="reply-tag reply-tag-blocked" title="Sender was automatically blocklisted in ListMonk when this automated reply was detected">Auto-blocklisted</span>' : ''}
                            ${camp ? `<span class="reply-tag reply-tag-camp" title="${this._esc(camp.title)}">${this._esc(camp.label)}</span>` : ''}
                        </span>
                    </span>
                </button>
            </article>
        `;
    },

    renderDetail() {
        const r = this._find(this.activeId);
        if (!r) {
            return `<div class="reply-detail-empty">${this.icon('mail', 28)}<p>Select a reply to read it here.</p></div>`;
        }
        const id = String(r.id);
        const ids = this.pageIds();
        const idx = ids.indexOf(id);
        const sent = this._sent(r.sentiment);
        const camp = this._campaign(r);
        const body = String(r.full_body || r.body_preview || '').trim();

        const statusBtn = s => {
            const on = r.status === s.value;
            return `<button type="button" class="reply-seg-btn ${on ? 'is-active' : ''}" aria-pressed="${on}" data-fk="st-${s.value}"
                onclick="Replies.setStatus(${this._arg(id)}, '${s.value}')">${s.value === 'lead' ? this.icon('star', 12, on) : ''}${this._esc(s.label)}</button>`;
        };

        return `
            <div class="reply-detail-top">
                <div class="reply-detail-nav">
                    <button type="button" class="btn btn-sm reply-back" onclick="Replies.closeDetail()">${this.icon('left', 14)} Inbox</button>
                    <button type="button" class="reply-icon-btn" data-fk="nav-prev" aria-label="Previous reply" title="Previous (K)" ${idx <= 0 ? 'disabled' : ''} onclick="Replies.step(-1)">${this.icon('left', 16)}</button>
                    <span class="reply-detail-pos">${idx + 1} of ${ids.length}</span>
                    <button type="button" class="reply-icon-btn" data-fk="nav-next" aria-label="Next reply" title="Next (J)" ${idx < 0 || idx >= ids.length - 1 ? 'disabled' : ''} onclick="Replies.step(1)">${this.icon('right', 16)}</button>
                </div>
                <div class="reply-seg" role="group" aria-label="Reply status">${this.STATUSES.map(statusBtn).join('')}</div>
                <div class="reply-detail-actions">
                    <button type="button" class="reply-icon-btn" aria-label="Copy email address" title="Copy email address" onclick="Replies.copyEmail(${this._arg(r.from_email || '')})">${this.icon('copy', 15)}</button>
                    <button type="button" class="reply-icon-btn is-danger" aria-label="Blocklist sender" title="Blocklist this sender in ListMonk and unsubscribe them from every list" onclick="Replies.unsubscribeLead(${this._arg(id)})">${this.icon('ban', 15)}</button>
                    <button type="button" class="reply-icon-btn is-danger" aria-label="Delete reply" title="Delete this reply from the log" onclick="Replies.remove(${this._arg(id)})">${this.icon('trash', 15)}</button>
                </div>
            </div>

            <div class="reply-detail-scroll">
                <div class="reply-detail-from ${sent.cls}">
                    <span class="reply-avatar reply-avatar-lg"><span class="reply-avatar-letter">${this._esc(this._initial(r))}</span><span class="reply-avatar-dot" aria-hidden="true"></span></span>
                    <div class="reply-detail-who">
                        <div class="reply-detail-name">${this._esc(this._displayName(r))}</div>
                        <div class="reply-detail-email">${this._esc(r.from_email || '')}</div>
                    </div>
                </div>

                <div class="reply-detail-badges ${sent.cls}">
                    <span class="reply-tag reply-tag-sent reply-tag-lg">${this._esc(sent.long)}</span>
                    ${r.status === 'lead' ? `<span class="reply-tag reply-tag-lead reply-tag-lg">${this.icon('star', 11, true)} Hot lead</span>` : ''}
                    ${r.auto_blocklisted ? '<span class="reply-tag reply-tag-blocked reply-tag-lg" title="Sender was automatically blocklisted in ListMonk when this automated reply was detected">Auto-blocklisted</span>' : ''}
                </div>

                <h2 class="reply-detail-subject">${this._esc(r.subject || '(No subject)')}</h2>

                <dl class="reply-facts">
                    <div><dt>Received</dt><dd>${this._esc(App.formatDateTime(r.date))}</dd></div>
                    ${camp ? `<div><dt>Campaign</dt><dd title="${this._esc(camp.title)}">${this._esc(camp.label)}</dd></div>` : ''}
                    ${r.to_email ? `<div><dt>Sent to</dt><dd>${this._esc(r.to_email)}</dd></div>` : ''}
                </dl>

                <div class="reply-message ${body ? '' : 'is-empty'}">${body ? this._esc(body) : '(Empty reply)'}</div>
            </div>

        `;
    },

    renderPager() {
        const total = this.repliesData?.total || 0;
        return total > this.perPage ? App.renderPagination(this.page, total, this.perPage, 'Replies.goToPage') : '';
    },

    /** IDs (as strings) of the replies currently on screen. */
    pageIds() {
        return (this.repliesData?.results || []).map(r => String(r.id));
    },

    _selectedIds() {
        return App.selection.values('replies');
    },

    // ── Reading pane ─────────────────────────────────────

    /** Open a reply; on narrow screens this swaps the list for the reading pane. */
    open(id) {
        id = String(id);
        if (!this._find(id)) return;
        this.activeId = id;
        if (this._isNarrow()) {
            const area = document.getElementById('contentArea');
            this._listScroll = area ? area.scrollTop : 0;
            this.reading = true;
        }
        this._paintActive();
        if (this._isNarrow()) {
            const area = document.getElementById('contentArea');
            if (area) area.scrollTop = 0;
        }
    },

    closeDetail() {
        this.reading = false;
        this._syncReading();
        const area = document.getElementById('contentArea');
        if (area && this._listScroll !== undefined) area.scrollTop = this._listScroll;
        // Put focus back on the row the user came from.
        const row = Array.from(document.querySelectorAll('.reply-item-main'))
            .find(el => el.getAttribute('data-fk') === `item-${this.activeId}`);
        if (row) row.focus({ preventScroll: true });
    },

    /** Move the reading pane to the previous / next reply on this page. */
    step(delta) {
        const ids = this.pageIds();
        if (!ids.length) return;
        const i = ids.indexOf(String(this.activeId));
        const next = ids[Math.min(Math.max(i < 0 ? 0 : i + delta, 0), ids.length - 1)];
        if (next === String(this.activeId)) return;
        this.activeId = next;
        this._paintActive();
        const row = Array.from(document.querySelectorAll('#repliesFeed .reply-item')).find(el => el.dataset.id === next);
        if (row) row.scrollIntoView({ block: 'nearest' });
    },

    /** Mirror `reading` onto the DOM: the layout swaps list ↔ pane, and the
        page hides its summary/toolbar so a reply opens at the top of the screen
        (all of this only has an effect below 900px). */
    _syncReading() {
        document.getElementById('repliesLayout')?.classList.toggle('is-reading', this.reading);
        document.getElementById('repliesPage')?.classList.toggle('is-reading', this.reading);
    },

    /** Move the highlight and rebuild only the reading pane. */
    _paintActive() {
        document.querySelectorAll('#repliesFeed .reply-item').forEach(el => {
            const on = el.dataset.id === String(this.activeId);
            el.classList.toggle('is-active', on);
            const btn = el.querySelector('.reply-item-main');
            if (btn) { if (on) btn.setAttribute('aria-current', 'true'); else btn.removeAttribute('aria-current'); }
        });
        this._syncReading();
        this._keepFocus(() => this._set('replyDetail', this.renderDetail()));
        const pane = document.querySelector('#replyDetail .reply-detail-scroll');
        if (pane) pane.scrollTop = 0;
    },

    /** J / K or the arrow keys walk the list; Esc leaves the reading pane on
        narrow screens. Installed once; inert on every other page. */
    _bindKeys() {
        if (this._keysBound) return;
        this._keysBound = true;
        document.addEventListener('keydown', e => {
            if (App.currentPage !== 'replies') return;
            if (e.defaultPrevented || e.metaKey || e.ctrlKey || e.altKey) return;
            const overlay = document.getElementById('modalOverlay');
            if (overlay && overlay.style.display === 'flex') return;
            const typing = e.target?.closest?.('input:not([type="checkbox"]):not([type="radio"]), select, textarea, [contenteditable="true"]');
            if (typing) return;
            if (e.key === 'Escape' && this.reading) { this.closeDetail(); return; }
            if (e.key === 'j' || e.key === 'ArrowDown') { e.preventDefault(); this.step(1); }
            else if (e.key === 'k' || e.key === 'ArrowUp') { e.preventDefault(); this.step(-1); }
        });
    },

    // ── Bulk actions ─────────────────────────────────────
    async bulkStatus(newStatus) {
        const ids = this._selectedIds();
        if (!ids.length) return;
        try {
            const result = await API.put('/api/replies/status', { ids, status: newStatus });
            App.toast(`${result?.updated ?? ids.length} reply(ies) marked ${newStatus}`, 'success');
            App.selection.clear('replies');
            this._markPosition();
            await Promise.all([this.loadStats(), this.loadReplies()]);
            this.refresh();
        } catch {
            // API.request already surfaced the server message.
        }
    },

    async bulkBlocklist() {
        const ids = this._selectedIds();
        if (!ids.length) return;
        if (!await App.confirm('Blocklist Senders',
                `Blocklist the senders of ${ids.length} selected repl(y/ies)? They will never receive further outreach.`)) return;
        try {
            const result = await API.post('/api/replies/blocklist', { ids }, API.LONG_TIMEOUT);
            const errors = result?.errors || 0;
            App.toast(`Blocklisted ${result?.blocklisted ?? ids.length} sender(s)${errors ? `, ${errors} failed` : ''}`,
                errors ? 'info' : 'success');
            App.selection.clear('replies');
            this._markPosition();
            this.reload();
        } catch {
            // API.request already surfaced the server message.
        }
    },

    async bulkDelete() {
        const ids = this._selectedIds();
        if (!ids.length) return;
        if (!await App.confirm('Delete Replies',
                `Delete ${ids.length} selected reply record(s) from the log?`)) return;
        try {
            const params = ids.map(id => `ids=${encodeURIComponent(id)}`).join('&');
            await API.del(`/api/replies?${params}`);
            App.toast(`${ids.length} reply(ies) deleted`, 'success');
            App.selection.clear('replies');
            this._markPosition();
            await Promise.all([this.loadStats(), this.loadReplies()]);
            this.refresh();
        } catch {
            // API.request already surfaced the server message.
        }
    },

    async remove(id) {
        if (!await App.confirm('Delete Reply', 'Delete this reply record from the log?')) return;
        try {
            await API.del(`/api/replies/${encodeURIComponent(id)}`);
            App.toast('Reply deleted', 'success');
            App.selection.clear('replies');
            this._markPosition();
            await Promise.all([this.loadStats(), this.loadReplies()]);
            this.refresh();
        } catch {
            // API.request already surfaced the server message.
        }
    },

    async scanInbox() {
        if (this.isScanning) return;
        this.isScanning = true;
        this._paintFilters();

        try {
            const res = await API.post('/api/replies/scan', {}, API.LONG_TIMEOUT);
            const stats = res.stats || {};
            // Safety net: the endpoint now answers 502 on IMAP failure, but a
            // 200 carrying an error must never read as a successful scan.
            if (stats.error) {
                App.toast(`Scan failed: ${stats.error}`, 'error');
            } else {
                const count = stats.new_replies || 0;
                const repaired = stats.repaired || 0;
                let msg = `Scan complete! Found ${count} new prospect reply(ies).`;
                if (repaired) {
                    msg += ` Recovered the body of ${repaired} empty reply(ies).`;
                }
                App.toast(msg, (count > 0 || repaired > 0) ? 'success' : 'info');
            }
            await Promise.all([this.loadStats(), this.loadReplies()]);
        } catch (err) {
            console.error('Scan error:', err);
            // API.request already surfaced the server message.
        } finally {
            // Restore the button even when the scan failed, so it does not
            // stay stuck on "Scanning Mailbox…" forever.
            this.isScanning = false;
            if (document.getElementById('repliesPage')) this.refresh();
            else this._paintFilters();
        }
    },

    /** Set one reply's status from the reading pane's switch. */
    async setStatus(id, newStatus) {
        const r = this._find(id);
        if (!r || r.status === newStatus) return;
        const label = (this.STATUSES.find(s => s.value === newStatus) || {}).label || newStatus;
        try {
            await API.put(`/api/replies/${encodeURIComponent(id)}/status`, { status: newStatus });
            App.toast(`Status updated to ${label}`, 'success');
            this._markPosition();
            // Reload rather than patching locally: with a status filter on, the
            // reply may no longer belong in the list.
            await Promise.all([this.loadStats(), this.loadReplies()]);
            this.refresh();
        } catch {
            // API.request already surfaced the server message; reload so the
            // switch reflects the state the server actually stored.
            await this.reload();
        }
    },

    copyEmail(email) {
        const done = () => App.toast(`Copied ${email} to clipboard`, 'success');
        const fail = () => App.toast(`Could not copy email`, 'error');

        if (navigator.clipboard?.writeText) {
            navigator.clipboard.writeText(email).then(done).catch(() => {
                // Clipboard API exists but was refused (insecure context,
                // missing permission): fall back to the legacy path.
                this._legacyCopy(email) ? done() : fail();
            });
            return;
        }
        this._legacyCopy(email) ? done() : fail();
    },

    /** Pre-Clipboard-API copy path (works on plain-HTTP dev instances). */
    _legacyCopy(text) {
        const ta = document.createElement('textarea');
        ta.value = text;
        ta.setAttribute('readonly', '');
        ta.style.position = 'fixed';
        ta.style.top = '-1000px';
        document.body.appendChild(ta);
        try {
            ta.select();
            return document.execCommand('copy');
        } catch {
            return false;
        } finally {
            document.body.removeChild(ta);
        }
    },

    async unsubscribeLead(id) {
        const reply = this._find(id);
        const email = reply?.from_email || 'this sender';
        const ok = await App.confirm(
            'Blocklist & Unsubscribe',
            `Are you sure you want to blocklist and unsubscribe ${email}? They will never receive any further outreach.`
        );
        if (!ok) return;

        try {
            // Resolved server-side so the sender address never reaches a
            // client-built ListMonk query.
            const result = await API.post(`/api/replies/${encodeURIComponent(id)}/blocklist`, {});
            App.toast(`${result?.email || email} successfully blocklisted.`, 'success');
        } catch {
            // API.request already surfaced the server message.
        }
    },

    // ── Filters ──────────────────────────────────────────

    /** Typing searches after a short pause; Enter searches immediately. */
    onSearchInput(val) {
        clearTimeout(this._searchTimer);
        this._searchTimer = setTimeout(() => this.handleSearch(val), 350);
    },

    handleSearch(val, force = false) {
        clearTimeout(this._searchTimer);
        const q = String(val || '').trim();
        if (q === this.query && !force) return;
        this.query = q;
        this._resetView();
    },

    filterSentiment(val) {
        // Pressing the active tab again clears the filter.
        this.sentiment = this.sentiment === val ? '' : val;
        this._resetView();
    },

    filterStatus(val) {
        this.status = this.status === val ? '' : val;
        this._resetView();
    },

    filterCampaign(val) {
        this.campaignId = val || 0;
        this._resetView();
    },

    clearFilters() {
        this.query = '';
        this.sentiment = '';
        this.status = '';
        this.campaignId = 0;
        const input = document.getElementById('repliesSearchInput');
        if (input) input.value = '';
        this._resetView();
    },

    /** A new filter state starts from the first page with nothing open. */
    _resetView() {
        this.page = 1;
        this.reading = false;
        this._lastIndex = 0;
        App.bumpSeq();
        this.reload();
    },

    goToPage(p) {
        this.page = p;
        this.reading = false;
        this._lastIndex = 0;
        App.bumpSeq();
        this.reload();
        const area = document.getElementById('contentArea');
        if (area) area.scrollTop = 0;
    }
};
