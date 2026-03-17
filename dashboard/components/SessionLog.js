import { useState, useEffect, useRef, useCallback } from 'https://esm.sh/preact@10.25.4/hooks';
import { api } from '../api.js';
import { html } from './utils.js';

function LogExpandable({ ts, label, labelCls, preview, full, logType, filters }) {
    const [expanded, setExpanded] = useState(false);
    const visible = !logType || filters.has(logType);

    if (!visible) return null;

    if (!full || full === preview) {
        return html`<div class="${labelCls} text-xs py-0.5">
            <span class="text-slate-600 mr-2">${ts}</span>${label} ${preview}
        </div>`;
    }

    return html`
        <div class="${labelCls} text-xs py-0.5 cursor-pointer" onClick=${() => setExpanded(!expanded)}>
            <span class="text-slate-600 mr-2">${ts}</span>${label} ${preview} <span class="text-slate-600">${expanded ? '\u25BE' : '\u25B8'}</span>
        </div>
        ${expanded ? html`<div class="text-xs ml-8 py-1 px-2 mb-1 bg-slate-800/50 rounded whitespace-pre-wrap text-slate-300 max-h-96 overflow-y-auto">${full}</div>` : null}
    `;
}

function SessionLogEntries({ entries, filters }) {
    if (entries.length === 0) {
        return html`<p class="text-slate-500 text-sm p-2">No session log</p>`;
    }

    return html`${entries.map((e, idx) => {
        const ts = e.timestamp ? new Date(e.timestamp).toLocaleTimeString() : '';
        const type = e.type || '';

        if (type === 'SystemMessage') {
            return html`<div key=${idx} class="log-system text-xs py-0.5">
                <span class="text-slate-600 mr-2">${ts}</span>SYSTEM ${e.subtype || ''}
            </div>`;
        }

        if (type === 'AssistantMessage') {
            const blocks = e.content || [];
            return html`${blocks.map((b, bi) => {
                if (b.type === 'text') {
                    const full = b.text || '';
                    const preview = full.slice(0, 150);
                    return html`<${LogExpandable} key=${`${idx}-${bi}`} ts=${ts} label="TEXT " labelCls="log-text"
                        preview=${preview + (full.length > 150 ? '\u2026' : '')}
                        full=${full.length > 150 ? full : null} logType="text" filters=${filters} />`;
                }
                if (b.type === 'tool_use') {
                    const input = typeof b.input === 'string' ? b.input : JSON.stringify(b.input) || '';
                    const preview = `${b.name || ''} \u2192 ${input.slice(0, 100)}`;
                    return html`<${LogExpandable} key=${`${idx}-${bi}`} ts=${ts} label="TOOL " labelCls="log-tool"
                        preview=${preview + (input.length > 100 ? '\u2026' : '')}
                        full=${input.length > 100 ? input : null} logType="tool" filters=${filters} />`;
                }
                return null;
            })}`;
        }

        if (type === 'UserMessage') {
            const blocks = e.content || [];
            return html`${blocks.map((b, bi) => {
                if (b.type === 'tool_result') {
                    const content = b.preview || '';
                    if (b.is_error) {
                        return html`<${LogExpandable} key=${`${idx}-${bi}`} ts=${ts} label="RESULT" labelCls="log-result text-red-400"
                            preview="(error)" full=${content || null} logType="error" filters=${filters} />`;
                    }
                    const preview = content.slice(0, 120);
                    return html`<${LogExpandable} key=${`${idx}-${bi}`} ts=${ts} label="RESULT" labelCls="log-result"
                        preview=${preview ? preview + (content.length > 120 ? '\u2026' : '') : `(${content.length}B)`}
                        full=${content.length > 120 ? content : null} logType="tool" filters=${filters} />`;
                }
                return null;
            })}`;
        }

        if (type === 'ResultMessage') {
            const cls = e.is_error ? 'log-error' : 'log-done';
            const result = e.result || '';
            const summary = `${e.num_turns || '?'} turns | $${(e.cost_usd || 0).toFixed(2)}`;
            return html`<${LogExpandable} key=${idx} ts=${ts} label="DONE " labelCls="${cls} font-medium"
                preview=${summary} full=${result || null} logType="text" filters=${filters} />`;
        }

        return null;
    })}`;
}

