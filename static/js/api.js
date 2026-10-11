/**
 * API client wrapper for the ListMonk Dashboard backend.
 *
 * Error contract: `request()` rejects with an Error carrying the server's
 * `detail` message and marks it `err.handled = true` so it has already been
 * surfaced. Callers that want their own toast must check that flag first,
 * otherwise the user sees the same failure twice.
 */
const API = {
    async request(method, path, body = null, isFormData = false) {
        const opts = {
            method,
            headers: {},
        };

        if (body && !isFormData) {
            opts.headers['Content-Type'] = 'application/json';
            opts.body = JSON.stringify(body);
        } else if (body && isFormData) {
            opts.body = body; // FormData sets its own Content-Type
        }

        try {
            const resp = await fetch(path, opts);
            if (!resp.ok) {
                if (resp.status === 401) {
                    window.location.href = '/auth/login';
                    throw Object.assign(new Error('Unauthorized'), { handled: true, silent: true });
                }
                let detail = `HTTP ${resp.status}`;
                try {
                    const err = await resp.json();
                    detail = err.detail || err.message || detail;
                } catch {}
                // Surface the real message once, here.
                App.toast(detail, 'error');
                throw Object.assign(new Error(detail), { handled: true });
            }

            const contentType = resp.headers.get('content-type') || '';
            if (contentType.includes('text/csv')) {
                return { blob: await resp.blob(), stats: resp.headers.get('x-conversion-stats') };
            }
            if (contentType.includes('text/html')) {
                return { html: await resp.text() };
            }
            return await resp.json();
        } catch (err) {
            if (!err.handled) {
                // Network/abort failures never reached the response checks above.
                App.toast(err.message, 'error');
                err.handled = true;
            }
            throw err;
        }
    },

    get(path) { return this.request('GET', path); },
    post(path, body) { return this.request('POST', path, body); },
    put(path, body) { return this.request('PUT', path, body); },
    del(path) { return this.request('DELETE', path); },
    upload(path, formData) { return this.request('POST', path, formData, true); },

    /**
     * Returns a human-readable message for an error that still needs one.
     * Empty string when API.request already toasted it.
     */
    errorMessage(err) {
        return err && err.handled ? '' : (err?.message || 'Unknown error');
    },

    // Download helper
    downloadBlob(blob, filename) {
        const url = URL.createObjectURL(blob);
        const a = document.createElement('a');
        a.href = url;
        a.download = filename;
        document.body.appendChild(a);
        a.click();
        document.body.removeChild(a);
        URL.revokeObjectURL(url);
    }
};
