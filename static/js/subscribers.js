/**
 * Subscribers page - CRUD, search, import/export, bulk actions.
 */
const Subscribers = {
    page: 1,
    query: '',
    statusFilter: '',
    listFilter: 0,
    perPage: 50,
    _lists: [],
    _rows: [],
    _editing: null,

    async render() {
        const seq = App.seq();
        try {
            // The list filter is a convenience: if the lists call fails, the
            // subscriber table must still render with the other filters.
            try {
                const listsRes = await API.get('/api/lists?per_page=200&minimal=true');
                this._lists = listsRes?.data?.results || [];
            } catch {
                this._lists = [];
            }

            const params = new URLSearchParams({
                page: String(this.page),
                per_page: String(this.perPage),
            });
            // `search` is free text; the backend turns it into a safely
            // escaped ILIKE expression. (ListMonk's own `query` parameter is
            // raw SQL and rejects plain words like "info@acme".)
            if (this.query) params.set('search', this.query);
            if (this.statusFilter) params.set('status', this.statusFilter);
            if (this.listFilter) params.set('list_id', String(this.listFilter));

            const result = await API.get(`/api/subscribers?${params.toString()}`);
            if (App.stale(seq)) return;
            const data = result?.data || {};
            const subscribers = data.results || [];
            const total = data.total || 0;
            this._rows = subscribers;

            const ids = this.pageIds();
            App.selection.register('subscribers', {
                noun: 'subscriber',
                pageValues: ids,
                actions: [
                    { label: 'Add / Remove from list', onclick: 'Subscribers.showBulkLists()' },
                    { label: 'Blocklist', onclick: 'Subscribers.bulkBlocklist()' },
                    { label: 'Export Selected', onclick: 'Subscribers.exportSelected()' },
                    { label: 'Delete', onclick: 'Subscribers.bulkDelete()', danger: true },
                ],
            });

            const listOptions = this._lists.map(l =>
                `<option value="${l.id}" ${l.id === this.listFilter ? 'selected' : ''}>${App.escapeHtml(l.name)}</option>`
            ).join('');

            let html = `
                <div class="search-bar">
                    <div class="search-input">
                        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="11" cy="11" r="8"/><line x1="21" y1="21" x2="16.65" y2="16.65"/></svg>
                        <input type="search" id="subSearch" placeholder="Search subscribers by email or name..."
                            aria-label="Search subscribers by email or name"
                            value="${App.escapeHtml(this.query)}" onkeydown="if(event.key==='Enter')Subscribers.search()">
                    </div>
                    <select id="subListFilter" aria-label="Filter leads by list" style="width:auto;min-width:180px" onchange="Subscribers.filterList(this.value)">
                        <option value="0" ${!this.listFilter ? 'selected' : ''}>All Lists</option>
                        ${listOptions}
                    </select>
                    <select id="subStatusFilter" aria-label="Filter leads by status" style="width:auto" onchange="Subscribers.filterStatus(this.value)">
                        <option value="" ${!this.statusFilter ? 'selected' : ''}>All Statuses</option>
                        <option value="enabled" ${this.statusFilter === 'enabled' ? 'selected' : ''}>Enabled</option>
                        <option value="blocklisted" ${this.statusFilter === 'blocklisted' ? 'selected' : ''}>Blocklisted</option>
                    </select>
                    <button class="btn" onclick="Subscribers.search()">Search</button>
                    <div class="page-actions">
                        <button class="btn" onclick="Subscribers.exportAll()">Export CSV</button>
                        <button class="btn btn-primary" onclick="Subscribers.showCreate()">+ Add Subscriber</button>
                    </div>
                </div>
                ${App.selection.bar('subscribers')}
                <div class="table-wrapper"><table>
                    <thead><tr>
                        <th class="col-select">${App.selection.headbox('subscribers', 'Select all subscribers on this page')}</th>
                        <th>ID</th><th>Email</th><th>Name</th><th>Status</th><th>Lists</th><th>Created</th><th class="cell-actions">Actions</th>
                    </tr></thead><tbody>`;

            if (!subscribers.length) {
                html += '<tr><td colspan="8"><div class="empty-state"><h3>No subscribers found</h3></div></td></tr>';
            }

            subscribers.forEach(s => {
                const lists = (s.lists || []).map(l => `<span class="tag" title="${App.escapeHtml(l.name)}">${App.escapeHtml(l.name)}</span>`).join('') || '-';
                const isBlocked = s.status === 'blocklisted';
                html += `<tr class="${App.selection.has('subscribers', s.id) ? 'row-selected' : ''}">
                    <td class="col-select">${App.selection.rowbox('subscribers', s.id, `Select ${s.email}`)}</td>
                    <td>${s.id}</td>
                    <td><strong>${App.escapeHtml(s.email)}</strong></td>
                    <td>${App.escapeHtml(s.name) || '-'}</td>
                    <td>${App.statusBadge(s.status)}</td>
                    <td>${lists}</td>
                    <td>${App.formatDate(s.created_at)}</td>
                    <td class="cell-actions">
                        <div class="action-btns">
                            <button class="btn btn-sm" onclick="Subscribers.showDetail(${s.id})">View</button>
                            <button class="btn btn-sm" onclick="Subscribers.showEdit(${s.id})">Edit</button>
                            ${isBlocked ? '' : `<button class="btn btn-sm" onclick="Subscribers.blocklist(${s.id})">Blocklist</button>`}
                            <button class="btn btn-sm btn-danger" onclick="Subscribers.remove(${s.id})">Delete</button>
                        </div>
                    </td>
                </tr>`;
            });

            html += '</tbody></table></div>';
            html += App.renderPagination(this.page, total, this.perPage, 'Subscribers.goToPage');
            if (App.stale(seq)) return;
            App.setContent(html);
        } catch {
            if (App.stale(seq)) return;
            App.setContent('<div class="empty-state"><h3>Failed to load subscribers</h3></div>');
        }
    },

    /** IDs (as strings) of the rows currently on screen. */
    pageIds() {
        return this._rows.map(s => String(s.id));
    },

    _selectedIds() {
        return App.selection.values('subscribers').map(Number).filter(Boolean);
    },

    search() {
        this.query = document.getElementById('subSearch')?.value || '';
        this.page = 1;
        this.render();
    },

    filterStatus(val) {
        this.statusFilter = val || '';
        this.page = 1;
        this.render();
    },

    filterList(val) {
        this.listFilter = parseInt(val) || 0;
        this.page = 1;
        this.render();
    },

    goToPage(p) { this.page = p; this.render(); },

    showCreate() {
        App.setContent(`
            <div class="inline-form">
                <h3 style="margin-bottom:16px">Add Subscriber</h3>
                <div class="form-grid">
                    <div class="form-group"><label>Email *</label><input type="email" id="subEmail" placeholder="email@example.com"></div>
                    <div class="form-group"><label>Name</label><input type="text" id="subName" placeholder="Full name"></div>
                </div>
                <div class="form-group">
                    <label>Status</label>
                    <select id="subStatus">
                        <option value="enabled">Enabled</option>
                        <option value="blocklisted">Blocklisted</option>
                    </select>
                </div>
                <div class="form-group">
                    <label>Attributes (JSON)</label>
                    <textarea id="subAttribs" placeholder='{"company": "Acme"}'>{}</textarea>
                </div>
                <div class="form-actions">
                    <button class="btn btn-primary" onclick="Subscribers.create()">Create</button>
                    <button class="btn btn-secondary" onclick="Subscribers.render()">Cancel</button>
                </div>
            </div>
        `);
    },

    async create() {
        const email = document.getElementById('subEmail').value.trim();
        const name = document.getElementById('subName').value.trim();
        const status = document.getElementById('subStatus').value;
        // A typo in the JSON must not silently create the subscriber with
        // empty attributes — surface the error and abort instead.
        let attribs;
        try {
            attribs = JSON.parse(document.getElementById('subAttribs').value || '{}');
        } catch {
            App.toast('Attributes is not valid JSON', 'error');
            return;
        }

        if (!email) { App.toast('Email is required', 'error'); return; }

        try {
            await API.post('/api/subscribers', { email, name, status, attribs, lists: [] });
            App.toast('Subscriber created', 'success');
            this.render();
        } catch {
            // API.request already surfaced the server message.
        }
    },

    async showDetail(id) {
        try {
            const [subRes, bounceRes] = await Promise.allSettled([
                API.get(`/api/subscribers/${id}`),
                API.get(`/api/subscribers/${id}/bounces`),
            ]);
            const s = subRes.status === 'fulfilled' ? (subRes.value?.data || {}) : {};
            if (!s.id) throw new Error('Subscriber not found');

            // ListMonk returns a plain array in `data` for this endpoint (not
            // the paginated {results:[...]} shape), so `.results` was always
            // undefined and the Bounces section never showed any record.
            const rawBounces = bounceRes.status === 'fulfilled' ? bounceRes.value?.data : null;
            const bounces = Array.isArray(rawBounces) ? rawBounces : (rawBounces?.results || []);
            const lists = (s.lists || []).map(l => `<span class="tag" title="${App.escapeHtml(l.name)}">${App.escapeHtml(l.name)}</span>`).join('') || '-';
            const attribs = s.attribs && Object.keys(s.attribs).length
                ? `<pre style="background:var(--bg-input);padding:12px;border-radius:8px;overflow:auto;max-height:240px;font-size:0.8rem">${App.escapeHtml(JSON.stringify(s.attribs, null, 2))}</pre>`
                : '<p style="color:var(--text-muted)">No custom attributes</p>';

            // showResult sets the title via textContent — escaping here would
            // show the entities literally.
            App.showResult(`Subscriber: ${s.email}`, `
                <div style="font-size:0.9rem;line-height:1.7">
                    <p><strong>Name:</strong> ${App.escapeHtml(s.name) || '-'}</p>
                    <p><strong>Status:</strong> ${App.statusBadge(s.status)}</p>
                    <p><strong>Lists:</strong> ${lists}</p>
                    <p><strong>Created:</strong> ${App.formatDateTime(s.created_at)}</p>
                    <p><strong>Updated:</strong> ${App.formatDateTime(s.updated_at)}</p>
                    <p style="margin-top:12px"><strong>Attributes</strong></p>
                    ${attribs}
                    <p style="margin-top:12px"><strong>Bounces (${bounces.length})</strong></p>
                    ${bounces.length ? `<ul>${bounces.slice(0, 20).map(b => `<li>${App.escapeHtml(b.type || '?')} · ${App.escapeHtml(b.source || '')} · ${App.formatDate(b.created_at)}</li>`).join('')}</ul>` : '<p style="color:var(--text-muted)">No bounce records</p>'}
                </div>
            `);
        } catch (err) {
            // Errors thrown locally (e.g. "Subscriber not found") were never
            // toasted by API.request — surface them instead of failing silently.
            if (!err?.handled) App.toast(err?.message || 'Failed to load subscriber', 'error');
        }
    },

    async showEdit(id) {
        const seq = App.seq();
        try {
            const result = await API.get(`/api/subscribers/${id}`);
            if (App.stale(seq)) return;
            const s = result?.data || {};
            // Keep the full record: ListMonk's PUT is a full replace, so the
            // update must echo the subscriber's existing list memberships.
            this._editing = s;

            const currentLists = (s.lists || []).map(l => l.id);
            const listBoxes = this._lists.map(l =>
                `<label class="checkbox-label"><input type="checkbox" name="editSubLists" value="${l.id}" ${currentLists.includes(l.id) ? 'checked' : ''}><span>${App.escapeHtml(l.name)}</span></label>`
            ).join('');
            // Memberships the form cannot show (lists beyond the fetched page)
            // are merged back in from `this._editing` on save — ListMonk's PUT
            // is a full replace, so anything not echoed would be dropped.

            App.setContent(`
                <div class="inline-form">
                    <h3 style="margin-bottom:16px">Edit Subscriber #${s.id}</h3>
                    <div class="form-grid">
                        <div class="form-group"><label>Email</label><input type="email" id="editEmail" value="${App.escapeHtml(s.email || '')}"></div>
                        <div class="form-group"><label>Name</label><input type="text" id="editName" value="${App.escapeHtml(s.name || '')}"></div>
                    </div>
                    <div class="form-group">
                        <label>Status</label>
                        <select id="editStatus">
                            <option value="enabled" ${s.status === 'enabled' ? 'selected' : ''}>Enabled</option>
                            <option value="blocklisted" ${s.status === 'blocklisted' ? 'selected' : ''}>Blocklisted</option>
                        </select>
                    </div>
                    <div class="form-group">
                        <label>Lists</label>
                        <div class="checkbox-group">${listBoxes || '<span style="color:var(--text-muted)">No lists available</span>'}</div>
                    </div>
                    <div class="form-group">
                        <label>Attributes (JSON)</label>
                        <textarea id="editAttribs">${App.escapeHtml(JSON.stringify(s.attribs || {}, null, 2))}</textarea>
                    </div>
                    <div class="form-actions">
                        <button class="btn btn-primary" onclick="Subscribers.update(${s.id})">Update</button>
                        <button class="btn btn-secondary" onclick="Subscribers.render()">Cancel</button>
                    </div>
                </div>
            `);
        } catch {
            // API.request already surfaced the server message.
        }
    },

    async update(id) {
        const email = document.getElementById('editEmail').value.trim();
        const name = document.getElementById('editName').value.trim();
        const status = document.getElementById('editStatus').value;
        // ListMonk's PUT is a full replace: silently substituting {} for a
        // malformed JSON body would wipe the subscriber's stored attributes
        // while reporting success. Abort with the error instead.
        let attribs;
        try {
            attribs = JSON.parse(document.getElementById('editAttribs').value || '{}');
        } catch {
            App.toast('Attributes is not valid JSON', 'error');
            return;
        }

        const boxes = Array.from(document.querySelectorAll('input[name="editSubLists"]'));
        const checked = boxes.filter(cb => cb.checked).map(cb => parseInt(cb.value));

        try {
            // ListMonk's PUT /api/subscribers/{id} replaces the whole record:
            // omitting `lists` removes the subscriber from every list. Honor
            // the checkboxes the form showed, and additionally echo any
            // stored membership the form could NOT show (no list boxes at
            // all, or a list beyond the fetched page) so an edit can never
            // silently unsubscribe the subscriber from it.
            const renderedIds = new Set(boxes.map(cb => parseInt(cb.value)));
            let stored;
            if (this._editing && this._editing.id === id) {
                stored = (this._editing.lists || []).map(l => l.id);
            } else {
                const res = await API.get(`/api/subscribers/${id}`);
                stored = ((res?.data?.lists) || []).map(l => l.id);
            }
            const hidden = stored.filter(lid => !renderedIds.has(lid));
            const lists = [...checked, ...hidden];

            await API.put(`/api/subscribers/${id}`, { email, name, status, attribs, lists });
            App.toast('Subscriber updated', 'success');
            this.render();
        } catch {
            // API.request already surfaced the server message.
        }
    },

    async remove(id) {
        if (await App.confirm('Delete Subscriber', 'Are you sure you want to delete this subscriber?')) {
            try {
                await API.del(`/api/subscribers/${id}`);
                App.toast('Subscriber deleted', 'success');
                App.selection.clear('subscribers');
                this.render();
            } catch {
                // API.request already surfaced the server message.
            }
        }
    },

    async blocklist(id) {
        try {
            await API.put(`/api/subscribers/${id}/blocklist`);
            App.toast('Subscriber blocklisted', 'success');
            this.render();
        } catch {
            // API.request already surfaced the server message.
        }
    },

    // ── Bulk actions ─────────────────────────────────────
    async bulkBlocklist() {
        const ids = this._selectedIds();
        if (!ids.length) return;
        if (!await App.confirm('Blocklist Subscribers',
                `Blocklist ${ids.length} selected subscriber(s)? They will stop receiving all campaigns.`)) return;
        try {
            await API.put('/api/subscribers/blocklist', { ids });
            App.toast(`${ids.length} subscriber(s) blocklisted`, 'success');
            App.selection.clear('subscribers');
            this.render();
        } catch {
            // API.request already surfaced the server message.
        }
    },

    async bulkDelete() {
        const ids = this._selectedIds();
        if (!ids.length) return;
        if (!await App.confirm('Delete Subscribers',
                `Permanently delete ${ids.length} selected subscriber(s)? This cannot be undone.`)) return;
        try {
            const params = ids.map(id => `ids=${id}`).join('&');
            await API.del(`/api/subscribers?${params}`);
            App.toast(`${ids.length} subscriber(s) deleted`, 'success');
            App.selection.clear('subscribers');
            this.render();
        } catch {
            // API.request already surfaced the server message.
        }
    },

    async showBulkLists() {
        const ids = this._selectedIds();
        if (!ids.length) return;
        if (!this._lists.length) { App.toast('No lists available', 'error'); return; }

        const options = this._lists.map(l =>
            `<option value="${l.id}">${App.escapeHtml(l.name)}</option>`
        ).join('');

        App.showResult('Add / Remove from List', `
            <p style="color:var(--text-secondary)">Apply to <strong>${ids.length}</strong> selected subscriber(s):</p>
            <div class="form-group" style="margin-top:12px"><label>List</label><select id="bulkListSelect">${options}</select></div>
            <div class="form-actions">
                <button class="btn btn-primary" onclick="Subscribers.applyBulkLists('add')">Subscribe to list</button>
                <button class="btn" onclick="Subscribers.applyBulkLists('remove')">Unsubscribe from list</button>
            </div>
        `);
    },

    async applyBulkLists(action) {
        const ids = this._selectedIds();
        const listId = parseInt(document.getElementById('bulkListSelect')?.value);
        if (!ids.length || !listId) return;

        try {
            const payload = { ids, action, target_list_ids: [listId] };
            if (action === 'add') payload.status = 'confirmed';
            await API.put('/api/subscribers/lists', payload);
            App.toast(`${action === 'add' ? 'Subscribed' : 'Unsubscribed'} ${ids.length} subscriber(s)`, 'success');
            App.closeModal();
            App.selection.clear('subscribers');
            this.render();
        } catch {
            // API.request already surfaced the server message.
        }
    },

    /** CSV of the selected rows that are loaded on this page. */
    exportSelected() {
        const selected = this._selectedIds();
        const rows = this._rows.filter(s => selected.includes(s.id));
        if (!rows.length) { App.toast('No selected rows on this page', 'error'); return; }

        const header = ['id', 'email', 'name', 'status', 'lists', 'created_at'];
        const csvRows = rows.map(s => [
            s.id,
            s.email,
            s.name || '',
            s.status,
            (s.lists || []).map(l => l.name).join('; '),
            s.created_at || '',
        ]);
        const csv = [header, ...csvRows]
            .map(cols => cols.map(v => App.csvCell(v)).join(','))
            .join('\n');

        API.downloadBlob(new Blob([csv], { type: 'text/csv' }), 'selected_subscribers.csv');
        App.toast(`Exported ${rows.length} row(s)`, 'success');
    },

    async exportAll() {
        try {
            const params = new URLSearchParams();
            if (this.query) params.set('search', this.query);
            if (this.statusFilter) params.set('status', this.statusFilter);
            if (this.listFilter) params.set('list_id', String(this.listFilter));
            const qs = params.toString();
            const result = await API.get(`/api/subscribers/export-all${qs ? '?' + qs : ''}`);
            API.downloadBlob(result.blob, 'subscribers_export.csv');
            App.toast('Export downloaded', 'success');
        } catch {
            // API.request already surfaced the server message.
        }
    },
};