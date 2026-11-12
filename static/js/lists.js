/**
 * Lists page - CRUD, search.
 */
const Lists = {
    page: 1,
    query: '',
    perPage: 50,
    _rows: [],

    async render() {
        const seq = App.seq();
        // Deep-linkable state: #/lists?page=2&q=aevora
        const rq = App.routeQuery();
        if (rq.get('page')) this.page = Math.max(1, parseInt(rq.get('page'), 10) || 1);
        if (rq.get('q') !== null) this.query = rq.get('q') || '';
        try {
            // Free text goes through the backend's safe ILIKE builder
            // (`search`); `query` is ListMonk's raw-SQL passthrough.
            const params = `page=${this.page}&per_page=${this.perPage}${this.query ? '&search=' + encodeURIComponent(this.query) : ''}`;
            const result = await API.get(`/api/lists?${params}`);
            if (App.stale(seq)) return;
            const data = result?.data || {};
            const lists = data.results || [];
            const total = data.total || 0;
            // A bulk delete can strand the view past the last page; clamp and
            // re-fetch instead of painting a false "no lists" page.
            const clamped = App.clampPage(this.page, total, this.perPage);
            if (clamped !== this.page) { this.page = clamped; return this.render(); }
            this._rows = lists;

            const subsCap = App.term('subscriber', { plural: true, cap: true });
            const listNoun = App.term('list');
            const listNouns = App.term('list', { plural: true });
            App.selection.register('lists', {
                noun: listNoun,
                pageValues: this.pageIds(),
                actions: [
                    { label: `Export ${subsCap}`, onclick: 'Lists.bulkExport()' },
                    { label: 'Delete', onclick: 'Lists.bulkDelete()', danger: true },
                ],
            });

            let html = `
                <div class="search-bar">
                    <div class="search-input">
                        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="11" cy="11" r="8"/><line x1="21" y1="21" x2="16.65" y2="16.65"/></svg>
                        <input type="search" id="listSearch" placeholder="Search ${listNouns}…"
                            aria-label="Search ${listNouns}"
                            value="${App.escapeHtml(this.query)}" oninput="Lists.searchLive()" onkeydown="if(event.key==='Enter')Lists.search()">
                    </div>
                    <span class="search-count">${App.formatNumber(total)} ${total === 1 ? listNoun : listNouns}</span>
                    <div class="page-actions">
                        <button class="btn btn-primary" onclick="Lists.showCreate()">+ New ${App.term('list', { cap: true })}</button>
                    </div>
                </div>
                ${App.selection.bar('lists')}
                <div class="table-wrapper" tabindex="0" role="region" aria-label="Scrollable data table"><table>
                    <thead><tr>
                        <th class="col-select">${App.selection.headbox('lists', 'Select all lists on this page')}</th>
                        <th>ID</th><th>Name</th><th>${subsCap}</th><th>Type</th><th>Optin</th><th>Status</th><th>Created</th><th class="cell-actions">Actions</th>
                    </tr></thead><tbody>`;

            if (!lists.length) {
                html += `<tr><td colspan="9"><div class="empty-state"><h2>No ${listNouns} found</h2>
                    ${this.query ? '<p>Try a different search.</p>' : `<p><button class="btn btn-primary" onclick="Lists.showCreate()">+ Create a ${listNoun}</button></p>`}</div></td></tr>`;
            }

            lists.forEach(l => {
                const tags = (l.tags || []).map(t => `<span class="tag">${App.escapeHtml(t)}</span>`).join('') || '';
                html += `<tr class="${App.selection.has('lists', l.id) ? 'row-selected' : ''}">
                    <td class="col-select">${App.selection.rowbox('lists', l.id, `Select ${l.name}`)}</td>
                    <td>${l.id}</td>
                    <td><a class="cell-link" href="javascript:void(0)" onclick="Lists.viewSubscribers(${l.id})" title="Show the ${App.term('subscriber', { plural: true })} on this ${listNoun}">${App.escapeHtml(l.name)}</a> ${tags}</td>
                    <td><a href="javascript:void(0)" onclick="Lists.viewSubscribers(${l.id})">${App.formatNumber(l.subscriber_count || 0)}</a></td>
                    <td><span class="badge badge-${l.type === 'public' ? 'info' : 'default'}">${App.escapeHtml(l.type || '')}</span></td>
                    <td>${App.escapeHtml(l.optin || '')}</td>
                    <td>${App.statusBadge(l.status || 'active')}</td>
                    <td>${App.formatDate(l.created_at)}</td>
                    <td class="cell-actions">
                        <div class="action-btns">
                            <button class="btn btn-sm" onclick="Lists.viewSubscribers(${l.id})">View ${subsCap}</button>
                            <button class="btn btn-sm" onclick="Lists.showEdit(${l.id})">Edit</button>
                            ${App.moreMenu([
                                { label: `Export ${subsCap} (CSV)`, onclick: `Lists.exportSubscribers(${l.id})` },
                                { label: 'Delete…', onclick: `Lists.remove(${l.id})`, danger: true },
                            ], `More actions for ${l.name}`)}
                        </div>
                    </td>
                </tr>`;
            });

            html += '</tbody></table></div>';
            html += App.renderPagination(this.page, total, this.perPage, 'Lists.goToPage');
            if (App.stale(seq)) return;
            App.syncUrl('lists', { page: this.page, q: this.query });
            const keepFocus = document.activeElement?.id === 'listSearch';
            App.setContent(html);
            if (keepFocus) {
                const box = document.getElementById('listSearch');
                if (box) { box.focus(); box.setSelectionRange(box.value.length, box.value.length); }
            }
        } catch {
            if (App.stale(seq)) return;
            App.setContent(`<div class="empty-state"><h2>Failed to load ${App.term('list', { plural: true })}</h2></div>`);
        }
    },

    searchLive: App.debounce(() => Lists.search(), 400),

    /** IDs (as strings) of the rows currently on screen. */
    pageIds() {
        return this._rows.map(l => String(l.id));
    },

    _selectedIds() {
        return App.selection.values('lists').map(Number).filter(Boolean);
    },

    /** Drop cached list dropdowns on other pages after a list mutation. */
    _invalidateListCaches(deletedIds = []) {
        if (typeof Subscribers !== 'undefined') {
            Subscribers._listsCache = null;
            if (deletedIds.includes(Subscribers.listFilter)) {
                Subscribers.listFilter = 0;
            }
        }
        if (typeof Validation !== 'undefined') {
            Validation.lists = null;
        }
    },

    search() {
        this.query = document.getElementById('listSearch')?.value || '';
        this.page = 1;
        App.bumpSeq();
        this.render();
    },

    goToPage(p) { this.page = p; App.bumpSeq(); this.render(); },

    /** Jump to the subscriber table pre-filtered to this list. */
    viewSubscribers(id) {
        Subscribers.listFilter = id;
        Subscribers.statusFilter = '';
        Subscribers.query = '';
        Subscribers.page = 1;
        App.selection.clear('subscribers');
        window.location.hash = '#/subscribers';
    },

    async exportSubscribers(id) {
        try {
            const result = await API.download(`/api/subscribers/export-all?list_id=${id}`);
            API.downloadBlob(result.blob, `list_${id}_subscribers.csv`);
            App.toast('List subscribers exported', 'success');
        } catch {
            // API.request already surfaced the server message (404 when empty).
        }
    },

    showCreate() {
        App.setContent(`
            <div class="inline-form">
                <h2 style="margin-bottom:16px">Create List</h2>
                <div class="form-grid">
                    <div class="form-group"><label>Name *</label><input type="text" id="listName" placeholder="List name"></div>
                    <div class="form-group">
                        <label>Type</label>
                        <select id="listType">
                            <option value="private">Private</option>
                            <option value="public">Public</option>
                        </select>
                    </div>
                    <div class="form-group">
                        <label>Optin</label>
                        <select id="listOptin">
                            <option value="single">Single</option>
                            <option value="double">Double</option>
                        </select>
                    </div>
                </div>
                <div class="form-group"><label>Description</label><textarea id="listDesc" placeholder="Optional description"></textarea></div>
                <div class="form-group"><label>Tags (comma-separated)</label><input type="text" id="listTags" placeholder="tag1, tag2"></div>
                <div class="form-actions">
                    <button class="btn btn-primary" onclick="Lists.create()">Create</button>
                    <button class="btn btn-secondary" onclick="Lists.render()">Cancel</button>
                </div>
            </div>
        `);
    },

    async create() {
        const name = document.getElementById('listName').value.trim();
        const type = document.getElementById('listType').value;
        const optin = document.getElementById('listOptin').value;
        const description = document.getElementById('listDesc').value.trim();
        const tags = document.getElementById('listTags').value.split(',').map(t => t.trim()).filter(Boolean);

        if (!name) { App.toast('Name is required', 'error'); return; }

        try {
            await API.post('/api/lists', { name, type, optin, description, tags });
            this._invalidateListCaches();
            App.toast('List created', 'success');
            this.render();
        } catch {
            // API.request already surfaced the server message.
        }
    },

    async showEdit(id) {
        const seq = App.seq();
        try {
            const result = await API.get(`/api/lists/${id}`);
            if (App.stale(seq)) return;
            const l = result?.data || {};

            App.setContent(`
                <div class="inline-form">
                    <h2 style="margin-bottom:16px">Edit List #${l.id}</h2>
                    <div class="form-grid">
                        <div class="form-group"><label>Name</label><input type="text" id="editListName" value="${App.escapeHtml(l.name || '')}"></div>
                        <div class="form-group">
                            <label>Type</label>
                            <select id="editListType">
                                <option value="private" ${l.type === 'private' ? 'selected' : ''}>Private</option>
                                <option value="public" ${l.type === 'public' ? 'selected' : ''}>Public</option>
                            </select>
                        </div>
                        <div class="form-group">
                            <label>Optin</label>
                            <select id="editListOptin">
                                <option value="single" ${l.optin === 'single' ? 'selected' : ''}>Single</option>
                                <option value="double" ${l.optin === 'double' ? 'selected' : ''}>Double</option>
                            </select>
                        </div>
                    </div>
                    <div class="form-group"><label>Description</label><textarea id="editListDesc">${App.escapeHtml(l.description || '')}</textarea></div>
                    <div class="form-group"><label>Tags</label><input type="text" id="editListTags" value="${App.escapeHtml((l.tags || []).join(', '))}"></div>
                    <div class="form-actions">
                        <button class="btn btn-primary" onclick="Lists.update(${l.id})">Update</button>
                        <button class="btn btn-secondary" onclick="Lists.render()">Cancel</button>
                    </div>
                </div>
            `);
        } catch {
            // API.request already surfaced the server message.
        }
    },

    async update(id) {
        const name = document.getElementById('editListName').value.trim();
        const type = document.getElementById('editListType').value;
        const optin = document.getElementById('editListOptin').value;
        const description = document.getElementById('editListDesc').value.trim();
        const tags = document.getElementById('editListTags').value.split(',').map(t => t.trim()).filter(Boolean);

        // Same rule as create(): an emptied name otherwise reaches ListMonk
        // and comes back as a generic validation error.
        if (!name) { App.toast('Name is required', 'error'); return; }

        try {
            await API.put(`/api/lists/${id}`, { name, type, optin, description, tags });
            this._invalidateListCaches();
            App.toast('List updated', 'success');
            this.render();
        } catch {
            // API.request already surfaced the server message.
        }
    },

    async remove(id) {
        if (await App.confirm('Delete List', 'Are you sure you want to delete this list? All subscribers in this list (except blocklisted ones) will also be deleted.')) {
            try {
                await API.del(`/api/lists/${id}`);
                this._invalidateListCaches([id]);
                App.toast('List deleted', 'success');
                App.selection.clear('lists');
                this.render();
            } catch {
                // API.request already surfaced the server message.
            }
        }
    },

    // ── Bulk actions ─────────────────────────────────────
    async bulkDelete() {
        const ids = this._selectedIds();
        if (!ids.length) return;
        if (!await App.confirm('Delete Lists',
                `Permanently delete ${ids.length} selected list(s)? All subscribers in those lists (except blocklisted ones) will also be deleted.`)) return;
        try {
            const params = ids.map(id => `ids=${id}`).join('&');
            const result = await API.del(`/api/lists?${params}`, API.LONG_TIMEOUT);
            const errors = result?.errors || 0;
            this._invalidateListCaches(ids);
            App.toast(`Deleted ${result?.deleted ?? ids.length} list(s)${errors ? `, ${errors} failed` : ''}`,
                errors ? 'info' : 'success');
            App.selection.clear('lists');
            this.render();
        } catch {
            // API.request already surfaced the server message.
        }
    },

    /** Export the subscribers of every selected list, one CSV per list. */
    async bulkExport() {
        // Skip empty lists: /export-all answers 404 for them, which would
        // toast once per list for no reason.
        const ids = this._selectedIds().filter(id => {
            const row = this._rows.find(l => l.id === id);
            return row && (row.subscriber_count || 0) > 0;
        });
        if (!ids.length) { App.toast('Selected list(s) have no subscribers', 'error'); return; }

        let exported = 0;
        for (const id of ids) {
            try {
                const result = await API.download(`/api/subscribers/export-all?list_id=${id}`);
                API.downloadBlob(result.blob, `list_${id}_subscribers.csv`);
                exported++;
            } catch {
                // API.request already surfaced the server message.
            }
        }
        // Count only what actually downloaded — the old toast claimed every
        // selected list even when individual exports had failed.
        if (exported) {
            App.toast(`Exported ${exported} of ${ids.length} list(s)${exported < ids.length ? ' (failures reported above)' : ''}`,
                exported < ids.length ? 'info' : 'success');
        }
    },
};
