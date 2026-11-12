/**
 * Main SPA router and shared UI components.
 */
const App = {
    currentPage: null,
    currentMode: 'newsletter',

    pages: {
        'dashboard': { title: 'Dashboard', render: () => Dashboard.render() },
        'subscribers': { title: 'Subscribers', coldTitle: 'Leads', render: () => Subscribers.render() },
        'lists': { title: 'Lists', coldTitle: 'Lead Lists', render: () => Lists.render() },
        'campaigns': { title: 'Campaigns', coldTitle: 'Cold Outreach', render: () => Campaigns.render() },
        'analytics': { title: 'Analytics', render: () => Analytics.render() },
        'settings': { title: 'Settings', render: () => Settings.render() },
        'templates': { title: 'Templates', render: () => Templates.render() },
        'bounces': { title: 'Bounces', render: () => Bounces.render() },
        'converter': { title: 'CSV Converter', render: () => Converter.render() },
        'unsubscribes': { title: 'Unsubscribes', render: () => Unsubscribes.render() },
        'replies': { title: 'Replies & Leads', render: () => Replies.render() },
        'deliverability': { title: 'Deliverability & Safety', render: () => Deliverability.render() },
        'validation': { title: 'Email Validation', render: () => Validation.render() },
    },

    init() {
        // Guard against a second call (e.g. a future hot-reload or a duplicated
        // bootstrap script): every listener below is added to a long-lived
        // target, so a repeat run would stack duplicate handlers.
        if (this._initialized) return;
        this._initialized = true;

        window.addEventListener('hashchange', () => this.route());

        // The nav list scrolls when the screen is too short for it (phones,
        // and short laptop screens); without a hint the clipped last entry
        // reads as a rendering bug instead of "scroll for more".
        const navMenu = document.querySelector('.nav-menu');
        const navMq = window.matchMedia('(max-width: 768px)');
        const syncNavHint = () => {
            if (!navMenu) return;
            const scrollable = navMenu.scrollHeight > navMenu.clientHeight + 1;
            navMenu.classList.toggle('is-scrollable', scrollable);
            navMenu.classList.toggle(
                'at-end',
                !scrollable || navMenu.scrollTop + navMenu.clientHeight >= navMenu.scrollHeight - 2
            );
        };
        if (navMenu) {
            navMenu.addEventListener('scroll', syncNavHint, { passive: true });
            window.addEventListener('resize', syncNavHint);
            navMq.addEventListener?.('change', syncNavHint);
        }

        document.getElementById('menuToggle').addEventListener('click', () => {
            const sidebar = document.getElementById('sidebar');
            sidebar.classList.toggle('open');
            // Drives the scrim behind the open drawer (see .nav-open in CSS).
            const open = sidebar.classList.contains('open');
            document.body.classList.toggle('nav-open', open);
            document.getElementById('menuToggle').setAttribute('aria-expanded', open ? 'true' : 'false');
            syncNavHint();
        });
        syncNavHint();

        // WAI-ARIA radio group: Arrow keys move between the two mode pills and
        // select as they go. Without this the group only responded to Tab, so
        // keyboard users could not switch modes the way the role promises.
        const modeGroup = document.getElementById('modeSegmented');
        if (modeGroup) {
            modeGroup.addEventListener('keydown', (e) => {
                const radios = Array.from(modeGroup.querySelectorAll('[role="radio"]'));
                if (!radios.length) return;
                const current = radios.indexOf(document.activeElement);
                let next = null;
                if (e.key === 'ArrowRight' || e.key === 'ArrowDown') {
                    next = current < 0 ? 0 : (current + 1) % radios.length;
                } else if (e.key === 'ArrowLeft' || e.key === 'ArrowUp') {
                    next = current < 0 ? radios.length - 1 : (current - 1 + radios.length) % radios.length;
                } else if (e.key === 'Home') {
                    next = 0;
                } else if (e.key === 'End') {
                    next = radios.length - 1;
                }
                if (next === null) return;
                e.preventDefault();
                const target = radios[next];
                target.focus();
                const mode = target.dataset.mode;
                if (mode && target.getAttribute('aria-checked') !== 'true') {
                    this.setMode(mode);
                }
            });
        }

        // One "⋯" menu open at a time: a click anywhere else closes it.
        document.addEventListener('click', (e) => {
            document.querySelectorAll('.more-menu[open]').forEach(d => {
                if (!d.contains(e.target) && !(d._pop && d._pop.contains(e.target))) d.open = false;
            });
        });
        // While open, the popover is moved to <body> and placed from the
        // button's rect: inside the table it was clipped by the scrolling
        // .table-wrapper, and glass cards (backdrop-filter) turn
        // position:fixed into position:absolute for their descendants.
        document.addEventListener('toggle', (e) => {
            const d = e.target;
            if (!(d instanceof HTMLElement) || !d.classList.contains('more-menu')) return;
            const pop = d._pop || d.querySelector('.more-menu-pop');
            const btn = d.querySelector('summary');
            if (!pop || !btn) return;
            d._pop = pop;
            if (!d.open) { pop.remove(); return; }
            document.querySelectorAll('.more-menu[open]').forEach(o => { if (o !== d) o.open = false; });
            document.body.appendChild(pop);
            const r = btn.getBoundingClientRect();
            pop.style.visibility = 'hidden';
            pop.style.top = '0px'; pop.style.left = '0px';
            const w = pop.offsetWidth, h = pop.offsetHeight;
            let left = d.classList.contains('more-menu-left') ? r.left : r.right - w;
            left = Math.max(8, Math.min(left, window.innerWidth - w - 8));
            let top = r.bottom + 4;
            if (top + h > window.innerHeight - 8) top = Math.max(8, r.top - h - 4);
            pop.style.left = left + 'px';
            pop.style.top = top + 'px';
            pop.style.visibility = '';
            pop.querySelector('.more-menu-item')?.focus();
        }, true);
        // A scrolled page would leave the fixed popover floating off its button.
        document.getElementById('contentArea')?.addEventListener('scroll', () => {
            document.querySelectorAll('.more-menu[open]').forEach(d => { d.open = false; });
        }, { passive: true });
        document.addEventListener('keydown', (e) => {
            const open = document.querySelector('.more-menu[open]');
            if (!open) return;
            if (e.key === 'Escape') {
                open.open = false;
                open.querySelector('summary')?.focus();
                return;
            }
            // Arrow keys move through the items; Tab leaves the menu.
            if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
                const items = Array.from(open._pop?.querySelectorAll('.more-menu-item') || []);
                if (!items.length) return;
                e.preventDefault();
                const i = items.indexOf(document.activeElement);
                const next = e.key === 'ArrowDown' ? (i + 1) % items.length : (i - 1 + items.length) % items.length;
                items[next].focus();
            } else if (e.key === 'Tab') {
                open.open = false;
            }
        });

        // Close sidebar on content click (mobile)
        document.querySelector('.content-area').addEventListener('click', () => {
            document.getElementById('sidebar').classList.remove('open');
            document.body.classList.remove('nav-open');
        });

        // Modal handlers
        document.getElementById('modalClose').addEventListener('click', () => this.closeModal());
        document.getElementById('modalCancel').addEventListener('click', () => this.closeModal());
        document.getElementById('modalOverlay').addEventListener('click', (e) => {
            if (e.target === e.currentTarget && !this._progressOpen()) this.closeModal();
        });

        // Dialog behavior for the shared overlay: Escape cancels, and Tab
        // cycles inside the modal instead of walking the page behind it.
        document.addEventListener('keydown', (e) => {
            // The mandatory mode picker: Escape must not skip the choice and
            // Tab must not walk the page behind its scrim.
            const onboarding = document.getElementById('modeOnboardingModal');
            if (onboarding && onboarding.style.display !== 'none') {
                if (e.key === 'Escape') { e.preventDefault(); return; }
                if (e.key === 'Tab') this._trapTab(onboarding, e);
                return;
            }
            const overlay = document.getElementById('modalOverlay');
            if (!overlay || overlay.style.display === 'none') return;
            if (e.key === 'Escape') {
                // A running bulk operation cannot be undone by hiding its
                // progress dialog; Escape is ignored until it finishes.
                if (this._progressOpen()) { e.preventDefault(); return; }
                e.preventDefault();
                this.closeModal();
                return;
            }
            if (e.key === 'Tab') this._trapTab(overlay, e);
        });

        // Initialize mode. localStorage is only an instant-render hint for
        // this browser: the server-persisted choice decides whether the
        // onboarding modal still needs to show, so "pick a mode" appears once
        // per installation instead of once per browser / cleared cookie jar.
        const savedMode = localStorage.getItem('listmonk_pro_mode');
        this.setMode(savedMode || 'newsletter', false);
        this.reconcileMode();

        this.checkConnection();
        this.route();
    },

    // Ask the server whether this installation has already picked a mode.
    // localStorage alone would re-ask on every new browser, device, private
    // window, or after the user clears site data to log out.
    async reconcileMode() {
        try {
            const state = await API.get('/api/app-settings');
            if (state.mode_selected) {
                localStorage.setItem('listmonk_pro_mode', state.mode);
                this.setMode(state.mode, false);
            } else {
                this.showModeOnboarding();
            }
        } catch {
            // API.request already toasted the error; fall back to the
            // localStorage-only behavior so the dashboard stays usable.
            if (!localStorage.getItem('listmonk_pro_mode')) {
                this.showModeOnboarding();
            }
        }
    },

    showModeOnboarding() {
        const modal = document.getElementById('modeOnboardingModal');
        if (modal) {
            modal.style.display = 'flex';
            // Move focus into the dialog so it is operable without a mouse.
            const first = modal.querySelector('button, [href], input');
            if (first && first.focus) first.focus();
        }
    },

    selectInitialMode(mode) {
        const modal = document.getElementById('modeOnboardingModal');
        if (modal) modal.style.display = 'none';
        this.setMode(mode, true);
        this.toast(`Welcome to ListMonk Pro! Activated ${mode === 'cold_email' ? 'Cold Email' : 'Newsletter'} mode.`, 'success');
    },

    setMode(mode, save = true) {
        // reconcileMode() re-applies the server's (usually identical) choice
        // right after the first paint; re-routing then rendered the whole
        // page twice on every load.
        const changed = mode !== this.currentMode || !this._modeApplied;
        this._modeApplied = true;
        this.currentMode = mode;
        if (save) {
            localStorage.setItem('listmonk_pro_mode', mode);
            // Mirror the choice server-side so other browsers and devices
            // (and any later login after cleared site data) skip onboarding.
            // API.request already toasts failures; a rejected promise here
            // must still be caught to stay unhandled-free.
            API.put('/api/app-settings', { mode }).catch(() => {});
        }
        document.documentElement.setAttribute('data-mode', mode);

        // Update mode switcher buttons
        document.querySelectorAll('.mode-pill-btn').forEach(btn => {
            const active = btn.dataset.mode === mode;
            btn.classList.toggle('active', active);
            btn.setAttribute('aria-checked', active ? 'true' : 'false');
            // Roving tabindex (WAI-ARIA radio group): Tab enters the group
            // once, on the checked option; arrows move within it.
            btn.setAttribute('tabindex', active ? '0' : '-1');
        });

        const isCold = mode === 'cold_email';

        // Dynamic sidebar labels
        const subsLabel = document.querySelector('.nav-link[data-page="subscribers"] span');
        if (subsLabel) subsLabel.textContent = isCold ? 'Leads' : 'Subscribers';

        const listsLabel = document.querySelector('.nav-link[data-page="lists"] span');
        if (listsLabel) listsLabel.textContent = isCold ? 'Lead Lists' : 'Lists';

        const campLabel = document.querySelector('.nav-link[data-page="campaigns"] span');
        if (campLabel) campLabel.textContent = isCold ? 'Cold Outreach' : 'Campaigns';

        // Dynamic visibility for mode-specific nav items
        document.querySelectorAll('.nav-item-cold').forEach(el => {
            el.style.display = isCold ? '' : 'none';
        });
        document.querySelectorAll('.nav-item-newsletter').forEach(el => {
            el.style.display = isCold ? 'none' : '';
        });

        // A category heading only makes sense while it still has visible items
        // (Newsletter mode has no Replies/Deliverability page, so that group
        // collapses to a single Bounces entry and keeps its heading).
        document.querySelectorAll('.nav-section-title').forEach(title => {
            const group = title.dataset.navGroup;
            if (!group) return;
            const siblings = document.querySelectorAll(
                `.nav-menu li[data-nav-group="${group}"]:not(.nav-section-title)`
            );
            const anyVisible = Array.from(siblings).some(li => li.style.display !== 'none');
            title.style.display = anyVisible ? '' : 'none';
        });

        // Re-render current page
        if (this.currentPage && changed) {
            this.route();
        }
    },

    /** Navigation generation counter. Every route() bumps it; async page
        renders capture App.seq() before awaiting and bail via App.stale(seq)
        before painting, so a slow page A can never overwrite page B. */
    _navSeq: 0,
    seq() { return this._navSeq; },
    stale(seq) { return seq !== this._navSeq; },

    /** Bump the render generation for in-page state changes (filter, search,
        pagination) so a slow response for the previous state can never repaint
        over the newer one. route() bumps for navigation; this is the in-page
        counterpart. */
    bumpSeq() { this._navSeq++; return this._navSeq; },

    /** Query params carried by the current hash (#/page?key=value). */
    routeQuery() { return this._routeQuery || new URLSearchParams(); },

    /** Rewrite the hash without adding a history entry or firing hashchange,
        so the URL mirrors page state without re-triggering route(). */
    syncUrl(page, params = {}) {
        const qs = new URLSearchParams();
        Object.entries(params).forEach(([k, v]) => {
            if (v !== undefined && v !== null && v !== '' && v !== 0 && v !== false) {
                qs.set(k, String(v));
            }
        });
        const url = '#/' + page + (qs.toString() ? '?' + qs.toString() : '');
        if (window.location.hash !== url) history.replaceState(null, '', url);
    },

    /** Destroy every live Chart.js instance. Charts are bound to canvases
        that route() is about to discard; Chart.js keeps them in its global
        registry, so without this each SPA navigation leaks one set. */
    destroyCharts() {
        try {
            if (window.Chart && Chart.instances) {
                Object.values(Chart.instances).forEach(c => {
                    try { c.destroy(); } catch { /* already destroyed */ }
                });
            }
        } catch { /* Chart not loaded yet */ }
    },

    route() {
        const raw = window.location.hash.slice(2) || 'dashboard';
        // Filters/pagination/detail views ride in the hash query so every
        // stateful view is deep-linkable and survives reload + Back.
        const [name, qs] = raw.split('?');
        const hash = name;
        this._routeQuery = new URLSearchParams(qs || '');
        // Own-property check: a plain lookup also resolves inherited keys, so
        // "#/constructor" or "#/toString" would return a function and blow up
        // with a confusing TypeError instead of redirecting to the dashboard.
        const page = Object.hasOwn(this.pages, hash) ? this.pages[hash] : null;

        if (!page || typeof page.render !== 'function') {
            window.location.hash = '#/dashboard';
            return;
        }

        this.currentPage = hash;
        const seq = ++this._navSeq;

        // A selection only makes sense on the page it was made on; the new
        // page re-registers its own bulk config while rendering.
        if (!this._renderingPage || this._renderingPage !== hash) {
            this.selection.clearAll();
            this._renderingPage = hash;
        }

        // Clear per-page state when navigating away
        if (hash !== 'bounces' && typeof Bounces !== 'undefined') {
            Bounces.campaigns = [];
            Bounces.campaignFilter = 0;
            Bounces.page = 1;
        }
        // Same convention for the unsubscribe detail view: its selection and
        // open campaign are page-local and must not survive navigation.
        if (hash !== 'unsubscribes' && typeof Unsubscribes !== 'undefined') {
            Unsubscribes.activeCampaign = null;
            Unsubscribes.selectedEmails.clear();
        }

        // The validation page polls a running scan; that timer must not outlive it.
        if (hash !== 'validation' && typeof Validation !== 'undefined') {
            Validation.stopPoll();
        }

        // A dialog belongs to the page that opened it: Back/Forward or a deep
        // link used to leave a result or confirm dialog covering the new page
        // (and a pending confirm still resolvable against stale state).
        // Running-operation progress dialogs stay until they finish.
        const overlay = document.getElementById('modalOverlay');
        if (overlay && overlay.style.display !== 'none' && !this._progressOpen()) {
            this.closeModal();
        }

        // Release charts bound to the outgoing page's canvases
        this.destroyCharts();
        this.stopAutoRefresh();
        this.closeMenus();

        // Update nav
        document.querySelectorAll('.nav-link').forEach(link => {
            const active = link.dataset.page === hash;
            link.classList.toggle('active', active);
            if (active) link.setAttribute('aria-current', 'page');
            else link.removeAttribute('aria-current');
        });

        // Update title: the same mode-aware wording as the sidebar entry.
        document.getElementById('pageTitle').textContent = this.pageTitle(hash);

        // Render page
        const content = document.getElementById('contentArea');
        content.innerHTML = '<div class="loading-spinner">Loading…</div>';
        try {
            const res = page.render();
            if (res && typeof res.catch === 'function') {
                res.catch(err => {
                    console.error(`Error rendering page ${hash}:`, err);
                    // currentPage (not the render generation): in-page seq
                    // bumps must not suppress this navigation-level error UI.
                    if (this.currentPage !== hash) return;
                    content.innerHTML = `<div class="empty-state"><h2>Failed to load ${App.escapeHtml(this.pageTitle(hash))}</h2><p>${App.escapeHtml(err?.message || '')}</p></div>`;
                });
            }
        } catch (err) {
            console.error(`Error rendering page ${hash}:`, err);
            if (this.currentPage !== hash) return;
            content.innerHTML = `<div class="empty-state"><h2>Failed to load ${App.escapeHtml(this.pageTitle(hash))}</h2><p>${App.escapeHtml(err?.message || '')}</p></div>`;
        }
    },

    /** Page heading for the current mode (Cold Email renames three pages). */
    pageTitle(hash) {
        const page = this.pages[hash];
        if (!page) return '';
        return (this.isCold() && page.coldTitle) || page.title;
    },

    isCold() { return this.currentMode === 'cold_email'; },

    /** Mode-aware nouns, so copy matches the sidebar: "lead" in Cold Email
        mode, "subscriber" in Newsletter mode. `plural` and `cap` select the
        form ("Leads", "subscribers"). */
    term(word, { plural = false, cap = false } = {}) {
        const cold = this.isCold();
        const forms = {
            subscriber: cold ? ['lead', 'leads'] : ['subscriber', 'subscribers'],
            list: cold ? ['lead list', 'lead lists'] : ['list', 'lists'],
        }[word] || [word, word + 's'];
        const out = forms[plural ? 1 : 0];
        return cap ? out.charAt(0).toUpperCase() + out.slice(1) : out;
    },

    /** Debounce helper for search-as-you-type boxes. */
    debounce(fn, ms = 350) {
        let t = null;
        return (...args) => { clearTimeout(t); t = setTimeout(() => fn(...args), ms); };
    },

    /** "⋯" overflow menu for secondary / dangerous actions. `items` are
        {label, onclick, danger?, hidden?}; hidden ones are skipped. Built on
        <details>, so it opens with the keyboard too; a document click closes
        any other open menu (see init). */
    moreMenu(items, label = 'More actions', { align = 'right', text = '' } = {}) {
        const rows = items.filter(i => i && !i.hidden).map(i =>
            `<button type="button" role="menuitem" class="more-menu-item${i.danger ? ' is-danger' : ''}"
                onclick="App.closeMenus();${App.escapeHtml(i.onclick)}">${App.escapeHtml(i.label)}</button>`
        ).join('');
        if (!rows) return '';
        return `<details class="more-menu more-menu-${align}">
            <summary class="btn btn-sm more-menu-btn" aria-label="${App.escapeHtml(label)}" title="${App.escapeHtml(label)}">${text ? App.escapeHtml(text) + ' ' : ''}<span aria-hidden="true">⋯</span></summary>
            <div class="more-menu-pop" role="menu">${rows}</div>
        </details>`;
    },

    /** Close every open "⋯" menu (their popovers live on <body> while open). */
    closeMenus() {
        document.querySelectorAll('.more-menu[open]').forEach(d => { d.open = false; });
        document.querySelectorAll('body > .more-menu-pop').forEach(p => p.remove());
    },

    /** Empty-state overlay for a chart card: a bare 0–1 grid or a grey ring
        reads as "broken"; say what is missing and where to go instead. */
    emptyChart(message, linkHref = '', linkText = '') {
        return `<div class="chart-empty">
            <svg aria-hidden="true" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" width="28" height="28"><line x1="18" y1="20" x2="18" y2="10"/><line x1="12" y1="20" x2="12" y2="4"/><line x1="6" y1="20" x2="6" y2="14"/></svg>
            <p>${App.escapeHtml(message)}</p>
            ${linkHref ? `<a href="${App.escapeHtml(linkHref)}">${App.escapeHtml(linkText)} →</a>` : ''}
        </div>`;
    },

    // ── Auto refresh ─────────────────────────────────────
    /** One page-level refresh timer. A page calls App.autoRefresh(page, fn, ms)
        while it shows live data (a running campaign); route() clears it, and it
        skips ticks while the tab is hidden or a dialog is open. */
    _refreshTimer: null,
    autoRefresh(page, fn, ms = 15000) {
        this.stopAutoRefresh();
        this._refreshTimer = setInterval(() => {
            if (this.currentPage !== page) { this.stopAutoRefresh(); return; }
            if (document.hidden) return;
            const overlay = document.getElementById('modalOverlay');
            if (overlay && overlay.style.display !== 'none') return;
            // Never repaint over something the user is typing into.
            const a = document.activeElement;
            if (a && /^(INPUT|TEXTAREA|SELECT)$/.test(a.tagName) && a.type !== 'checkbox') return;
            if (document.querySelector('.more-menu[open]')) return;
            try { fn(); } catch (e) { console.error(e); }
        }, ms);
    },
    stopAutoRefresh() {
        if (this._refreshTimer) clearInterval(this._refreshTimer);
        this._refreshTimer = null;
    },

    async checkConnection() {
        const dot = document.querySelector('.status-dot');
        const text = document.querySelector('.status-text');
        try {
            // silent: the status dot IS the failure UI; a toast per poll
            // during an outage would stack one per check on top of it.
            await API.get('/api/lists?per_page=1&minimal=true', true);
            dot.className = 'status-dot connected';
            text.textContent = 'Connected';
        } catch {
            dot.className = 'status-dot error';
            text.textContent = 'Disconnected';
        }
    },

    // ── Toast ────────────────────────────────────────────
    toast(message, type = 'info') {
        const container = document.getElementById('toastContainer');
        const toast = document.createElement('div');
        toast.className = `toast toast-${type}`;
        toast.textContent = message;
        container.appendChild(toast);
        setTimeout(() => {
            toast.style.opacity = '0';
            toast.style.transform = 'translateX(100%)';
            setTimeout(() => toast.remove(), 300);
        }, 4000);
    },

    // ── Modal / Confirm ──────────────────────────────────
    _modalResolve: null,

    escapeHtml(s) {
        if (s == null) return '';
        return String(s)
            .replace(/&/g, '&amp;')
            .replace(/</g, '&lt;')
            .replace(/>/g, '&gt;')
            .replace(/"/g, '&quot;')
            .replace(/'/g, '&#39;');
    },

    /** One CSV cell, safe to open in a spreadsheet: quote-wrapped, and cells
        starting with a formula trigger (=, +, -, @, tab, CR) get a leading
        apostrophe — a subscriber named `=HYPERLINK(...)` must not execute in
        Excel/LibreOffice. */
    csvCell(v) {
        let s = String(v ?? '');
        // Leading whitespace is ignored when checking, like the server-side
        // sanitizer: Excel still evaluates " =SUM(A1)".
        if (/^\s*[=+\-@]/.test(s) || /^[\t\r]/.test(s)) s = "'" + s;
        return `"${s.replace(/"/g, '""')}"`;
    },

    _resetModalButtons() {
        const cancelBtn = document.getElementById('modalCancel');
        const confirmBtn = document.getElementById('modalConfirm');
        if (cancelBtn) cancelBtn.style.display = '';
        if (confirmBtn) {
            confirmBtn.style.display = '';
            confirmBtn.textContent = 'Confirm';
            confirmBtn.className = 'btn btn-danger';
            confirmBtn.onclick = null;
        }
    },

    /** Element that had focus before the shared overlay opened; restored on
        close so keyboard users land back on the control that triggered it. */
    _lastFocused: null,

    /** True while a bulk operation's progress dialog is on screen. */
    _progressOpen() {
        return !!document.getElementById('progressBar');
    },

    /** Keep Tab/Shift+Tab cycling inside `container` (focus trap). */
    _trapTab(container, e) {
        const focusables = Array.from(container.querySelectorAll(
            'button, [href], input, select, textarea, [tabindex]:not([tabindex="-1"])'
        )).filter(el => !el.disabled && el.offsetParent !== null);
        if (!focusables.length) { e.preventDefault(); return; }
        const first = focusables[0];
        const last = focusables[focusables.length - 1];
        const active = document.activeElement;
        if (e.shiftKey && (active === first || !container.contains(active))) {
            e.preventDefault();
            last.focus();
        } else if (!e.shiftKey && (active === last || !container.contains(active))) {
            e.preventDefault();
            first.focus();
        }
    },

    /** Show the shared overlay as a real dialog: remember the trigger and
        move focus inside. Tab cycling is trapped by the document-level
        keydown handler registered in init(). Focus lands on Cancel first:
        auto-focusing the danger-styled Confirm made a stray Enter confirm
        destructive deletes. */
    _openModalOverlay() {
        this._lastFocused = document.activeElement;
        const overlay = document.getElementById('modalOverlay');
        overlay.style.display = 'flex';
        const confirm = document.getElementById('modalConfirm');
        const cancel = document.getElementById('modalCancel');
        const target = (cancel && cancel.style.display !== 'none' && cancel)
            || (confirm && confirm.style.display !== 'none' && confirm)
            || overlay.querySelector('button, [href], input, select, textarea');
        if (target && target.focus) target.focus();
    },

    confirm(title, message) {
        return new Promise((resolve) => {
            this._modalResolve = resolve;
            document.getElementById('modalTitle').textContent = title;
            document.getElementById('modalBody').innerHTML = `<p>${this.escapeHtml(message)}</p>`;
            this._resetModalButtons();
            this._openModalOverlay();
            document.getElementById('modalConfirm').onclick = () => {
                this._modalResolve = null;
                this.closeModal();
                resolve(true);
            };
        });
    },

    closeModal() {
        document.getElementById('modalOverlay').style.display = 'none';
        this._resetModalButtons();
        if (this._lastFocused && this._lastFocused.focus) {
            this._lastFocused.focus();
            this._lastFocused = null;
        }
        if (this._modalResolve) {
            this._modalResolve(false);
            this._modalResolve = null;
        }
    },

    showProgress(title, message) {
        this._modalResolve = null;
        document.getElementById('modalTitle').textContent = title;
        document.getElementById('modalBody').innerHTML = `
            <p>${this.escapeHtml(message)}</p>
            <div id="progressPhase" style="margin-top:14px;font-weight:600;color:var(--text-primary)">Starting…</div>
            <div style="margin-top:10px;background:var(--bg-input);border-radius:9999px;height:12px;overflow:hidden;border:1px solid var(--glass-border);box-shadow:inset 0 1px 3px rgba(0,0,0,0.2)">
                <div id="progressBar" style="height:100%;width:0%;background:linear-gradient(90deg,var(--primary),var(--emerald));border-radius:9999px;box-shadow:0 0 12px var(--primary-glow);transition:width 0.3s cubic-bezier(0.16,1,0.3,1)"></div>
            </div>
            <div id="progressStats" style="margin-top:10px;font-size:0.88rem;color:var(--text-secondary);text-align:center;font-feature-settings:'tnum'">0 / 0</div>
            <div id="progressMessage" style="margin-top:8px;font-size:0.82rem;color:var(--text-muted);font-family:monospace;word-break:break-word;text-align:center"></div>
        `;
        document.getElementById('modalCancel').style.display = 'none';
        document.getElementById('modalConfirm').style.display = 'none';
        this._openModalOverlay();
    },

    updateProgress(progress) {
        const phaseEl = document.getElementById('progressPhase');
        const barEl = document.getElementById('progressBar');
        const statsEl = document.getElementById('progressStats');
        const msgEl = document.getElementById('progressMessage');
        if (!phaseEl || !barEl) return;
        const phaseLabels = {
            starting: 'Starting…',
            fetching: 'Fetching hard bounces',
            identifying: 'Identifying blacklist bounces',
            reclassifying: 'Reclassifying hard → soft',
            unblocking: 'Unblocking subscribers',
            done: 'Complete',
            error: 'Error',
        };
        phaseEl.textContent = phaseLabels[progress.phase] || progress.phase || '';
        const pct = progress.total > 0
            ? Math.min(100, Math.round((progress.current / progress.total) * 100))
            : (progress.status === 'done' ? 100 : 0);
        barEl.style.width = pct + '%';
        statsEl.textContent = progress.total > 0
            ? `${progress.current} / ${progress.total} (${pct}%)`
            : `${progress.current}`;
        if (msgEl) msgEl.textContent = progress.message || '';
    },

    showResult(title, bodyHtml) {
        this._modalResolve = null;
        const body = document.getElementById('modalBody');
        document.getElementById('modalTitle').textContent = title;
        body.innerHTML = bodyHtml;
        this.labelControls(body);
        document.getElementById('modalCancel').style.display = 'none';
        const confirmBtn = document.getElementById('modalConfirm');
        confirmBtn.style.display = '';
        confirmBtn.textContent = 'OK';
        confirmBtn.className = 'btn btn-primary';
        confirmBtn.onclick = () => this.closeModal();
        this._openModalOverlay();
    },

    /** A small form in the shared dialog: Cancel + one primary action, so
        it never shows a second "OK" next to the form's own button. The
        handler returns false to keep the dialog open (validation failed). */
    showForm(title, bodyHtml, confirmText, onConfirm) {
        this._modalResolve = null;
        const body = document.getElementById('modalBody');
        document.getElementById('modalTitle').textContent = title;
        body.innerHTML = bodyHtml;
        this.labelControls(body);
        const cancelBtn = document.getElementById('modalCancel');
        cancelBtn.style.display = '';
        const confirmBtn = document.getElementById('modalConfirm');
        confirmBtn.style.display = '';
        confirmBtn.textContent = confirmText;
        confirmBtn.className = 'btn btn-primary';
        confirmBtn.onclick = async () => {
            confirmBtn.disabled = true;
            try {
                const keep = await onConfirm();
                if (keep !== false) this.closeModal();
            } finally {
                confirmBtn.disabled = false;
            }
        };
        this._openModalOverlay();
        const first = body.querySelector('input, select, textarea');
        if (first) first.focus();
        // Enter in the form's field submits like the button.
        body.querySelectorAll('input').forEach(inp => inp.addEventListener('keydown', (e) => {
            if (e.key === 'Enter') { e.preventDefault(); confirmBtn.click(); }
        }));
    },

    // ── Pagination ───────────────────────────────────────
    renderPagination(currentPage, total, perPage, onPageChange) {
        const totalPages = Math.ceil(total / perPage);
        if (totalPages <= 1) return '';

        // Build the list of page numbers to show (windowed with ellipsis).
        // The ellipsis conditions compare against the window actually
        // rendered (start/end), so no page silently vanishes at the edges —
        // e.g. page 4 of 10 used to render "1 3 4 5 … 10" with page 2 gone
        // and no hint that anything was hidden.
        const pages = [];
        if (totalPages <= 7) {
            for (let i = 1; i <= totalPages; i++) pages.push(i);
        } else {
            pages.push(1);
            const start = Math.max(2, currentPage - 1);
            const end = Math.min(totalPages - 1, currentPage + 1);
            if (start > 2) pages.push('...');
            for (let i = start; i <= end; i++) pages.push(i);
            if (end < totalPages - 1) pages.push('...');
            pages.push(totalPages);
        }

        let html = '<div class="pagination">';
        html += `<button class="btn btn-sm" ${currentPage <= 1 ? 'disabled' : ''} onclick="${onPageChange}(${currentPage - 1})">&#8592;</button>`;
        for (const p of pages) {
            if (p === '...') {
                html += '<span class="pagination-ellipsis">&hellip;</span>';
            } else {
                const active = p === currentPage ? ' active' : '';
                html += `<button class="btn btn-sm${active}" onclick="${onPageChange}(${p})">${p}</button>`;
            }
        }
        html += `<button class="btn btn-sm" ${currentPage >= totalPages ? 'disabled' : ''} onclick="${onPageChange}(${currentPage + 1})">&#8594;</button>`;
        html += `<span class="pagination-info">${App.formatNumber(total)} records</span>`;
        html += '</div>';
        return html;
    },

    /** Bulk deletes can leave the current page past the last page; without a
        clamp the re-render shows an empty "no records" page even though
        earlier pages still hold rows. Returns the page to render. */
    clampPage(page, total, perPage) {
        const totalPages = Math.max(1, Math.ceil((total || 0) / perPage));
        return Math.min(Math.max(1, page), totalPages);
    },

    // ── Multi-select (bulk actions) ──────────────────────
    /**
     * Per-table selection state shared by the list pages.
     *
     * A page registers what its bulk bar can do on every render
     * (App.selection.register), renders App.selection.bar(key) above its
     * table and wires row checkboxes to rowToggle. Values are always held as
     * strings so a page can rebuild a set across pagination without type
     * mismatches.
     */
    selection: {
        sets: {},
        configs: {},

        set(key) {
            if (!this.sets[key]) this.sets[key] = new Set();
            return this.sets[key];
        },

        register(key, config) {
            this.configs[key] = config || {};
        },

        count(key) { return this.set(key).size; },
        values(key) { return [...this.set(key)]; },
        has(key, value) { return this.set(key).has(String(value)); },

        pageFullySelected(key, values) {
            return values.length > 0 && values.every(v => this.set(key).has(String(v)));
        },

        /** Row checkbox handler; trusts the element's own checked state. */
        rowToggle(key, value, checkbox) {
            const selected = this.set(key);
            const val = String(value);
            const checked = checkbox ? checkbox.checked : !selected.has(val);
            if (checked) selected.add(val); else selected.delete(val);
            if (checkbox) checkbox.closest('tr')?.classList.toggle('row-selected', checked);
            this.sync(key);
        },

        /** Select-all handler: applies to every value on the current page. */
        pageToggle(key, values, checkbox) {
            const selected = this.set(key);
            const wanted = values.map(String);
            wanted.forEach(v => checkbox.checked ? selected.add(v) : selected.delete(v));
            document.querySelectorAll(`.row-select[data-key="${key}"]`).forEach(box => {
                if (wanted.includes(String(box.dataset.value))) {
                    box.checked = checkbox.checked;
                    box.closest('tr')?.classList.toggle('row-selected', checkbox.checked);
                }
            });
            this.sync(key);
        },

        clear(key) {
            this.set(key).clear();
            document.querySelectorAll(`.row-select[data-key="${key}"]`).forEach(box => {
                box.checked = false;
                box.closest('tr')?.classList.remove('row-selected');
            });
            this.sync(key);
        },

        /** Drop every page's selection (called when navigating away). */
        clearAll() {
            Object.keys(this.sets).forEach(key => this.sets[key].clear());
            this.configs = {};
        },

        /** Refresh the bulk bar and header checkbox after a selection change. */
        sync(key) {
            const bar = document.getElementById(`bulkBar_${key}`);
            if (bar) bar.innerHTML = this.barHtml(key);
            const box = document.getElementById(`selectAll_${key}`);
            if (box) {
                const values = (this.configs[key] || {}).pageValues || [];
                const onPage = values.filter(v => this.set(key).has(String(v))).length;
                box.checked = values.length > 0 && onPage === values.length;
                box.indeterminate = onPage > 0 && onPage < values.length;
            }
        },

        barHtml(key) {
            const count = this.count(key);
            if (!count) return '';
            const cfg = this.configs[key] || {};
            const noun = cfg.noun || 'item';
            // Pages whose noun does not pluralise with a plain "s" ("reply" →
            // "replies") pass `plural`; everyone else keeps noun + "s".
            const label = count === 1 ? noun : (cfg.plural || `${noun}s`);
            const actions = (cfg.actions || []).map(a =>
                `<button class="btn btn-sm ${a.danger ? 'btn-danger' : ''}" onclick="${a.onclick}">${a.label}</button>`
            ).join('');
            return `
                <span class="bulk-bar-count"><strong>${count}</strong> ${label} selected</span>
                <div class="action-btns">
                    ${actions}
                    <button class="btn btn-sm" onclick="App.selection.clear('${key}')">Clear</button>
                </div>`;
        },

        /** Container for a page's bulk bar; kept mounted so sync() can fill it. */
        bar(key) {
            return `<div class="bulk-bar" id="bulkBar_${key}">${this.barHtml(key)}</div>`;
        },

        /** Table-header "select all on this page" checkbox. */
        headbox(key, label) {
            const values = (this.configs[key] || {}).pageValues || [];
            return `<label class="cell-check"><input type="checkbox" id="selectAll_${key}"
                aria-label="${App.escapeHtml(label || 'Select all on this page')}"
                ${this.pageFullySelected(key, values) ? 'checked' : ''}
                onchange="App.selection.pageToggle('${key}', (App.selection.configs['${key}'] || {}).pageValues || [], this)"/></label>`;
        },

        /** Per-row checkbox. `value` is stored as its string form.

            Wrapped in a label on purpose: a bare 16x16 input is the whole click
            target, which is well under the 24x24 minimum (44 for touch). The
            label grows the target without moving the 16px box the eye sees. */
        rowbox(key, value, label) {
            const val = String(value);
            return `<label class="cell-check"><input type="checkbox" class="row-select" data-key="${key}" data-value="${App.escapeHtml(val)}"
                aria-label="${App.escapeHtml(label || 'Select row')}"
                ${this.has(key, val) ? 'checked' : ''}
                onchange="App.selection.rowToggle('${key}', ${App.escapeHtml(JSON.stringify(val))}, this)"/></label>`;
        },
    },

    // ── Status Badge ─────────────────────────────────────
    statusBadge(status) {
        const map = {
            'enabled': 'success', 'active': 'success', 'running': 'success', 'finished': 'success',
            'blocklisted': 'danger', 'cancelled': 'danger', 'archived': 'default',
            'draft': 'info', 'scheduled': 'warning', 'paused': 'warning',
        };
        const cls = map[status] || 'default';
        return `<span class="badge badge-${cls}">${this.escapeHtml(status)}</span>`;
    },

    // ── Format Date ──────────────────────────────────────
    formatDate(dateStr) {
        if (!dateStr) return '-';
        const d = new Date(dateStr);
        // Wrapped in .cell-date: without it "Oct 1, 2026" broke across two
        // lines in the narrow Date/Created table columns, doubling row height.
        return `<span class="cell-date">${d.toLocaleDateString(undefined, { year: 'numeric', month: 'short', day: 'numeric' })}</span>`;
    },

    /** Shorten a chart axis label without cutting mid-word: the old
        `substring(0, 20)` turned "1st Campaign (Remaining Leads)" into
        "1st Campaign (Remain" with no ellipsis. */
    truncateLabel(text, max = 22) {
        const s = String(text ?? '').trim();
        if (!s) return 'Untitled';
        return s.length > max ? s.slice(0, max - 1).trimEnd() + '…' : s;
    },

    formatDateTime(dateStr) {
        if (!dateStr) return '-';
        const d = new Date(dateStr);
        return d.toLocaleString(undefined, {
            year: 'numeric', month: 'short', day: 'numeric',
            hour: '2-digit', minute: '2-digit'
        });
    },

    // ── Number Format ────────────────────────────────────
    formatNumber(n) {
        if (n === undefined || n === null) return '0';
        return n.toLocaleString();
    },

    // ── Set Content ──────────────────────────────────────
    setContent(html) {
        const area = document.getElementById('contentArea');
        area.innerHTML = html;
        this.labelControls(area);
    },

    /** Give every control an accessible name, and hide decorative icons.
        
        Pages build their filter rows as raw HTML strings, and 19 of those
        controls shipped with a placeholder but no label — which is not a
        label, so a screen reader announced an unnamed combobox. Running one
        pass per setContent() keeps pages from regressing one at a time
        instead of forcing every renderer to remember. */
    labelControls(root) {
        const labels = Array.from(root.querySelectorAll('label'));

        root.querySelectorAll(
            'select, input:not([type="hidden"]):not([type="checkbox"]):not([type="radio"]):not([type="submit"]), textarea'
        ).forEach(el => {
            if (el.getAttribute('aria-label') || el.getAttribute('aria-labelledby') || el.title) return;
            if (el.closest('label')) return;
            if (el.id && labels.some(l => l.htmlFor === el.id)) return;

            const group = el.closest('.form-group, .search-input, .filter-group');
            const ownLabel = group && group.querySelector('label');
            // Labels are frequently a *sibling* of the control rather than a
            // wrapper or a `for`-linked pair (the schedule time selectors do
            // this), so the previous element counts as a label too.
            const prev = el.previousElementSibling;
            const prevLabel = prev && prev.tagName === 'LABEL' ? prev.textContent.trim() : '';
            const name = (ownLabel && ownLabel.textContent.trim())
                || prevLabel
                || el.getAttribute('placeholder')
                || el.getAttribute('name')
                || '';
            if (name) el.setAttribute('aria-label', name);
        });

        // Icons are decoration next to text. Skip any icon that is itself the
        // only content of a control, or hiding it would leave the control
        // completely unnamed instead of merely redundant.
        root.querySelectorAll('svg').forEach(svg => {
            if (svg.hasAttribute('aria-hidden') || svg.getAttribute('role') || svg.querySelector('title')) return;
            const control = svg.closest('button, a');
            if (control && !control.textContent.trim()
                && !control.getAttribute('aria-label') && !control.title) return;
            svg.setAttribute('aria-hidden', 'true');
            svg.setAttribute('focusable', 'false');
        });
    },
};

// ── Templates Page (simple) ──────────────────────────────
const Templates = {
    _data: [],
    _filter: '',

    async render() {
        const seq = App.seq();
        // Deep-linkable state: #/templates?q=sequence
        const rq = App.routeQuery();
        if (rq.get('q') !== null) this._filter = rq.get('q') || '';
        try {
            const result = await API.get('/api/templates');
            if (App.stale(seq)) return;
            this._data = result?.data || [];

            const needle = this._filter.trim().toLowerCase();
            const templates = needle
                ? this._data.filter(t => (t.name || '').toLowerCase().includes(needle))
                : this._data;

            App.selection.register('templates', {
                noun: 'template',
                pageValues: templates.map(t => String(t.id)),
                actions: [
                    { label: 'Delete', onclick: 'Templates.bulkDelete()', danger: true },
                ],
            });

            let html = `
                <div class="search-bar">
                    <div class="search-input">
                        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="11" cy="11" r="8"/><line x1="21" y1="21" x2="16.65" y2="16.65"/></svg>
                        <input type="search" id="tplSearch" placeholder="Search templates by name…"
                            aria-label="Search templates"
                            value="${App.escapeHtml(this._filter)}" oninput="Templates.searchLive()" onkeydown="if(event.key==='Enter')Templates.search()">
                    </div>
                    <span class="search-count">${templates.length} template${templates.length === 1 ? '' : 's'}</span>
                    <div class="page-actions">
                        <button class="btn btn-primary" onclick="Templates.showCreate()">+ New Template</button>
                    </div>
                </div>
                ${App.selection.bar('templates')}
                <div class="table-wrapper" tabindex="0" role="region" aria-label="Scrollable data table"><table>
                    <thead><tr>
                        <th class="col-select">${App.selection.headbox('templates', 'Select all templates shown')}</th>
                        <th>ID</th><th>Name</th><th>Type</th><th>Default</th><th>Created</th><th class="cell-actions">Actions</th>
                    </tr></thead><tbody>`;

            if (!templates.length) {
                html += '<tr><td colspan="7"><div class="empty-state"><h2>No templates found</h2></div></td></tr>';
            }
            templates.forEach(t => {
                const name = App.escapeHtml(t.name || '');
                html += `<tr class="${App.selection.has('templates', t.id) ? 'row-selected' : ''}">
                    <td class="col-select">${App.selection.rowbox('templates', t.id, `Select ${t.name}`)}</td>
                    <td>${t.id}</td>
                    <td><a class="cell-link cell-clamp cell-clamp-lg" href="javascript:void(0)" onclick="Templates.preview(${t.id})" title="${name}">${name}</a></td>
                    <td><span class="badge badge-info">${App.escapeHtml(t.type || '-')}</span></td>
                    <td>${t.is_default ? '<span class="badge badge-success">Default</span>' : '-'}</td>
                    <td title="Updated ${App.escapeHtml(App.formatDateTime(t.updated_at))}">${App.formatDate(t.created_at)}</td>
                    <td class="cell-actions">
                        <div class="action-btns">
                            <button class="btn btn-sm" onclick="Templates.preview(${t.id})">Preview</button>
                            <button class="btn btn-sm" onclick="Templates.showEdit(${t.id})">Edit</button>
                            ${App.moreMenu([
                                { label: 'Duplicate', onclick: `Templates.duplicate(${t.id})` },
                                { label: 'Set as default', onclick: `Templates.setDefault(${t.id})`, hidden: !!t.is_default },
                                { label: 'Delete…', onclick: `Templates.remove(${t.id})`, danger: true, hidden: !!t.is_default },
                            ], `More actions for ${t.name}`)}
                        </div>
                    </td>
                </tr>`;
            });

            html += '</tbody></table></div>';
            if (App.stale(seq)) return;
            App.syncUrl('templates', { q: this._filter });
            const keepFocus = document.activeElement?.id === 'tplSearch';
            App.setContent(html);
            if (keepFocus) {
                const box = document.getElementById('tplSearch');
                if (box) { box.focus(); box.setSelectionRange(box.value.length, box.value.length); }
            }
        } catch (err) {
            console.error('Templates load error:', err);
            if (App.stale(seq)) return;
            App.setContent(`<div class="empty-state"><h2>Failed to load templates</h2><p>${App.escapeHtml(err.message || '')}</p></div>`);
        }
    },

    search() {
        this._filter = document.getElementById('tplSearch')?.value || '';
        App.bumpSeq();
        this.render();
    },

    searchLive: App.debounce(() => Templates.search(), 250),

    /** Editor with a live, ListMonk-rendered preview beside the code. */
    _editor({ title, nameId, bodyId, name, body, actions }) {
        return `
            <div class="inline-form template-editor">
                <h2 style="margin-bottom:16px">${title}</h2>
                <div class="form-group"><label>Name</label><input type="text" id="${nameId}" value="${App.escapeHtml(name || '')}" placeholder="Template name"></div>
                <div class="template-editor-grid">
                    <div class="form-group">
                        <label for="${bodyId}">Body (HTML)</label>
                        <textarea id="${bodyId}" class="code-input" rows="24" spellcheck="false" placeholder="HTML content. Put {{ template &quot;content&quot; . }} where the campaign body goes."
                            oninput="Templates.livePreview('${bodyId}')">${App.escapeHtml(body || '')}</textarea>
                        <p class="field-hint">A campaign template must contain <code>{{ template "content" . }}</code>: that is where each campaign's body is inserted.</p>
                    </div>
                    <div class="form-group">
                        <div class="label-row"><label>Live preview</label><span class="cell-sub" id="tplPreviewState">Sample campaign &amp; subscriber</span></div>
                        <iframe class="preview-frame template-live-frame" id="tplLiveFrame" sandbox="" title="Template preview"></iframe>
                    </div>
                </div>
                <div class="form-actions">${actions}</div>
            </div>`;
    },

    _previewTimer: null,
    livePreview(bodyId, immediate = false) {
        clearTimeout(this._previewTimer);
        const run = async () => {
            const frame = document.getElementById('tplLiveFrame');
            const state = document.getElementById('tplPreviewState');
            const body = document.getElementById(bodyId)?.value ?? '';
            if (!frame) return;
            if (!body.trim()) { frame.srcdoc = ''; if (state) state.textContent = 'Nothing to preview yet'; return; }
            if (state) state.textContent = 'Rendering…';
            try {
                const res = await API.request('POST', '/api/templates/preview', { body, type: 'campaign' }, false, true);
                if (!document.getElementById('tplLiveFrame')) return;
                frame.srcdoc = res?.html ?? '';
                if (state) state.textContent = 'Rendered by ListMonk with sample data';
            } catch (err) {
                if (state) state.textContent = `Template error: ${err?.message || 'could not render'}`;
            }
        };
        if (immediate) run(); else this._previewTimer = setTimeout(run, 700);
    },

    async preview(id) {
        const t = this._data.find(tpl => tpl.id === id);
        if (!t) return;
        const seq = App.bumpSeq();
        const header = `
            <div style="margin-bottom:12px">
                <button class="btn btn-secondary" onclick="Templates.render()">Back to Templates</button>
                <span style="margin-left:12px;font-size:1.1rem;font-weight:600">${App.escapeHtml(t.name || '')}</span>
            </div>`;
        App.setContent(`${header}<div class="loading-spinner">Rendering preview…</div>`);
        // ListMonk renders the template with a sample campaign and subscriber,
        // so {{ … }} actions show their output instead of raw template code.
        let html = null;
        try {
            const res = await API.get(`/api/templates/${id}/preview`, true);
            html = res && typeof res.html === 'string' ? res.html : null;
        } catch { /* fall back to the raw body below */ }
        if (App.stale(seq)) return;
        const note = html === null
            ? '<p style="margin-bottom:8px;color:var(--text-muted)">ListMonk could not render this template (check it for template errors). Showing the raw HTML.</p>'
            : '<p style="margin-bottom:8px;color:var(--text-muted)">Rendered with ListMonk\'s sample campaign and subscriber.</p>';
        App.setContent(`${header}${note}
            <iframe class="preview-frame" sandbox="" srcdoc="${App.escapeHtml(html ?? (t.body || ''))}"></iframe>
        `);
    },

    showCreate() {
        const starter = `<!doctype html>
<html>
  <body style="font-family: Arial, sans-serif; color: #222;">
    {{ template "content" . }}
    <p style="font-size: 12px; color: #888;">
      <a href="{{ UnsubscribeURL }}">Unsubscribe</a>
    </p>
  </body>
</html>`;
        App.setContent(this._editor({
            title: 'Create Template', nameId: 'tplName', bodyId: 'tplBody', name: '', body: starter,
            actions: `<button class="btn btn-primary" onclick="Templates.create()">Create</button>
                      <button class="btn btn-secondary" onclick="Templates.render()">Cancel</button>`,
        }));
        this.livePreview('tplBody', true);
    },

    async showEdit(id) {
        const t = this._data.find(tpl => tpl.id === id);
        if (!t) { App.toast('Template not found', 'error'); return; }
        App.setContent(this._editor({
            title: `Edit Template #${t.id}`, nameId: 'editTplName', bodyId: 'editTplBody', name: t.name, body: t.body,
            actions: `<button class="btn btn-primary" onclick="Templates.update(${t.id})">Save</button>
                      <button class="btn" onclick="Templates.preview(${t.id})">Full preview</button>
                      <button class="btn btn-secondary" onclick="Templates.render()">Cancel</button>`,
        }));
        this.livePreview('editTplBody', true);
    },

    async create() {
        const name = document.getElementById('tplName').value.trim();
        const body = document.getElementById('tplBody').value;
        if (!name) { App.toast('Name is required', 'error'); return; }
        try {
            await API.post('/api/templates', { name, body, type: 'campaign' });
            App.toast('Template created', 'success');
            this.render();
        } catch { /* API.request already surfaced the server message */ }
    },

    async update(id) {
        const name = document.getElementById('editTplName').value.trim();
        const body = document.getElementById('editTplBody').value;
        if (!name) { App.toast('Name is required', 'error'); return; }
        try {
            await API.put(`/api/templates/${id}`, { name, body });
            App.toast('Template updated', 'success');
            this.render();
        } catch { /* API.request already surfaced the server message */ }
    },

    async setDefault(id) {
        try {
            await API.put(`/api/templates/${id}/default`);
            App.toast('Default template updated', 'success');
            this.render();
        } catch { /* API.request already surfaced the server message */ }
    },

    async remove(id) {
        if (await App.confirm('Delete Template', 'Are you sure you want to delete this template?')) {
            try {
                await API.del(`/api/templates/${id}`);
                App.toast('Template deleted', 'success');
                App.selection.clear('templates');
                this.render();
            } catch { /* API.request already surfaced the server message */ }
        }
    },

    /** Create a copy of a template so it can be edited without touching the original. */
    async duplicate(id) {
        const t = this._data.find(tpl => tpl.id === id);
        if (!t) { App.toast('Template not found', 'error'); return; }
        try {
            await API.post('/api/templates', {
                name: `${t.name || 'Template'} (copy)`,
                body: t.body || '',
                type: t.type || 'campaign',
            });
            App.toast('Template duplicated', 'success');
            this.render();
        } catch { /* API.request already surfaced the server message */ }
    },

    async bulkDelete() {
        const ids = App.selection.values('templates').map(Number).filter(Boolean);
        if (!ids.length) return;
        if (!await App.confirm('Delete Templates',
                `Permanently delete ${ids.length} selected template(s)?`)) return;
        try {
            const params = ids.map(id => `ids=${id}`).join('&');
            const result = await API.del(`/api/templates?${params}`, API.LONG_TIMEOUT);
            const errors = result?.errors || 0;
            App.toast(`Deleted ${result?.deleted ?? ids.length} template(s)${errors ? `, ${errors} failed (default templates cannot be deleted)` : ''}`,
                errors ? 'info' : 'success');
            App.selection.clear('templates');
            this.render();
        } catch { /* API.request already surfaced the server message */ }
    },
};

// ── Bounces Page ─────────────────────────────────────────
const Bounces = {
    page: 1,
    campaignFilter: 0,
    typeFilter: 'hard',
    campaigns: [],
    total: 0,
    perPage: 25,
    // Mirror of MAX_PAGE in app/routers/bounces.py: the hard-bounce total
    // comes from a cache that is not page-bounded, so a pager tap (or deep
    // link) past the backend bound must be clamped or FastAPI answers 422.
    MAX_PAGE: 1500,

    _queryParams() {
        const params = new URLSearchParams({
            page: String(this.page),
            per_page: String(this.perPage),
        });
        if (this.typeFilter) params.set('bounce_type', this.typeFilter);
        if (this.campaignFilter) params.set('campaign_id', String(this.campaignFilter));
        return params.toString();
    },

    _typeLabel() {
        if (this.typeFilter === 'hard') return 'hard bounces';
        if (this.typeFilter === 'soft') return 'soft bounces';
        return 'bounces';
    },

    async render() {
        const seq = App.seq();
        // Deep-linkable state: #/bounces?page=2&type=soft&campaign=6
        const rq = App.routeQuery();
        if (rq.get('page')) this.page = Math.max(1, parseInt(rq.get('page'), 10) || 1);
        if (rq.get('type') !== null) this.typeFilter = rq.get('type') || '';
        if (rq.get('campaign') !== null) this.campaignFilter = parseInt(rq.get('campaign'), 10) || 0;
        this.page = Math.min(Math.max(1, this.page), this.MAX_PAGE);
        try {
            const fetchCampaigns = !this.campaigns.length
                ? API.get('/api/campaigns?per_page=500&order_by=created_at&order=DESC')
                : Promise.resolve(null);

            // Per-type totals for the tabs (best effort: a failed count shows
            // no number rather than failing the page).
            const countFor = (type) => {
                const p = new URLSearchParams({ page: '1', per_page: '1' });
                if (type) p.set('bounce_type', type);
                if (this.campaignFilter) p.set('campaign_id', String(this.campaignFilter));
                return API.get(`/api/bounces?${p}`, true).then(r => r?.data?.total ?? null).catch(() => null);
            };
            const [campRes, bounceRes, ...counts] = await Promise.all([
                fetchCampaigns,
                API.get(`/api/bounces?${this._queryParams()}`),
                ...['hard', 'soft', ''].map(t => t === this.typeFilter ? Promise.resolve('self') : countFor(t)),
            ]);
            if (App.stale(seq)) return;
            if (campRes) this.campaigns = campRes?.data?.results || [];

            const data = bounceRes?.data || {};
            const pageBounces = data.results || [];
            const total = data.total || 0;
            const typeCounts = Object.fromEntries(['hard', 'soft', ''].map((t, i) =>
                [t, counts[i] === 'self' ? total : counts[i]]));
            // A delete can strand the view past the last page; clamp and
            // re-fetch instead of painting a false "no bounces" page.
            const clamped = App.clampPage(this.page, total, this.perPage);
            if (clamped !== this.page) { this.page = clamped; return this.render(); }
            this.total = total;
            this._rows = pageBounces;

            // Build campaign filter options
            const campOptions = this.campaigns.map(c =>
                `<option value="${c.id}" ${c.id === this.campaignFilter ? 'selected' : ''}>${App.escapeHtml(c.name)} (${App.escapeHtml(c.status || '')})</option>`
            ).join('');

            const filterLabel = this.campaignFilter
                ? this.campaigns.find(c => c.id === this.campaignFilter)?.name || `#${this.campaignFilter}`
                : 'All campaigns';

            const deleteActionLabel = this.campaignFilter
                ? `Delete ${this._typeLabel()}`
                : `Delete All ${this.typeFilter ? (this.typeFilter === 'hard' ? 'Hard ' : 'Soft ') : ''}Bounces`;

            App.selection.register('bounces', {
                noun: 'bounce',
                pageValues: this.pageIds(),
                actions: [
                    { label: 'Export Selected', onclick: 'Bounces.exportSelected()' },
                    { label: 'Delete Selected', onclick: 'Bounces.deleteSelected()', danger: true },
                ],
            });

            // The page's actions belong in the filter row, next to the filters
            // they act on - the shared top bar keeps only the page title and
            // the global mode/theme controls.
            let html = `
                <div class="search-bar">
                    <select id="bounceCampFilter" aria-label="Filter bounces by campaign" onchange="Bounces.filterCampaign(this.value)" style="width:auto;min-width:240px">
                        <option value="0" ${!this.campaignFilter ? 'selected' : ''}>All Campaigns</option>
                        ${campOptions}
                    </select>
                    <div class="deliv-seg" role="tablist" aria-label="Bounce type">
                        ${[['hard', 'Hard'], ['soft', 'Soft'], ['', 'All']].map(([v, l]) => {
                            const n = typeCounts[v];
                            const active = this.typeFilter === v;
                            return `<button type="button" role="tab" aria-selected="${active}" class="deliv-seg-btn${active ? ' is-active' : ''}"
                                onclick="Bounces.filterType('${v}')">${l}${n === null || n === undefined ? '' : ` <span class="seg-count">${App.formatNumber(n)}</span>`}</button>`;
                        }).join('')}
                    </div>
                    ${this.campaignFilter ? `<span class="search-count">for ${App.escapeHtml(filterLabel)}</span>` : ''}
                    <div class="page-actions">
                        <button class="btn btn-primary" onclick="Bounces.ingestBounces()">Ingest New Bounces</button>
                        ${App.moreMenu([
                            { label: 'Export CSV', onclick: 'Bounces.exportBounces()' },
                            { label: `${deleteActionLabel}…`, onclick: 'Bounces.deleteAll()', danger: true, hidden: !total },
                        ], 'More bounce actions')}
                    </div>
                </div>
                ${App.selection.bar('bounces')}`;

            // The empty state renders outside the table on purpose: the table
            // keeps a 600px min-width on phones, which pushed the centred
            // message off-screen behind the pinned action column.
            if (!pageBounces.length) {
                html += `<div class="card"><div class="empty-state"><h2>No ${this._typeLabel()} found</h2>
                    ${this.typeFilter === 'hard' && typeCounts.soft ? `<p>There ${typeCounts.soft === 1 ? 'is' : 'are'} ${App.formatNumber(typeCounts.soft)} soft bounce${typeCounts.soft === 1 ? '' : 's'}. <a href="javascript:void(0)" onclick="Bounces.filterType('soft')">Show soft bounces</a></p>` : ''}</div></div>`;
            } else {
                html += `<div class="table-wrapper" tabindex="0" role="region" aria-label="Scrollable data table"><table>
                    <thead><tr>
                        <th class="col-select">${App.selection.headbox('bounces', 'Select all bounces on this page')}</th>
                        <th>ID</th><th>Email</th><th>Campaign</th><th>Source</th><th>Type</th><th>Date</th><th class="cell-actions">Actions</th>
                    </tr></thead><tbody>`;
                pageBounces.forEach(b => {
                    const campName = App.escapeHtml(b.campaign?.name || '-');
                    html += `<tr class="${App.selection.has('bounces', b.id) ? 'row-selected' : ''}">
                        <td class="col-select">${App.selection.rowbox('bounces', b.id, `Select ${b.email}`)}</td>
                        <td>${b.id}</td>
                        <td><strong>${App.escapeHtml(b.email || '-')}</strong></td>
                        <td style="max-width:250px;overflow:hidden;text-overflow:ellipsis">${campName}</td>
                        <td>${App.escapeHtml(b.source || '-')}</td>
                        <td><span class="badge badge-${b.type === 'hard' ? 'danger' : 'warning'}">${App.escapeHtml(b.type || 'unknown')}</span></td>
                        <td>${App.formatDate(b.created_at)}</td>
                        <td class="cell-actions"><button class="btn btn-sm btn-danger" onclick="Bounces.remove(${b.id})">Delete</button></td>
                    </tr>`;
                });

                html += '</tbody></table></div>';
            }

            html += App.renderPagination(this.page, total, this.perPage, 'Bounces.goToPage');
            if (App.stale(seq)) return;
            App.syncUrl('bounces', {
                page: this.page, type: this.typeFilter, campaign: this.campaignFilter,
            });
            App.setContent(html);
        } catch (err) {
            if (App.stale(seq)) return;
            App.setContent(`<div class="empty-state"><h2>Failed to load bounces</h2><p>${App.escapeHtml(err.message || '')}</p></div>`);
        }
    },

    filterCampaign(val) {
        this.campaignFilter = parseInt(val) || 0;
        this.page = 1;
        App.bumpSeq();
        this.render();
    },

    filterType(val) {
        this.typeFilter = val || '';
        this.page = 1;
        App.bumpSeq();
        this.render();
    },

    goToPage(p) { this.page = p; App.bumpSeq(); this.render(); },

    /** IDs (as strings) of the bounce rows currently on screen. */
    pageIds() {
        return (this._rows || []).map(b => String(b.id));
    },

    _selectedIds() {
        return App.selection.values('bounces').map(Number).filter(Boolean);
    },

    /** Delete only the rows the user checked, not the whole filter. */
    async deleteSelected() {
        const ids = this._selectedIds();
        if (!ids.length) return;
        if (!await App.confirm('Delete Bounces',
                `Permanently delete ${ids.length} selected bounce record(s)? Continue?`)) return;

        App.showProgress('Delete Bounces', `Deleting ${ids.length} selected bounce(s). Please wait…`);
        try {
            const params = ids.map(id => `ids=${id}`).join('&');
            const result = await API.del(`/api/bounces?${params}`, API.LONG_TIMEOUT);
            App.showResult('Delete Bounces — Done', `
                <ul style="line-height:1.8">
                    <li>Deleted: <strong>${result?.deleted ?? 0}</strong></li>
                    ${result?.errors ? `<li>Errors: <strong>${result.errors}</strong></li>` : ''}
                </ul>
            `);
            App.selection.clear('bounces');
            this.render();
        } catch (err) {
            App.showResult('Delete Bounces — Failed',
                `<p style="color:#e74c3c">${App.escapeHtml(err.message || 'Unknown error')}</p>`);
        }
    },

    /** CSV of the selected rows on this page. */
    exportSelected() {
        const selected = this._selectedIds();
        const rows = (this._rows || []).filter(b => selected.includes(b.id));
        if (!rows.length) { App.toast('No selected rows on this page', 'error'); return; }

        const header = ['id', 'email', 'campaign', 'source', 'type', 'created_at'];
        const csvRows = rows.map(b => [
            b.id, b.email || '', b.campaign?.name || '', b.source || '', b.type || '', b.created_at || '',
        ]);
        const csv = [header, ...csvRows]
            .map(cols => cols.map(v => App.csvCell(v)).join(','))
            .join('\n');

        API.downloadBlob(new Blob([csv], { type: 'text/csv' }), 'selected_bounces.csv');
        App.toast(`Exported ${rows.length} row(s)`, 'success');
    },

    async remove(id) {
        // Irreversible delete from a sticky action column: every other
        // destructive action in the app confirms first, this one must too.
        if (!await App.confirm('Delete Bounce',
                'Permanently delete this bounce record? Continue?')) return;
        try {
            await API.del(`/api/bounces/${id}`);
            App.toast('Bounce deleted', 'success');
            App.selection.clear('bounces');
            this.render();
        } catch {
            // API.request already surfaced the server message.
        }
    },

    _campaignLabel() {
        if (this.campaignFilter) {
            return this.campaigns.find(c => c.id === this.campaignFilter)?.name
                || `#${this.campaignFilter}`;
        }
        return 'All Campaigns';
    },

    _campaignQuery() {
        const params = [];
        if (this.campaignFilter) params.push(`campaign_id=${this.campaignFilter}`);
        return params.length ? `?${params.join('&')}` : '';
    },

    async deleteAll() {
        const typeDesc = this.typeFilter ? `${this.typeFilter} ` : '';
        const label = this.campaignFilter
            ? `${typeDesc}bounces for "${this._campaignLabel()}"`
            : `ALL ${typeDesc}bounce records`;
        if (!await App.confirm('Delete Bounces', `This will permanently delete ${label}. Continue?`)) return;
        App.showProgress('Delete Bounces', `Deleting ${label}. Please wait…`);
        try {
            const params = new URLSearchParams();
            if (this.typeFilter) params.set('bounce_type', this.typeFilter);
            if (this.campaignFilter) params.set('campaign_id', String(this.campaignFilter));
            // The unfiltered wipe needs an explicit confirm flag server-side
            // (the dialog above is that confirmation); without it "Delete All
            // Bounces" on "All Bounce Types" always failed with a 400.
            if (!this.typeFilter && !this.campaignFilter) params.set('confirm', 'true');
            const result = await API.del(`/api/bounces?${params}`, API.LONG_TIMEOUT);
            // The unfiltered wipe reports `deleted: null` when the pre-count
            // failed; it still removed everything.
            const deleted = result?.deleted ?? (result?.all ? 'all' : 0);
            const errors = result?.errors ?? 0;
            App.showResult('Delete Bounces — Done', `
                <ul style="line-height:1.8">
                    <li>Deleted: <strong>${deleted}</strong></li>
                    ${errors ? `<li>Errors: <strong>${errors}</strong></li>` : ''}
                </ul>
            `);
            App.selection.clear('bounces');
            this.render();
        } catch (err) {
            App.showResult('Delete Bounces — Failed',
                `<p style="color:#e74c3c">${App.escapeHtml(err.message || 'Unknown error')}</p>`);
        }
    },

    async exportBounces() {
        App.showProgress('Export Bounces', 'Preparing export…');
        try {
            const params = new URLSearchParams();
            if (this.typeFilter) params.set('bounce_type', this.typeFilter);
            if (this.campaignFilter) params.set('campaign_id', String(this.campaignFilter));
            const result = await API.download(`/api/bounces/export?${params}`);
            if (!result.blob) {
                App.showResult('Export Bounces', `<p>No ${this._typeLabel()} to export.</p>`);
                return;
            }
            const typeSuffix = this.typeFilter ? `_${this.typeFilter}` : '';
            const suffix = this.campaignFilter
                ? `_${this.campaigns.find(c => c.id === this.campaignFilter)?.name?.replace(/[^a-zA-Z0-9]/g, '_').substring(0, 30) || this.campaignFilter}${typeSuffix}`
                : (typeSuffix || '_all');
            API.downloadBlob(result.blob, `bounces${suffix}.csv`);
            App.toast('Bounces exported', 'success');
            App.closeModal();
        } catch (err) {
            App.showResult('Export Bounces — Failed',
                `<p style="color:#e74c3c">${App.escapeHtml(err.message || 'Unknown error')}</p>`);
        }
    },

    async ingestBounces() {
        App.showProgress('Ingest New Bounces',
            'Scanning the bounce mailbox (last 30 days, read or unread) for delivery failures not ingested yet, classifying each, and creating matching records in ListMonk. Only truly invalid addresses become hard bounces. Please wait…');
        try {
            const result = await API.post('/api/bounces/ingest', null, API.LONG_TIMEOUT);
            if (result && result.error) {
                App.showResult('Ingest Bounces — Error',
                    `<p style="color:#e74c3c">${App.escapeHtml(result.error)}</p>`);
                return;
            }
            const r = result || {};
            const reasons = r.skipped_reasons || {};
            const reasonList = Object.entries(reasons)
                .map(([k, v]) => `<li>${App.escapeHtml(k)}: <strong>${v}</strong></li>`)
                .join('');
            App.showResult('Ingest Bounces — Done', `
                <ul style="line-height:1.8">
                    <li>Emails scanned: <strong>${r.scanned || 0}</strong></li>
                    <li>Ingested: <strong>${r.ingested || 0}</strong>
                        (hard: <strong>${r.hard || 0}</strong>, soft: <strong>${r.soft || 0}</strong>)</li>
                    <li>Skipped: <strong>${r.skipped || 0}</strong></li>
                    ${reasonList ? `<li>Skip reasons:<ul>${reasonList}</ul></li>` : ''}
                    <li>Errors: <strong>${r.errors || 0}</strong></li>
                    ${r.message ? `<li>${App.escapeHtml(r.message)}</li>` : ''}
                </ul>
            `);
            if (r.ingested > 0) {
                this.render();
            }
        } catch (err) {
            App.showResult('Ingest Bounces — Failed',
                `<p style="color:#e74c3c">${App.escapeHtml(err.message || 'Unknown error')}</p>`);
        }
    },

};

// Initialize on DOM ready
document.addEventListener('DOMContentLoaded', () => App.init());
