/**
 * Subscribers page - CRUD, search, import/export.
 */
const Subscribers = {
    page: 1,
    query: '',

    async render() {
        try {
            const params = `page=${this.page}&per_page=50${this.query ? '&query=' + encodeURIComponent(this.query) : ''}`;
            const result = await API.get(`/api/subscribers?${params}`);
            const data = result?.data || {};
            const subscribers = data.results || [];
            const total = data.total || 0;

            App.setActions(`
                <button class="btn btn-sm" onclick="Subscribers.exportAll()">Export CSV</button>
                <button class="btn btn-sm btn-primary" onclick="Subscribers.showCreate()">+ Add Subscriber</button>
            `);

            let html = `
                <div class="search-bar">
                    <div class="search-input">
                        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="11" cy="11" r="8"/><line x1="21" y1="21" x2="16.65" y2="16.65"/></svg>
                        <input type="search" id="subSearch" placeholder="Search subscribers by email or name..."
                            aria-label="Search subscribers by email or name"
                            value="${App.escapeHtml(this.query)}" onkeydown="if(event.key==='Enter')Subscribers.search()">
                    </div>
                    <button class="btn" onclick="Subscribers.search()">Search</button>
                </div>
                <div class="table-wrapper"><table>
                    <thead><tr>
                        <th>ID</th><th>Email</th><th>Name</th><th>Status</th><th>Lists</th><th>Created</th><th class="cell-actions">Actions</th>
                    </tr></thead><tbody>`;

            if (!subscribers.length) {
                html += '<tr><td colspan="7"><div class="empty-state"><h3>No subscribers found</h3></div></td></tr>';
            }

            subscribers.forEach(s => {
                const lists = (s.lists || []).map(l => `<span class="tag">${App.escapeHtml(l.name)}</span>`).join('') || '-';
                html += `<tr>
                    <td>${s.id}</td>
                    <td><strong>${App.escapeHtml(s.email)}</strong></td>
                    <td>${App.escapeHtml(s.name) || '-'}</td>
                    <td>${App.statusBadge(s.status)}</td>
                    <td>${lists}</td>
                    <td>${App.formatDate(s.created_at)}</td>
                    <td class="cell-actions">
                        <div class="action-btns">
                            <button class="btn btn-sm" onclick="Subscribers.showEdit(${s.id})">Edit</button>
                            <button class="btn btn-sm btn-danger" onclick="Subscribers.remove(${s.id})">Delete</button>
                        </div>
                    </td>
                </tr>`;
            });

            html += '</tbody></table></div>';
            html += App.renderPagination(this.page, total, 50, 'Subscribers.goToPage');
            App.setContent(html);
        } catch {
            App.setContent('<div class="empty-state"><h3>Failed to load subscribers</h3></div>');
        }
    },

    search() {
        this.query = document.getElementById('subSearch')?.value || '';
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
        let attribs = {};
        try { attribs = JSON.parse(document.getElementById('subAttribs').value); } catch {}

        if (!email) { App.toast('Email is required', 'error'); return; }

        try {
            await API.post('/api/subscribers', { email, name, status, attribs, lists: [] });
            App.toast('Subscriber created', 'success');
            this.render();
        } catch {
            // API.request already surfaced the server message.
        }
    },

    async showEdit(id) {
        try {
            const result = await API.get(`/api/subscribers/${id}`);
            const s = result?.data || {};

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
        let attribs = {};
        try { attribs = JSON.parse(document.getElementById('editAttribs').value); } catch {}

        try {
            await API.put(`/api/subscribers/${id}`, { email, name, status, attribs });
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
                this.render();
            } catch {
                // API.request already surfaced the server message.
            }
        }
    },

    async exportAll() {
        try {
            const queryParam = this.query ? `?query=${encodeURIComponent(this.query)}` : '';
            const result = await API.get(`/api/subscribers/export-all${queryParam}`);
            API.downloadBlob(result.blob, 'subscribers_export.csv');
            App.toast('Export downloaded', 'success');
        } catch {
            // API.request already surfaced the server message.
        }
    },
};
