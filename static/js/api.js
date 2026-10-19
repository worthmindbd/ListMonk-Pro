/**
 * API client wrapper for the ListMonk Dashboard backend.
 *
 * Error contract: `request()` rejects with an Error carrying the server's
 * `detail` message and marks it `err.handled = true` so it has already been
 * surfaced. Callers that want their own toast must check that flag first,
 * otherwise the user sees the same failure twice.
 */
const API = {
    /** `silent` suppresses the automatic error toast — for background polls
        that surface failures in their own UI instead of stacking toasts.
        The rejection is still marked err.handled so callers stay consistent. */
    /** `timeoutMs` overrides DEFAULT_TIMEOUT — page loads on a busy ListMonk
        pass a larger budget so a slow upstream is not read as an error. */
    /** Default timeout for API requests (ms). Exports and scans override this. */
    DEFAULT_TIMEOUT: 30000,

    /** Set after the first 401 redirect so concurrent requests don't each
        navigate and toast independently. */
    _redirecting: false,

    async request(method, path, body = null, isFormData = false, silent = false, timeoutMs = null) {
        const controller = new AbortController();
        const timeout = setTimeout(
            () => controller.abort(),
            timeoutMs || this.DEFAULT_TIMEOUT,
        );

        const opts = {
            method,
            headers: {
                'X-Requested-With': 'XMLHttpRequest',
            },
            signal: controller.signal,
        };

        if (body && !isFormData) {
            opts.headers['Content-Type'] = 'application/json';
            opts.body = JSON.stringify(body);
        } else if (body && isFormData) {
            opts.body = body; // FormData sets its own Content-Type
        }

        try {
            const resp = await fetch(path, opts);
            clearTimeout(timeout);
            if (!resp.ok) {
                if (resp.status === 401) {
                    if (!this._redirecting) {
                        this._redirecting = true;
                        window.location.href = '/auth/login';
                    }
                    throw Object.assign(new Error('Unauthorized'), { handled: true, silent: true });
                }
                let detail = `HTTP ${resp.status}`;
                try {
                    const err = await resp.json();
                    detail = err.detail || err.message || detail;
                } catch {}
                // Surface the real message once, here.
                if (!silent) App.toast(detail, 'error');
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
            clearTimeout(timeout);
            if (!err.handled) {
                // Network/abort/timeout failures never reached the response checks above.
                const msg = err.name === 'AbortError'
                    ? 'Request timed out — please try again'
                    : (err.message || 'Unknown error');
                if (!silent) App.toast(msg, 'error');
                err.handled = true;
            }
            throw err;
        }
    },

    get(path, silent = false, timeoutMs = null) { return this.request('GET', path, null, false, silent, timeoutMs); },
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
        try {
            document.body.appendChild(a);
            a.click();
        } finally {
            // Always revoke, even if the DOM insertion or click throws (e.g.
            // a sandboxed iframe): an unreleased object URL pins the blob in
            // memory for the lifetime of the document.
            if (a.parentNode) a.parentNode.removeChild(a);
            URL.revokeObjectURL(url);
        }
    }
};
