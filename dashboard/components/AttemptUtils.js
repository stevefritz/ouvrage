// AttemptUtils — shared attempt grouping and per-attempt session log
// Used by both TaskPanel.js (slide-out) and TaskDetail.js (full page)
import { useState, useEffect, useRef } from 'https://esm.sh/preact@10.25.4/hooks';
import { api } from '../api.js';
import { html } from './utils.js';

// ── Attempt boundary markers ────────────────────────────────
export const ATTEMPT_BOUNDARIES = [
    'Task completed', 'Task failed', 'Dispatch error', 'Turns exhausted',
    'Session killed by signal', 'Rate limited', 'Wall clock timeout',
    'Recovery limit reached',
];

// ── Group messages into attempts ────────────────────────────
export function groupMessagesByAttempt(messages) {
    if (!messages || messages.length === 0) return [];
    const attempts = [];
    let current = { messages: [], outcome: null, number: 1 };

    for (const msg of messages) {
        if (msg.type === 'plan') continue;
        current.messages.push(msg);
        if (msg.author === 'dispatcher' && msg.type === 'status'
            && ATTEMPT_BOUNDARIES.some(b => (msg.title || '').includes(b))) {
            current.outcome = msg.title || 'Completed';
            attempts.push(current);
            current = { messages: [], outcome: null, number: attempts.length + 1 };
        }
    }
    if (current.messages.length > 0) {
        if (current.messages.every(m => m.author === 'dispatcher')) {
            if (attempts.length > 0) {
                const last = attempts[attempts.length - 1];
                last.messages.push(...current.messages);
            } else {
                current.outcome = 'Status';
                attempts.push(current);
            }
        } else {
            current.outcome = 'In Progress';
            attempts.push(current);
        }
    }
    return attempts;
}

// ── Per-Attempt Session Log ─────────────────────────────────
export function AttemptSessionLog({ taskId, attemptNumber, autoRefresh }) {
    const [expanded, setExpanded] = useState(false);
    const [entries, setEntries] = useState([]);
    const [loaded, setLoaded] = useState(false);
    const [showTools, setShowTools] = useState(false);
    const logRef = useRef(null);

    useEffect(() => {
        if (!expanded) return;
        let cancelled = false;
        const load = () => {
            api.getSessionLog(taskId, { attempt: attemptNumber })
                .then(data => {
                    if (cancelled) return;
                    setEntries(data);
                    setLoaded(true);
                    if (logRef.current) {
                        const el = logRef.current;
                        const wasAtBottom = el.scrollHeight - el.scrollTop - el.clientHeight < 40;
                        if (wasAtBottom) {
                            requestAnimationFrame(() => { el.scrollTop = el.scrollHeight; });
                        }
                    }
                })
                .catch(() => { if (!cancelled) setLoaded(true); });
        };
        load();
        let timer;
        if (autoRefresh) {
            timer = setInterval(load, 5000);
        }
        return () => { cancelled = true; if (timer) clearInterval(timer); };
    }, [expanded, taskId, attemptNumber, autoRefresh]);

    return html`
        <div class="mt-2 border-t pt-2" style="border-color: var(--border-subtle)">
            <button onClick=${() => setExpanded(!expanded)}
                class="text-xs flex items-center gap-1" style="color: var(--text-faint); cursor: pointer">
                ${expanded ? '\u25BE' : '\u25B8'} Session Log
            </button>
            ${expanded ? html`
                <div class="mt-1">
                    <button onClick=${() => setShowTools(!showTools)}
                        class="text-xs px-2 py-0.5 rounded mb-1" style="background: var(--bg-secondary); color: var(--text-faint)">
                        ${showTools ? 'Text only' : 'Show tools'}
                    </button>
                    <pre ref=${logRef} class="text-xs overflow-y-auto whitespace-pre-wrap rounded p-2"
                        style="max-height: 400px; background: var(--bg-primary); color: var(--text-muted)">
                        ${!loaded ? 'Loading...' : entries.length === 0 ? 'No session log' :
                            entries.map(e => {
                                if (e.type === 'AssistantMessage') {
                                    return (e.content || []).map(b => {
                                        if (b.type === 'text') return b.text + '\n';
                                        if (b.type === 'tool_use' && showTools) return '[TOOL] ' + b.name + ': ' + JSON.stringify(b.input).slice(0, 200) + '\n';
                                        return '';
                                    }).join('');
                                }
                                if (e.type === 'UserMessage' && showTools) {
                                    return (e.content || []).map(b => {
                                        if (b.type === 'tool_result') return '[RESULT] ' + (b.preview || '').slice(0, 200) + '\n';
                                        return '';
                                    }).join('');
                                }
                                return '';
                            }).join('')
                        }
                    </pre>
                </div>
            ` : null}
        </div>
    `;
}
