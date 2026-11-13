/**
 * Unsubscribe records — no page of their own.
 *
 * Records are browsed per campaign: `mount()` renders a campaign's records
 * panel into a container on another page (Analytics → the selected campaign).
 * The monitor (scan, export all, undo all) and the records no campaign view
 * can show (never attributed, or their campaign was deleted) live in
 * Settings → Automation (`mountMonitor()` / `mountOrphans()`).
 *
 * One records panel exists at a time; its row selection is the shared
 * `App.selection` key below, so it gets the standard bulk bar.
 */
const Unsubscribes = {
    SEL: 'unsub_records',
    PER_PAGE: 25,
    settings: { blocklist_enabled: false },

    /** The mounted records panel: {containerId, campaignId, title, page, data, seq}. */
    panel: null,

    /* ── Helpers ──────────────────────────────────────────── */
    /** Strip a leading date prefix from a raw ListMonk campaign name. */
    cleanCampaignName(name) {
        if (!name || name === 'Unknown' || name === 'No matching campaign') return 'Unattributed (no campaign matched)';
        const cleaned = name.replace(/^\d{4}-?\d{2}-?\d{2}\s*/, '').trim();
        return cleaned || name;
    },

    /** Render a source badge for a log record. */
    renderSourceBadge(record) {
        const source = record.source || 'email';
        if (source === 'link') {
            return '<span class="badge badge-primary" title="Clicked unsubscribe link">link click</span>';
        }
        const kw = App.escapeHtml(record.keyword || 'email reply');
        return `<span class="badge badge-warning" title="${kw}">email reply</span>`;
    },

    /** A campaign id as a JS literal for inline handlers: campaign ids are
        numbers, the unattributed group is the string 'unknown'. */
    _ref(campaignId) {
        return Number.isInteger(campaignId) ? String(campaignId) : "'unknown'";
    },

    /** Old links (#/unsubscribes?campaign=6) land on the new homes. */
    render() {
        const raw = App.routeQuery().get('campaign') || '';
        const id = parseInt(raw, 10);
        window.location.replace(Number.isInteger(id) && id > 0
            ? `#/analytics?campaign=${id}&section=unsubscribes`
            : '#/settings?tab=automation');
    },

    /* ── Records panel (one campaign) ─────────────────────── */
    /** Render the records of `campaignId` (a number or 'unknown') into
        `containerId`. `title` overrides the card heading. */
    async mount(containerId, campaignId, { title = 'Unsubscribes' } = {}) {
        const same = this.panel && this.panel.containerId === containerId
            && this.panel.campaignId === campaignId;
        if (!same) App.selection.clear(this.SEL);
        this.panel = {
            containerId, campaignId, title,
            page: same ? this.panel.page : 1,
            data: same ? this.panel.data : null,
            seq: (this.panel?.seq || 0) + 1,
        };
        const el = document.getElementById(containerId);
        if (!el) return;
        // A host re-render for the same campaign repaints at once, then refreshes.
        if (this.panel.data) this._paint();
        else el.innerHTML = '<div class="loading-spinner">Loading unsubscribes…</div>';
        await this._load();
    },

    async _load() {
        const p = this.panel;
        if (!p) return;
        const seq = ++p.seq;
        let data;
        try {
            const res = await API.get(`/api/unsubscribes/campaign/${p.campaignId}?page=${p.page}&per_page=${this.PER_PAGE}`, true);
            data = res.data || { results: [], total: 0 };
        } catch {
            data = null;
        }
        // A newer mount / page change owns the container now.
        if (this.panel !== p || seq !== p.seq) return;
        p.data = data;
        // The last row of a page was removed: step back instead of an empty page.
        if (data && !data.results?.length && data.total > 0 && p.page > 1) {
            p.page = Math.max(1, Math.ceil(data.total / this.PER_PAGE));
            await this._load();
            return;
        }
        this._paint();
    },

    _paint() {
        const p = this.panel;
        const el = p && document.getElementById(p.containerId);
        if (!el) return;
        if (!p.data) {
            el.innerHTML = `
                <div class="card-header"><h2 class="card-title">${App.escapeHtml(p.title)}</h2></div>
                <div class="empty-state" style="padding:24px"><p>Unsubscribe records could not be loaded.</p>
                    <button class="btn btn-sm" onclick="Unsubscribes._load()">Try again</button></div>`;
            return;
        }
        const d = p.data;
        const records = d.results || [];
        const total = d.total || 0;
        const ref = this._ref(p.campaignId);

        App.selection.register('unsub_records', {
            noun: 'record',
            pageValues: records.map(r => r.email).filter(Boolean),
            actions: [
                { label: 'Export Selected', onclick: 'Unsubscribes.exportSelected()' },
                { label: 'Remove', onclick: 'Unsubscribes.removeSelected()', danger: true },
            ],
        });

        const rows = records.map(r => `
            <tr class="${App.selection.has(this.SEL, r.email) ? 'row-selected' : ''}">
                <td>${App.selection.rowbox(this.SEL, r.email || '', `Select ${r.email || ''}`)}</td>
                <td><span class="cell-email">${App.escapeHtml(r.email || '-')}</span></td>
                <td>${App.escapeHtml(r.name || '-')}</td>
                <td>${this.renderSourceBadge(r)}</td>
                <td>${App.formatDateTime(r.timestamp)}</td>
                <td><button class="btn btn-sm" onclick="Unsubscribes.removeRecord(${App.escapeHtml(JSON.stringify(r.email || ''))})">Remove</button></td>
            </tr>`).join('');

        el.innerHTML = `
            <div class="card-header">
                <h2 class="card-title">${App.escapeHtml(p.title)}</h2>
                <div class="page-actions">
                    <button class="btn btn-sm" onclick="Unsubscribes.exportCampaignCSV(${ref})" ${total ? '' : 'disabled'}>Export CSV</button>
                    ${App.moreMenu([
                        { label: 'Remove all records…', onclick: 'Unsubscribes.removeAll()', danger: true, hidden: !total },
                        { label: 'Unsubscribe settings', onclick: "location.hash='#/settings?tab=automation'" },
                    ], 'More unsubscribe actions')}
                </div>
            </div>
            <div style="display:flex;flex-wrap:wrap;gap:8px 20px;font-size:0.85rem;color:var(--text-secondary);margin-bottom:12px">
                <span><strong style="color:var(--text-primary)">${App.formatNumber(total)}</strong> unsubscribe${total === 1 ? '' : 's'}</span>
                <span>${App.formatNumber(d.email_count || 0)} email repl${(d.email_count || 0) === 1 ? 'y' : 'ies'}</span>
                <span>${App.formatNumber(d.link_count || 0)} link click${(d.link_count || 0) === 1 ? '' : 's'}</span>
            </div>
            ${total ? `
            ${App.selection.bar('unsub_records')}
            <div class="table-wrapper" tabindex="0" role="region" aria-label="Unsubscribe records"><table>
                <thead><tr>
                    <th style="width:36px">${App.selection.headbox(this.SEL, 'Select all records on this page')}</th>
                    <th>Email</th><th>Name</th><th>Type</th><th>Date</th><th></th>
                </tr></thead>
                <tbody>${rows}</tbody>
            </table></div>
            ${App.renderPagination(p.page, total, this.PER_PAGE, 'Unsubscribes.goToPage')}
            <p class="field-hint" style="margin-top:10px">Removing a record only clears it from this log: the contact stays unsubscribed in ListMonk.</p>`
            : '<div class="empty-state" style="padding:24px"><p>No unsubscribes recorded for this campaign.</p></div>'}`;
        App.labelControls?.(el);
    },

    async goToPage(page) {
        if (!this.panel) return;
        this.panel.page = page;
        await this._load();
        document.getElementById(this.panel.containerId)?.scrollIntoView({ block: 'start', behavior: 'smooth' });
    },

    /** Query suffix limiting a record delete to the panel's campaign: an
        address that unsubscribed from several campaigns keeps the others. */
    _campaignScope() {
        const cid = this.panel?.campaignId;
        if (Number.isInteger(cid)) return `&campaign_id=${cid}`;
        return cid === 'unknown' ? '&campaign_id=unknown' : '';
    },

    /** Something on the host page shows counts derived from the log. */
    _changed() {
        document.dispatchEvent(new CustomEvent('unsubscribes:changed'));
    },

    async _removeEmails(emails) {
        const params = emails.map(e => `emails=${encodeURIComponent(e)}`).join('&');
        const res = await API.del(`/api/unsubscribes/records?${params}${this._campaignScope()}`);
        emails.forEach(e => App.selection.set(this.SEL).delete(String(e)));
        App.selection.sync(this.SEL);
        return res;
    },

    async removeRecord(email) {
        if (!email) return;
        if (!await App.confirm('Remove Record',
                `Remove "${email}" from this campaign's unsubscribe log? The contact stays unsubscribed.`)) return;
        try {
            await this._removeEmails([email]);
            App.toast(`Removed ${email}`, 'success');
            await this._load();
            this._changed();
        } catch {
            // API.request already surfaced the server message.
        }
    },

    async removeSelected() {
        const emails = App.selection.values(this.SEL);
        if (!emails.length) return;
        if (!await App.confirm('Remove Records',
                `Remove ${emails.length} selected record(s) from the unsubscribe log? The contacts stay unsubscribed.`)) return;
        try {
            const res = await this._removeEmails(emails);
            App.toast(res?.message || `Removed ${emails.length} record(s)`, 'success');
            await this._load();
            this._changed();
        } catch {
            // API.request already surfaced the server message.
        }
    },

    async removeAll() {
        const p = this.panel;
        if (!p) return;
        if (!await App.confirm('Remove All Records',
                'Remove ALL unsubscribe records of this campaign from the log? The contacts stay unsubscribed. This cannot be undone.')) return;
        try {
            const res = await API.del(`/api/unsubscribes/campaign/${p.campaignId}`);
            App.toast(res?.message || 'Records removed', 'success');
            App.selection.clear(this.SEL);
            p.page = 1;
            await this._load();
            this._changed();
        } catch {
            // API.request already surfaced the server message.
        }
    },

    /** CSV of the selected rows loaded on this page (rule: App.csvCell). */
    exportSelected() {
        const wanted = new Set(App.selection.values(this.SEL));
        const rows = (this.panel?.data?.results || []).filter(r => wanted.has(String(r.email)));
        if (!rows.length) { App.toast('Only rows on the current page can be exported', 'info'); return; }
        const cols = ['email', 'name', 'source', 'keyword', 'campaign_name', 'timestamp'];
        const csv = [cols.join(','), ...rows.map(r => cols.map(c => App.csvCell(r[c] ?? '')).join(','))].join('\n');
        API.downloadBlob(new Blob([csv], { type: 'text/csv' }), 'unsubscribes_selected.csv');
    },

    async exportCampaignCSV(campaignId) {
        try {
            const result = await API.download(`/api/unsubscribes/campaign/${campaignId}/export`);
            if (result.blob) {
                API.downloadBlob(result.blob, `unsubscribes_campaign_${campaignId}.csv`);
                App.toast('Unsubscribes exported', 'success');
            }
        } catch {
            // API.request already surfaced the server message.
        }
    },

    /* ── Monitor card (Settings → Automation) ─────────────── */
    async mountMonitor(containerId) {
        const el = document.getElementById(containerId);
        if (!el) return;
        const [statsRes, imapRes] = await Promise.allSettled([
            API.get('/api/unsubscribes/stats', true),
            API.get('/api/unsubscribes/imap-status', true),
        ]);
        const target = document.getElementById(containerId);
        if (!target) return;
        const stats = statsRes.status === 'fulfilled' ? statsRes.value : null;
        const imap = imapRes.status === 'fulfilled' ? imapRes.value : { configured: false, connected: false };
        let badge;
        if (!imap.configured) badge = '<span class="badge badge-default">IMAP not configured</span>';
        else if (imap.connected) badge = '<span class="badge badge-success">IMAP connected</span>';
        else badge = `<span class="badge badge-danger">IMAP error: ${App.escapeHtml(imap.error || 'unknown')}</span>`;

        target.innerHTML = `
            <div class="card-header">
                <h2 class="card-title">Unsubscribe monitor</h2>
                <div class="page-actions">
                    ${badge}
                    <button class="btn btn-sm btn-primary" id="unsubScanBtn" onclick="Unsubscribes.triggerScan()">Scan now</button>
                    ${App.moreMenu([
                        { label: 'Export all (CSV)', onclick: 'Unsubscribes.exportAllCSV()', hidden: !stats?.total },
                        { label: 'Undo all unsubscribes…', onclick: 'Unsubscribes.resetAll()', danger: true, hidden: !stats?.total },
                    ], 'More unsubscribe actions')}
                </div>
            </div>
            <div class="unsub-monitor-info">
                <div><span style="color:var(--text-muted)">Scan:</span> <strong>every hour</strong>
                    <span style="color:var(--text-muted);font-size:0.75rem">(inbox replies + unsubscribe-link clicks)</span></div>
                <div>
                    <span style="color:var(--text-muted)">Keywords:</span>
                    <code style="font-size:0.8rem">"Remove me"</code>,
                    <code style="font-size:0.8rem">"Unsubscribe me"</code>,
                    <code style="font-size:0.8rem">"Stop emailing me"</code>
                    <details class="inline-details">
                        <summary>and many more</summary>
                        <div class="inline-details-body">
                            <p>Any plain instruction to stop the mail, aimed at the sender, for example
                                <code>remove us</code>, <code>please unsubscribe</code>, <code>take us off your list</code>,
                                <code>do not contact us</code>, <code>no further contact</code>, or a reply that is just
                                <code>STOP</code> / <code>Unsubscribe</code>.</p>
                            <p>The same requests in German, French, Spanish, Italian, Portuguese, Dutch, Polish, Danish, Norwegian and Swedish are recognised too.</p>
                            <p>Not acted on: refusals such as "not interested", footer text, quoted earlier mail, and automatic replies (out-of-office, ticket systems).</p>
                        </div>
                    </details>
                </div>
                ${stats ? `<div><span style="color:var(--text-muted)">Recorded:</span>
                    <strong>${App.formatNumber(stats.total || 0)}</strong>
                    <span style="color:var(--text-muted)">(${App.formatNumber(stats.today || 0)} today · ${App.formatNumber(stats.this_week || 0)} this week)</span></div>` : ''}
            </div>
            <p class="field-hint" style="margin-top:10px">Each campaign's unsubscribes are listed on
                <a href="#/analytics">Analytics</a> under the campaign.</p>`;
    },

    /* ── Records with no campaign view (Settings → Automation) ── */
    async mountOrphans(containerId) {
        const el = document.getElementById(containerId);
        if (!el) return;
        let groups;
        try {
            groups = (await API.get('/api/unsubscribes/orphans', true))?.data || [];
        } catch {
            const t = document.getElementById(containerId);
            if (t) t.innerHTML = '<p class="field-hint">Unsubscribes without a campaign could not be checked (ListMonk unreachable).</p>';
            return;
        }
        const target = document.getElementById(containerId);
        if (!target) return;
        if (!groups.length) { target.innerHTML = ''; target.style.display = 'none'; return; }
        target.style.display = '';
        this._orphans = groups;
        const label = g => g.reason === 'unattributed'
            ? 'No campaign matched'
            : `${this.cleanCampaignName(g.campaign_name)} (deleted campaign)`;
        target.innerHTML = `
            <div class="card-header"><h2 class="card-title">Unsubscribes without a campaign</h2></div>
            <p class="field-hint" style="margin-bottom:12px">These records belong to no campaign that Analytics can show:
                the request could not be matched to a campaign, or its campaign was deleted.</p>
            <div class="deliv-seg" role="group" aria-label="Group">
                ${groups.map((g, i) => `<button type="button" class="deliv-seg-btn" data-orphan="${i}"
                    onclick="Unsubscribes.openOrphan(${i})">${App.escapeHtml(label(g))} · ${App.formatNumber(g.count)}</button>`).join('')}
            </div>
            <div id="unsubOrphanPanel" style="margin-top:16px"></div>`;
        // A refresh (after a removal or scan) stays on the open group.
        const open = this.panel?.containerId === 'unsubOrphanPanel'
            ? groups.findIndex(g => g.campaign_id === this.panel.campaignId) : -1;
        this.openOrphan(Math.max(0, open));
    },

    openOrphan(i) {
        const g = (this._orphans || [])[i];
        if (!g) return;
        document.querySelectorAll('[data-orphan]').forEach(b =>
            b.classList.toggle('is-active', b.dataset.orphan === String(i)));
        this.mount('unsubOrphanPanel', g.campaign_id, {
            title: g.reason === 'unattributed' ? 'No campaign matched' : this.cleanCampaignName(g.campaign_name),
        });
    },

    /* ── Global actions ───────────────────────────────────── */
    async exportAllCSV() {
        try {
            const result = await API.download('/api/unsubscribes/export');
            if (result.blob) {
                API.downloadBlob(result.blob, 'unsubscribes_all.csv');
                App.toast('All unsubscribes exported', 'success');
            }
        } catch {
            // API.request already surfaced the server message (404 when empty).
        }
    },

    async triggerScan() {
        const btn = document.getElementById('unsubScanBtn');
        if (btn) { btn.disabled = true; btn.textContent = 'Scanning…'; }
        App.toast('Scanning inbox…', 'info');
        try {
            const result = await API.post('/api/unsubscribes/scan', {}, API.LONG_TIMEOUT);
            const imapData = result.imap || result;  // supports both old and new format
            const linkData = result.link || {};
            const totalProcessed = (imapData.processed || 0) + (linkData.processed || 0);
            App.toast(`Scanned ${imapData.scanned || 0} emails + ${linkData.scanned_lists || 0} lists — ${totalProcessed} unsubscribed`,
                totalProcessed > 0 ? 'success' : 'info');
            this._changed();
        } catch {
            // API.request already toasted the error; nothing more to do.
        } finally {
            const b = document.getElementById('unsubScanBtn');
            if (b) { b.disabled = false; b.textContent = 'Scan now'; }
        }
    },

    async toggleBlocklist(enabled) {
        try {
            await API.put('/api/unsubscribes/settings', { blocklist_enabled: enabled });
            this.settings.blocklist_enabled = enabled;
            App.toast(`Action set to: ${enabled ? 'Unsubscribe + Blocklist' : 'Unsubscribe Only'}`, 'success');
            return true;
        } catch {
            // API.request already surfaced the server message.
            this.settings.blocklist_enabled = !enabled;
            return false;
        }
    },

    async resetAll() {
        if (!await App.confirm('Undo All Unsubscribes',
            'This will UNDO all unsubscribes — re-enable and re-subscribe all processed leads in ListMonk, then remove restored records from the log. Continue?')) return;
        App.toast('Resetting all unsubscribes…', 'info');
        try {
            const result = await API.post('/api/unsubscribes/reset', null, API.LONG_TIMEOUT);
            App.toast(result.message || 'Reset complete', 'success');
            this._changed();
        } catch {
            // API.request already surfaced the server message.
        }
    },
};
