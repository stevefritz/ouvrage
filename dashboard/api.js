// Dashboard API client

const BASE = '/dashboard/api';

async function request(path, options = {}) {
    const resp = await fetch(BASE + path, {
        headers: { 'Content-Type': 'application/json' },
        ...options,
    });
    if (!resp.ok) {
        const err = await resp.json().catch(() => ({ error: resp.statusText }));
        throw new Error(err.error || resp.statusText);
    }
    const ct = resp.headers.get('content-type') || '';
    if (ct.includes('application/json')) return resp.json();
    return resp.text();
}

export const api = {
    // Read
    getTasks: (params = {}) => {
        const qs = new URLSearchParams(params).toString();
        return request('/tasks' + (qs ? '?' + qs : ''));
    },
    getTask: (id) => request(`/tasks/${id}`),
    getMessages: (id, params = {}) => {
        const qs = new URLSearchParams(params).toString();
        return request(`/tasks/${id}/messages` + (qs ? '?' + qs : ''));
    },
    getSessionLog: (id) => request(`/tasks/${id}/session-log`),
    getDispatchLog: (id) => request(`/tasks/${id}/dispatch-log`),
    getProjects: () => request('/projects'),
    getProject: (id) => request(`/projects/${id}`),
    getSystem: () => request('/system'),

    // Actions
    cancelTask: (id) => request(`/tasks/${id}/cancel`, { method: 'POST' }),
    retryTask: (id, clean = false) => request(`/tasks/${id}/retry`, {
        method: 'POST', body: JSON.stringify({ clean }),
    }),
    resumeTask: (id) => request(`/tasks/${id}/resume`, { method: 'POST' }),

    // Messages
    postMessage: (id, content, type = 'review', title = null) => request(`/tasks/${id}/messages`, {
        method: 'POST',
        body: JSON.stringify({ content, type, title }),
    }),
};