export function SessionLogPanel({ taskId, isOpen, onToggle, autoRefresh }) {
    const [entries, setEntries] = useState([]);
    const [loaded, setLoaded] = useState(false);
    const [filters, setFilters] = useState(new Set(['text', 'tool', 'error']));
    const panelRef = useRef(null);
    const prevTaskId = useRef(taskId);

    // Reset when task changes
    if (prevTaskId.current !== taskId) {
        prevTaskId.current = taskId;
        setEntries([]);
        setLoaded(false);
    }

    useEffect(() => {
        if (!isOpen || !taskId) return;

        let cancelled = false;
        async function load() {
            try {
                const data = await api.getSessionLog(taskId);
                if (!cancelled) {
                    setEntries(data);
                    setLoaded(true);
                    // Auto-scroll to bottom
                    if (panelRef.current) {
                        const el = panelRef.current;
                        const wasAtBottom = el.scrollHeight - el.scrollTop - el.clientHeight < 30;
                        if (wasAtBottom) {
                            requestAnimationFrame(() => { el.scrollTop = el.scrollHeight; });
                        }
                    }
                }
            } catch (e) {
                console.warn('Session log error:', e.message);
            }
        }

        load();

        let timer;
        if (autoRefresh) {
            timer = setInterval(load, 5000);
        }
        return () => { cancelled = true; if (timer) clearInterval(timer); };
    }, [isOpen, taskId, autoRefresh]);

    const toggleFilter = useCallback((key) => {
        setFilters(prev => {
            const next = new Set(prev);
            if (next.has(key)) next.delete(key);
            else next.add(key);
            if (next.size === 0) { next.add('text'); next.add('tool'); next.add('error'); }
            return next;
        });
    }, []);

    const filterBtn = (key, label) => {
        const on = filters.has(key);
        const cls = on ? 'bg-slate-600 text-slate-200' : 'bg-slate-800 text-slate-400 hover:bg-slate-700';
        return html`<button onClick=${() => toggleFilter(key)} class="px-2 py-0.5 text-xs rounded ${cls}">${label}</button>`;
    };

    return html`
        <div class="bg-slate-900 border border-slate-800 rounded-lg mb-4">
            <button onClick=${onToggle}
                class="w-full flex items-center justify-between px-4 py-3 text-sm text-slate-300 hover:text-slate-100">
                <span>Session Log</span>
                <span class="text-slate-500">${isOpen ? '\u25BE' : '\u25B8'}</span>
            </button>
            ${isOpen ? html`
                <div ref=${panelRef} class="px-4 pb-3 max-h-96 overflow-y-auto font-mono">
                    <div class="flex gap-1 mb-2 pb-2 border-b border-slate-700/50 sticky top-0 bg-slate-900 z-10 pt-1">
                        ${filterBtn('text', 'Text')}${filterBtn('tool', 'Tools')}${filterBtn('error', 'Errors')}
                    </div>
                    <${SessionLogEntries} entries=${entries} filters=${filters} />
                </div>
            ` : null}
        </div>
    `;
}

export function DispatchLogPanel({ taskId, isOpen, onToggle }) {
    const [text, setText] = useState('');
    const [loaded, setLoaded] = useState(false);
    const prevTaskId = useRef(taskId);

    if (prevTaskId.current !== taskId) {
        prevTaskId.current = taskId;
        setText('');
        setLoaded(false);
    }

    useEffect(() => {
        if (!isOpen || !taskId || loaded) return;
        let cancelled = false;
        api.getDispatchLog(taskId)
            .then(data => { if (!cancelled) { setText(data); setLoaded(true); } })
            .catch(e => { if (!cancelled) { setText(`Error: ${e.message}`); setLoaded(true); } });
        return () => { cancelled = true; };
    }, [isOpen, taskId, loaded]);

    return html`
        <div class="bg-slate-900 border border-slate-800 rounded-lg mb-4">
            <button onClick=${onToggle}
                class="w-full flex items-center justify-between px-4 py-3 text-sm text-slate-300 hover:text-slate-100">
                <span>Dispatch Log</span>
                <span class="text-slate-500">${isOpen ? '\u25BE' : '\u25B8'}</span>
            </button>
            ${isOpen ? html`
                <div class="px-4 pb-3 max-h-96 overflow-y-auto font-mono">
                    ${text
                        ? html`<pre class="text-xs text-slate-400 whitespace-pre-wrap">${text}</pre>`
                        : html`<p class="text-slate-500 text-sm">No dispatch log</p>`}
                </div>
            ` : null}
        </div>
    `;
}
