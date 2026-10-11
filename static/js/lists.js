/**
 * Lists page - CRUD, search.
 */
const Lists = {
    page: 1,
    query: '',

    async render() {
        try {
            const params = `page=${this.page}&per_page=50${this.query ? '&query=' + encodeURIComponent(this.query) : ''}`;
            const result = await API.get(`/api/lists?${params}`);
            const data = result?.data || {};
            const lists = data.results || [];
            const total = data.total || 0;

            App.setActions(`
                <button class="btn btn-sm btn-primary" onclick="Lists.showCreate()">+ New List</button>
            `);

            let html = `
                <div class="search-bar">
                    <div class="search-input">
                        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="11" cy="11" r="8"/><line x1="21" y1="21" x2="16.65" y2="16.65"/></svg>
                        <input type="search" id="listSearch" placeholder="Search lists..."
                            aria-label="Search lists"
                            value="${App.escapeHtml(this.query)}" onkeydown="if(event.key==='Enter')Lists.search()">
                    </div>
                    <button class="btn" onclick="Lists.search()">Search</button>
                </div>
                <div class="table-wrapper"><table>
                    <thead><tr>
                        <th>ID</th><th>Name</th><th>Type</th><th>Optin</th><th>Subscribers</th><th>Status</th><th>Created</th><th class="cell-actions">Actions</th>
                    </tr></thead><tbody>`;

            if (!lists.length) {
                html += '<tr><td colspan="8"><div class="empty-state"><h3>No lists found</h3></div></td></tr>';
            }

            lists.forEach(l => {
                const tags = (l.tags || []).map(t => `<span class="tag">${App.escapeHtml(t)}</span>`).join('') || '';
                html += `<tr>
                    <td>${l.id}</td>
                    <td><strong>${App.escapeHtml(l.name)}</strong> ${tags}</td>
                    <td><span class="badge badge-${l.type === 'public' ? 'info' : 'default'}">${l.type}</span></td>
                    <td>${l.optin}</td>
                    <td>${App.formatNumber(l.subscriber_count || 0)}</td>
                    <td>${App.statusBadge(l.status || 'active')}</td>
                    <td>${App.formatDate(l.created_at)}</td>
                    <td class="cell-actions">
                        <div class="action-btns">
                            <button class="btn btn-sm" onclick="Lists.showEdit(${l.id})">Edit</button>
                            <button class="btn btn-sm btn-danger" onclick="Lists.remove(${l.id})">Delete</button>
                        </div>
                    </td>
                </tr>`;
            });

            html += '</tbody></table></div>';
            html += App.renderPagination(this.page, total, 50, 'Lists.goToPage');
            App.setContent(html);
        } catch {
            App.setContent('<div class="empty-state"><h3>Failed to load lists</h3></div>');
        }
    },

    search() {
        this.query = document.getElementById('listSearch')?.value || '';
        this.page = 1;
        this.render();
    },

    goToPage(p) { this.page = p; this.render(); },

    showCreate() {
        App.setContent(`
            <div class="inline-form">
                <h3 style="margin-bottom:16px">Create List</h3>
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
            App.toast('List created', 'success');
            this.render();
        } catch {
            // API.request already surfaced the server message.
        }
    },

    async showEdit(id) {
        try {
            const result = await API.get(`/api/lists/${id}`);
            const l = result?.data || {};

            App.setContent(`
                <div class="inline-form">
                    <h3 style="margin-bottom:16px">Edit List #${l.id}</h3>
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

        try {
            await API.put(`/api/lists/${id}`, { name, type, optin, description, tags });
            App.toast('List updated', 'success');
            this.render();
        } catch {
            // API.request already surfaced the server message.
        }
    },

    async remove(id) {
        if (await App.confirm('Delete List', 'Are you sure you want to delete this list? All subscriber associations will be removed.')) {
            try {
                await API.del(`/api/lists/${id}`);
                App.toast('List deleted', 'success');
                this.render();
            } catch {
                // API.request already surfaced the server message.
            }
        }
    },
};
