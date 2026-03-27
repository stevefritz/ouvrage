import { html } from './utils.js';
import { useState, useEffect, useCallback, useRef } from 'https://esm.sh/preact@10.25.4/hooks';
import { api } from '../api.js';

// ── Toggle component ──────────────────────────────────────────────────────

function Toggle({ checked, onChange, disabled = false }) {
    return html`
        <button
            role="switch"
            aria-checked=${checked}
            disabled=${disabled}
            onClick=${() => !disabled && onChange(!checked)}
            class="relative inline-flex h-5 w-9 items-center rounded-full transition-colors focus:outline-none
                   ${checked ? 'bg-indigo-500' : 'bg-slate-600'}
                   ${disabled ? 'opacity-40 cursor-not-allowed' : 'cursor-pointer'}"
        >
            <span class="inline-block h-3.5 w-3.5 transform rounded-full bg-white transition-transform
                         ${checked ? 'translate-x-4' : 'translate-x-1'}" />
        </button>
    `;
}

// ── Subscribe/unsubscribe logic ───────────────────────────────────────────

async function getPushState() {
    if (!('serviceWorker' in navigator) || !('PushManager' in window)) {
        return { supported: false, serverConfigured: true, subscribed: false };
    }
    let serverConfigured = true;
    try {
        const keyResp = await api.getVapidPublicKey();
        serverConfigured = !!keyResp.vapid_public_key;
    } catch {
        serverConfigured = false;
    }
    const reg = await navigator.serviceWorker.ready;
    const sub = await reg.pushManager.getSubscription();
    return { supported: true, serverConfigured, subscribed: !!sub, subscription: sub };
}

async function subscribePush() {
    const reg = await navigator.serviceWorker.ready;
    const keyResp = await api.getVapidPublicKey();
    const vapidKey = keyResp.vapid_public_key;
    if (!vapidKey) throw new Error('VAPID public key not configured on server');

    const padding = '='.repeat((4 - (vapidKey.length % 4)) % 4);
    const b64 = (vapidKey + padding).replace(/-/g, '+').replace(/_/g, '/');
    const raw = atob(b64);
    const applicationServerKey = new Uint8Array(raw.length);
    for (let i = 0; i < raw.length; i++) applicationServerKey[i] = raw.charCodeAt(i);

    const sub = await reg.pushManager.subscribe({
        userVisibleOnly: true,
        applicationServerKey,
    });

    const json = sub.toJSON();
    await api.pushSubscribe({
        endpoint: json.endpoint,
        p256dh: json.keys.p256dh,
        auth: json.keys.auth,
    });
    return sub;
}

async function unsubscribePush(subscription) {
    await api.pushUnsubscribe({ endpoint: subscription.endpoint });
    await subscription.unsubscribe();
}

// ── Common timezone list ──────────────────────────────────────────────────

const TIMEZONES = [
    'America/New_York', 'America/Chicago', 'America/Denver', 'America/Los_Angeles',
    'America/Anchorage', 'Pacific/Honolulu', 'America/Toronto', 'America/Vancouver',
    'America/Halifax', 'America/St_Johns', 'America/Edmonton', 'America/Winnipeg',
    'America/Sao_Paulo', 'America/Argentina/Buenos_Aires', 'America/Mexico_City',
    'Europe/London', 'Europe/Paris', 'Europe/Berlin', 'Europe/Madrid', 'Europe/Rome',
    'Europe/Amsterdam', 'Europe/Brussels', 'Europe/Zurich', 'Europe/Stockholm',
    'Europe/Oslo', 'Europe/Helsinki', 'Europe/Warsaw', 'Europe/Prague',
    'Europe/Moscow', 'Europe/Istanbul',
    'Asia/Dubai', 'Asia/Kolkata', 'Asia/Bangkok', 'Asia/Singapore',
    'Asia/Shanghai', 'Asia/Tokyo', 'Asia/Seoul', 'Asia/Hong_Kong',
    'Australia/Sydney', 'Australia/Melbourne', 'Australia/Perth',
    'Pacific/Auckland', 'UTC',
];

// ── Shared styles ─────────────────────────────────────────────────────────

const inputClass = 'w-full px-3 py-2 rounded-md text-sm focus:outline-none focus:ring-1 focus:ring-indigo-500';
const inputStyle = {
    background: 'var(--bg-secondary)',
    border: '1px solid var(--border-primary)',
    color: 'var(--text-primary)',
};
const btnPrimary = 'px-4 py-2 text-sm rounded-md bg-indigo-600 text-white hover:bg-indigo-500 disabled:opacity-50 disabled:cursor-not-allowed';
const btnDanger = 'px-4 py-2 text-sm rounded-md bg-red-600/20 text-red-400 hover:bg-red-600/30 disabled:opacity-50 disabled:cursor-not-allowed';
const btnSecondary = 'px-4 py-2 text-sm rounded-md text-slate-300 hover:text-slate-100 disabled:opacity-50 disabled:cursor-not-allowed';
const cardClass = 'border rounded-lg p-5 mb-6';
const cardStyle = { background: 'var(--bg-card)', borderColor: 'var(--border-primary)' };

// ── Feedback banner ───────────────────────────────────────────────────────

function FeedbackBanner({ message, type = 'success' }) {
    if (!message) return null;
    const colors = type === 'success' ? 'text-emerald-400' : 'text-red-400';
    return html`<div class="text-sm ${colors} mt-2">${message}</div>`;
}

// ── Copy button ───────────────────────────────────────────────────────────

function CopyButton({ value }) {
    const [copied, setCopied] = useState(false);
    const handleCopy = useCallback(async () => {
        try {
            await navigator.clipboard.writeText(value);
            setCopied(true);
            setTimeout(() => setCopied(false), 2000);
        } catch {
            // Fallback
            const ta = document.createElement('textarea');
            ta.value = value;
            document.body.appendChild(ta);
            ta.select();
            document.execCommand('copy');
            document.body.removeChild(ta);
            setCopied(true);
            setTimeout(() => setCopied(false), 2000);
        }
    }, [value]);

    return html`<button onClick=${handleCopy}
        class="px-2 py-1 text-xs rounded ${copied ? 'text-emerald-400' : 'text-slate-400 hover:text-slate-200'}"
        style="background: var(--bg-secondary); border: 1px solid var(--border-primary);"
        >${copied ? 'Copied!' : 'Copy'}</button>`;
}

// ── Confirmation modal ────────────────────────────────────────────────────

function ConfirmModal({ show, title, message, confirmLabel, onConfirm, onCancel }) {
    if (!show) return null;
    return html`
        <div class="confirm-overlay" onClick=${onCancel}>
            <div class="confirm-dialog" onClick=${(e) => e.stopPropagation()}>
                <h3>${title}</h3>
                <p>${message}</p>
                <div class="confirm-actions">
                    <button class="confirm-btn confirm-btn-cancel" onClick=${onCancel}>Cancel</button>
                    <button class="confirm-btn confirm-btn-danger" onClick=${onConfirm}>${confirmLabel}</button>
                </div>
            </div>
        </div>
    `;
}

// ══════════════════════════════════════════════════════════════════════════
// Instance Settings — GitHub Connection
// ══════════════════════════════════════════════════════════════════════════

function GitHubCard({ github, onSaved }) {
    const [pat, setPat] = useState('');
    const [saving, setSaving] = useState(false);
    const [testing, setTesting] = useState(false);
    const [feedback, setFeedback] = useState(null);
    const [editing, setEditing] = useState(!github.connected && !github.pat_last4);

    const handleSave = useCallback(async () => {
        if (!pat.trim()) return;
        setSaving(true);
        setFeedback(null);
        try {
            await api.patchInstanceSettings({ github_pat: pat.trim() });
            setPat('');
            setEditing(false);
            setFeedback({ type: 'success', message: 'GitHub PAT saved' });
            if (onSaved) onSaved();
        } catch (e) {
            setFeedback({ type: 'error', message: e.message });
        } finally {
            setSaving(false);
        }
    }, [pat, onSaved]);

    const handleTest = useCallback(async () => {
        setTesting(true);
        setFeedback(null);
        try {
            const result = await api.testGithub();
            if (result.valid) {
                setFeedback({ type: 'success', message: `Connected as ${result.username}` });
            } else {
                setFeedback({ type: 'error', message: result.error || 'Connection failed' });
            }
        } catch (e) {
            setFeedback({ type: 'error', message: e.message });
        } finally {
            setTesting(false);
        }
    }, []);

    return html`
        <div class=${cardClass} style=${cardStyle}>
            <h2 class="text-base font-medium mb-1" style="color: var(--text-primary)">GitHub Connection</h2>
            <p class="text-sm mb-4" style="color: var(--text-muted)">
                Connect a GitHub Personal Access Token to enable repository operations.
            </p>

            ${github.connected && !editing && html`
                <div class="flex items-center gap-2 mb-3">
                    <span class="inline-block w-2 h-2 rounded-full bg-emerald-400"></span>
                    <span class="text-sm" style="color: var(--text-primary)">Connected as <strong>${github.username}</strong></span>
                    <span class="text-xs" style="color: var(--text-muted)">PAT ****${github.pat_last4}</span>
                </div>
                <div class="flex gap-2">
                    <button class=${btnSecondary} style="background: var(--bg-secondary); border: 1px solid var(--border-primary);"
                        onClick=${() => setEditing(true)}>Update</button>
                    <button class=${btnSecondary} style="background: var(--bg-secondary); border: 1px solid var(--border-primary);"
                        onClick=${handleTest} disabled=${testing}>
                        ${testing ? 'Testing...' : 'Test Connection'}
                    </button>
                </div>
            `}

            ${!github.connected && github.pat_last4 && !editing && html`
                <div class="flex items-center gap-2 mb-3">
                    <span class="inline-block w-2 h-2 rounded-full bg-amber-400"></span>
                    <span class="text-sm" style="color: var(--text-primary)">PAT configured (****${github.pat_last4}) but connection failed</span>
                </div>
                <div class="flex gap-2">
                    <button class=${btnSecondary} style="background: var(--bg-secondary); border: 1px solid var(--border-primary);"
                        onClick=${() => setEditing(true)}>Update</button>
                    <button class=${btnSecondary} style="background: var(--bg-secondary); border: 1px solid var(--border-primary);"
                        onClick=${handleTest} disabled=${testing}>
                        ${testing ? 'Testing...' : 'Test Connection'}
                    </button>
                </div>
            `}

            ${editing && html`
                <div class="flex gap-2 mb-2">
                    <input type="password" class=${inputClass} style=${inputStyle}
                        placeholder="ghp_xxxxxxxxxxxx"
                        value=${pat} onInput=${(e) => setPat(e.target.value)}
                        onKeyDown=${(e) => e.key === 'Enter' && handleSave()} />
                    <button class=${btnPrimary} onClick=${handleSave} disabled=${saving || !pat.trim()}>
                        ${saving ? 'Saving...' : (github.pat_last4 ? 'Update' : 'Connect')}
                    </button>
                    ${github.pat_last4 && html`
                        <button class=${btnSecondary} style="background: var(--bg-secondary); border: 1px solid var(--border-primary);"
                            onClick=${() => { setEditing(false); setPat(''); setFeedback(null); }}>Cancel</button>
                    `}
                </div>
            `}

            <${FeedbackBanner} message=${feedback?.message} type=${feedback?.type} />
        </div>
    `;
}

// ══════════════════════════════════════════════════════════════════════════
// Instance Settings — OAuth / MCP Connection
// ══════════════════════════════════════════════════════════════════════════

function OAuthCard({ oauth, onRegenerated }) {
    const [showConfirm, setShowConfirm] = useState(false);
    const [regenerating, setRegenerating] = useState(false);
    const [feedback, setFeedback] = useState(null);

    const handleRegenerate = useCallback(async () => {
        setShowConfirm(false);
        setRegenerating(true);
        setFeedback(null);
        try {
            await api.regenerateOAuthSecret();
            setFeedback({ type: 'success', message: 'Secret regenerated. Update your Claude.ai configuration.' });
            if (onRegenerated) onRegenerated();
        } catch (e) {
            setFeedback({ type: 'error', message: e.message });
        } finally {
            setRegenerating(false);
        }
    }, [onRegenerated]);

    if (!oauth.client_id) {
        return html`
            <div class=${cardClass} style=${cardStyle}>
                <h2 class="text-base font-medium mb-1" style="color: var(--text-primary)">OAuth / MCP Connection</h2>
                <p class="text-sm" style="color: var(--text-muted)">OAuth client not configured.</p>
            </div>
        `;
    }

    return html`
        <div class=${cardClass} style=${cardStyle}>
            <h2 class="text-base font-medium mb-1" style="color: var(--text-primary)">OAuth / MCP Connection</h2>
            <p class="text-sm mb-4" style="color: var(--text-muted)">
                Use these credentials to connect Claude.ai to your Switchboard MCP server.
            </p>

            <div class="space-y-3">
                <div>
                    <label class="text-xs font-medium" style="color: var(--text-muted)">Client ID</label>
                    <div class="flex items-center gap-2 mt-1">
                        <code class="flex-1 px-3 py-2 rounded-md text-sm font-mono"
                            style="background: var(--bg-secondary); color: var(--text-primary); border: 1px solid var(--border-primary);"
                            >${oauth.client_id}</code>
                        <${CopyButton} value=${oauth.client_id} />
                    </div>
                </div>

                <div>
                    <label class="text-xs font-medium" style="color: var(--text-muted)">Client Secret</label>
                    <div class="flex items-center gap-2 mt-1">
                        <code class="flex-1 px-3 py-2 rounded-md text-sm font-mono"
                            style="background: var(--bg-secondary); color: var(--text-primary); border: 1px solid var(--border-primary); word-break: break-all;"
                            >${oauth.client_secret}</code>
                        <${CopyButton} value=${oauth.client_secret} />
                    </div>
                </div>
            </div>

            <div class="mt-4">
                <button class=${btnDanger} onClick=${() => setShowConfirm(true)} disabled=${regenerating}>
                    ${regenerating ? 'Regenerating...' : 'Regenerate Secret'}
                </button>
            </div>

            <${FeedbackBanner} message=${feedback?.message} type=${feedback?.type} />

            <${ConfirmModal}
                show=${showConfirm}
                title="Regenerate OAuth Secret"
                message="This will disconnect existing Claude.ai connections. You will need to reconfigure Claude.ai with the new secret."
                confirmLabel="Regenerate"
                onConfirm=${handleRegenerate}
                onCancel=${() => setShowConfirm(false)}
            />
        </div>
    `;
}

// ══════════════════════════════════════════════════════════════════════════
// User Settings — Anthropic API Key
// ══════════════════════════════════════════════════════════════════════════

function AnthropicKeyCard({ anthropic, onSaved }) {
    const [key, setKey] = useState('');
    const [saving, setSaving] = useState(false);
    const [testing, setTesting] = useState(false);
    const [feedback, setFeedback] = useState(null);
    const [editing, setEditing] = useState(!anthropic.configured);

    const handleSave = useCallback(async () => {
        if (!key.trim()) return;
        setSaving(true);
        setFeedback(null);
        try {
            await api.patchUserSettings({ anthropic_api_key: key.trim() });
            setKey('');
            setEditing(false);
            setFeedback({ type: 'success', message: 'API key saved' });
            if (onSaved) onSaved();
        } catch (e) {
            setFeedback({ type: 'error', message: e.message });
        } finally {
            setSaving(false);
        }
    }, [key, onSaved]);

    const handleTest = useCallback(async () => {
        setTesting(true);
        setFeedback(null);
        try {
            const result = await api.testAnthropic();
            if (result.valid) {
                setFeedback({ type: 'success', message: 'API key is valid' });
            } else {
                setFeedback({ type: 'error', message: result.error || 'Validation failed' });
            }
        } catch (e) {
            setFeedback({ type: 'error', message: e.message });
        } finally {
            setTesting(false);
        }
    }, []);

    return html`
        <div class=${cardClass} style=${cardStyle}>
            <h2 class="text-base font-medium mb-1" style="color: var(--text-primary)">Anthropic API Key</h2>
            <p class="text-sm mb-4" style="color: var(--text-muted)">
                Your personal API key for Claude model access.
            </p>

            ${anthropic.configured && !editing && html`
                <div class="flex items-center gap-2 mb-3">
                    <span class="inline-block w-2 h-2 rounded-full bg-emerald-400"></span>
                    <span class="text-sm" style="color: var(--text-primary)">Configured</span>
                    <span class="text-xs" style="color: var(--text-muted)">****${anthropic.key_last4}</span>
                </div>
                <div class="flex gap-2">
                    <button class=${btnSecondary} style="background: var(--bg-secondary); border: 1px solid var(--border-primary);"
                        onClick=${() => setEditing(true)}>Update</button>
                    <button class=${btnSecondary} style="background: var(--bg-secondary); border: 1px solid var(--border-primary);"
                        onClick=${handleTest} disabled=${testing}>
                        ${testing ? 'Testing...' : 'Test Connection'}
                    </button>
                </div>
            `}

            ${editing && html`
                <div class="flex gap-2 mb-2">
                    <input type="password" class=${inputClass} style=${inputStyle}
                        placeholder="sk-ant-xxxxxxxxxxxx"
                        value=${key} onInput=${(e) => setKey(e.target.value)}
                        onKeyDown=${(e) => e.key === 'Enter' && handleSave()} />
                    <button class=${btnPrimary} onClick=${handleSave} disabled=${saving || !key.trim()}>
                        ${saving ? 'Saving...' : 'Save'}
                    </button>
                    ${anthropic.configured && html`
                        <button class=${btnSecondary} style="background: var(--bg-secondary); border: 1px solid var(--border-primary);"
                            onClick=${() => { setEditing(false); setKey(''); setFeedback(null); }}>Cancel</button>
                    `}
                </div>
            `}

            <${FeedbackBanner} message=${feedback?.message} type=${feedback?.type} />
        </div>
    `;
}

// ══════════════════════════════════════════════════════════════════════════
// User Settings — Profile
// ══════════════════════════════════════════════════════════════════════════

function ProfileCard({ profile, onSaved }) {
    const [name, setName] = useState(profile.name || '');
    const [email, setEmail] = useState(profile.email || '');
    const [timezone, setTimezone] = useState(profile.timezone || '');
    const [saving, setSaving] = useState(false);
    const [feedback, setFeedback] = useState(null);

    const handleSave = useCallback(async () => {
        setSaving(true);
        setFeedback(null);
        try {
            const updates = {};
            if (name !== (profile.name || '')) updates.name = name;
            if (email !== (profile.email || '')) updates.email = email;
            if (timezone !== (profile.timezone || '')) updates.timezone = timezone;
            if (Object.keys(updates).length === 0) {
                setFeedback({ type: 'success', message: 'No changes' });
                setSaving(false);
                return;
            }
            await api.patchUserSettings(updates);
            setFeedback({ type: 'success', message: 'Profile updated' });
            if (onSaved) onSaved();
        } catch (e) {
            setFeedback({ type: 'error', message: e.message });
        } finally {
            setSaving(false);
        }
    }, [name, email, timezone, profile, onSaved]);

    return html`
        <div class=${cardClass} style=${cardStyle}>
            <h2 class="text-base font-medium mb-1" style="color: var(--text-primary)">Profile</h2>
            <p class="text-sm mb-4" style="color: var(--text-muted)">
                Your account information.
            </p>

            <div class="space-y-3">
                <div>
                    <label class="text-xs font-medium" style="color: var(--text-muted)">Name</label>
                    <input type="text" class=${inputClass + ' mt-1'} style=${inputStyle}
                        value=${name} onInput=${(e) => setName(e.target.value)} />
                </div>
                <div>
                    <label class="text-xs font-medium" style="color: var(--text-muted)">Email</label>
                    <input type="email" class=${inputClass + ' mt-1'} style=${inputStyle}
                        value=${email} onInput=${(e) => setEmail(e.target.value)} />
                </div>
                <div>
                    <label class="text-xs font-medium" style="color: var(--text-muted)">Timezone</label>
                    <select class=${inputClass + ' mt-1'} style=${inputStyle}
                        value=${timezone} onChange=${(e) => setTimezone(e.target.value)}>
                        <option value="">Select timezone...</option>
                        ${TIMEZONES.map(tz => html`<option key=${tz} value=${tz}>${tz}</option>`)}
                    </select>
                </div>
            </div>

            <div class="mt-4">
                <button class=${btnPrimary} onClick=${handleSave} disabled=${saving}>
                    ${saving ? 'Saving...' : 'Save Profile'}
                </button>
            </div>

            <${FeedbackBanner} message=${feedback?.message} type=${feedback?.type} />
        </div>
    `;
}

// ══════════════════════════════════════════════════════════════════════════
// User Settings — Change Password
// ══════════════════════════════════════════════════════════════════════════

function ChangePasswordCard() {
    const [currentPassword, setCurrentPassword] = useState('');
    const [newPassword, setNewPassword] = useState('');
    const [confirmPassword, setConfirmPassword] = useState('');
    const [saving, setSaving] = useState(false);
    const [feedback, setFeedback] = useState(null);

    const handleSubmit = useCallback(async () => {
        setFeedback(null);
        if (!currentPassword || !newPassword) {
            setFeedback({ type: 'error', message: 'All fields are required' });
            return;
        }
        if (newPassword !== confirmPassword) {
            setFeedback({ type: 'error', message: 'New passwords do not match' });
            return;
        }
        if (newPassword.length < 8) {
            setFeedback({ type: 'error', message: 'New password must be at least 8 characters' });
            return;
        }
        setSaving(true);
        try {
            await api.changePassword({ current_password: currentPassword, new_password: newPassword });
            setCurrentPassword('');
            setNewPassword('');
            setConfirmPassword('');
            setFeedback({ type: 'success', message: 'Password changed' });
        } catch (e) {
            setFeedback({ type: 'error', message: e.message });
        } finally {
            setSaving(false);
        }
    }, [currentPassword, newPassword, confirmPassword]);

    return html`
        <div class=${cardClass} style=${cardStyle}>
            <h2 class="text-base font-medium mb-1" style="color: var(--text-primary)">Change Password</h2>
            <p class="text-sm mb-4" style="color: var(--text-muted)">
                Update your login password.
            </p>

            <div class="space-y-3" style="max-width: 400px;">
                <div>
                    <label class="text-xs font-medium" style="color: var(--text-muted)">Current Password</label>
                    <input type="password" class=${inputClass + ' mt-1'} style=${inputStyle}
                        value=${currentPassword} onInput=${(e) => setCurrentPassword(e.target.value)} />
                </div>
                <div>
                    <label class="text-xs font-medium" style="color: var(--text-muted)">New Password</label>
                    <input type="password" class=${inputClass + ' mt-1'} style=${inputStyle}
                        value=${newPassword} onInput=${(e) => setNewPassword(e.target.value)} />
                </div>
                <div>
                    <label class="text-xs font-medium" style="color: var(--text-muted)">Confirm New Password</label>
                    <input type="password" class=${inputClass + ' mt-1'} style=${inputStyle}
                        value=${confirmPassword} onInput=${(e) => setConfirmPassword(e.target.value)}
                        onKeyDown=${(e) => e.key === 'Enter' && handleSubmit()} />
                </div>
            </div>

            <div class="mt-4">
                <button class=${btnPrimary} onClick=${handleSubmit}
                    disabled=${saving || !currentPassword || !newPassword || !confirmPassword}>
                    ${saving ? 'Changing...' : 'Change Password'}
                </button>
            </div>

            <${FeedbackBanner} message=${feedback?.message} type=${feedback?.type} />
        </div>
    `;
}

// ══════════════════════════════════════════════════════════════════════════
// Main Settings component
// ══════════════════════════════════════════════════════════════════════════

export function Settings() {
    // Push notification state (existing)
    const [push, setPush] = useState({ supported: false, subscribed: false, subscription: null });
    const [pushLoading, setPushLoading] = useState(false);
    const [pushError, setPushError] = useState(null);
    const [settings, setSettings] = useState(null);
    const [settingsSaving, setSettingsSaving] = useState(false);
    const [settingsError, setSettingsError] = useState(null);
    const [saved, setSaved] = useState(false);

    // User settings state
    const [userSettings, setUserSettings] = useState(null);
    const [userLoading, setUserLoading] = useState(true);
    const [userError, setUserError] = useState(null);

    // Instance settings state (only loaded for admins)
    const [instanceSettings, setInstanceSettings] = useState(null);
    const [instanceLoading, setInstanceLoading] = useState(false);
    const [instanceError, setInstanceError] = useState(null);

    const isAdmin = userSettings?.profile?.role === 'owner' || userSettings?.profile?.role === 'admin';

    // Load user settings on mount
    const loadUserSettings = useCallback(async () => {
        try {
            const data = await api.getUserSettings();
            setUserSettings(data);
            setUserError(null);
        } catch (e) {
            setUserError(e.message);
        } finally {
            setUserLoading(false);
        }
    }, []);

    // Load instance settings (admin only)
    const loadInstanceSettings = useCallback(async () => {
        setInstanceLoading(true);
        try {
            const data = await api.getInstanceSettings();
            setInstanceSettings(data);
            setInstanceError(null);
        } catch (e) {
            if (!e.message.includes('Forbidden')) {
                setInstanceError(e.message);
            }
        } finally {
            setInstanceLoading(false);
        }
    }, []);

    useEffect(() => {
        loadUserSettings();
        getPushState().then(setPush);
        api.getNotificationSettings()
            .then(setSettings)
            .catch(e => setSettingsError(e.message));
    }, []);

    // Load instance settings once we know the user is admin
    useEffect(() => {
        if (isAdmin) loadInstanceSettings();
    }, [isAdmin]);

    // Existing push/notification handlers
    const handlePushToggle = useCallback(async () => {
        setPushError(null);
        setPushLoading(true);
        try {
            if (push.subscribed) {
                await unsubscribePush(push.subscription);
                setPush(await getPushState());
            } else {
                const perm = await Notification.requestPermission();
                if (perm !== 'granted') {
                    throw new Error('Notification permission denied. Enable it in browser settings.');
                }
                await subscribePush();
                setPush(await getPushState());
            }
        } catch (e) {
            setPushError(e.message);
        } finally {
            setPushLoading(false);
        }
    }, [push]);

    const handleSettingToggle = useCallback(async (key, value) => {
        setSaved(false);
        setSettingsError(null);
        const updated = { ...settings, [key]: value };
        setSettings(updated);
        setSettingsSaving(true);
        try {
            const saved = await api.updateNotificationSettings({ [key]: value });
            setSettings(saved);
            setSaved(true);
            setTimeout(() => setSaved(false), 2000);
        } catch (e) {
            setSettingsError(e.message);
            setSettings(settings);
        } finally {
            setSettingsSaving(false);
        }
    }, [settings]);

    const notifDisabled = !push.subscribed;

    if (userLoading) {
        return html`<div class="px-6 py-8">
            <h1 class="text-xl font-semibold mb-6" style="color: var(--text-primary)">Settings</h1>
            <div class="flex items-center gap-3 p-8">
                <span class="loading-spinner"></span>
                <span class="text-sm" style="color: var(--text-muted)">Loading settings...</span>
            </div>
        </div>`;
    }

    return html`
        <div class="px-6 py-8" style="max-width: 800px;">
            <h1 class="text-xl font-semibold mb-6" style="color: var(--text-primary)">Settings</h1>

            ${userError && html`
                <div class="text-sm text-red-400 mb-4">Failed to load user settings: ${userError}</div>
            `}

            <!-- ═══ Instance Settings (admin/owner only) ═══ -->
            ${isAdmin && html`
                <div class="mb-8">
                    <h2 class="text-lg font-semibold mb-4" style="color: var(--text-primary); border-bottom: 1px solid var(--border-primary); padding-bottom: 0.5rem;">
                        Instance Settings
                    </h2>

                    ${instanceLoading && html`
                        <div class="flex items-center gap-3 p-4">
                            <span class="loading-spinner"></span>
                            <span class="text-sm" style="color: var(--text-muted)">Loading instance settings...</span>
                        </div>
                    `}

                    ${instanceError && html`
                        <div class="text-sm text-red-400 mb-4">${instanceError}</div>
                    `}

                    ${instanceSettings && html`
                        <${GitHubCard}
                            github=${instanceSettings.github}
                            onSaved=${loadInstanceSettings}
                        />
                        <${OAuthCard}
                            oauth=${instanceSettings.oauth}
                            onRegenerated=${loadInstanceSettings}
                        />
                    `}
                </div>
            `}

            <!-- ═══ User Settings ═══ -->
            <div class="mb-8">
                <h2 class="text-lg font-semibold mb-4" style="color: var(--text-primary); border-bottom: 1px solid var(--border-primary); padding-bottom: 0.5rem;">
                    User Settings
                </h2>

                ${userSettings && html`
                    <${AnthropicKeyCard}
                        anthropic=${userSettings.anthropic}
                        onSaved=${loadUserSettings}
                    />

                    <${ProfileCard}
                        profile=${userSettings.profile}
                        onSaved=${loadUserSettings}
                    />

                    <${ChangePasswordCard} />
                `}
            </div>

            <!-- ═══ Notifications (existing) ═══ -->
            <div class="mb-8">
                <h2 class="text-lg font-semibold mb-4" style="color: var(--text-primary); border-bottom: 1px solid var(--border-primary); padding-bottom: 0.5rem;">
                    Notifications
                </h2>

                <!-- Push notifications card -->
                <div class=${cardClass} style=${cardStyle}>
                    <h2 class="text-base font-medium mb-1" style="color: var(--text-primary)">Push Notifications</h2>
                    <p class="text-sm mb-4" style="color: var(--text-muted)">
                        Receive browser notifications when tasks complete, fail, or need attention.
                        This browser/device only.
                    </p>

                    ${!push.supported && html`
                        <p class="text-sm text-amber-400">
                            Push notifications are not supported in this browser.
                        </p>
                    `}

                    ${push.supported && !push.serverConfigured && html`
                        <p class="text-sm text-amber-400">
                            Push notifications require server configuration (VAPID keys not set).
                        </p>
                    `}

                    ${push.supported && push.serverConfigured && html`
                        <div class="flex items-center justify-between">
                            <div>
                                <div class="text-sm font-medium" style="color: var(--text-primary)">Enable push notifications</div>
                                <div class="text-xs mt-0.5" style="color: var(--text-muted)">
                                    ${push.subscribed ? 'Active on this device' : 'Not subscribed'}
                                </div>
                            </div>
                            <${Toggle}
                                checked=${push.subscribed}
                                onChange=${handlePushToggle}
                                disabled=${pushLoading}
                            />
                        </div>
                    `}

                    ${pushError && html`
                        <p class="text-sm text-red-400 mt-3">${pushError}</p>
                    `}
                </div>

                <!-- Notification types card -->
                <div class=${cardClass} style=${cardStyle}>
                    <div class="flex items-center justify-between mb-1">
                        <h2 class="text-base font-medium" style="color: var(--text-primary)">Notification Types</h2>
                        ${saved && html`<span class="text-xs text-emerald-400">Saved</span>`}
                    </div>
                    <p class="text-sm mb-4" style="color: var(--text-muted)">
                        Choose which events trigger a push notification.
                        ${notifDisabled ? html`<span style="color: var(--text-muted)"> Enable push above first.</span>` : ''}
                    </p>

                    ${!settings && !settingsError && html`
                        <p class="text-sm" style="color: var(--text-muted)">Loading\u2026</p>
                    `}

                    ${settingsError && html`
                        <p class="text-sm text-red-400">${settingsError}</p>
                    `}

                    ${settings && html`
                        <div class="space-y-4">
                            <${SettingRow}
                                label="Task failed"
                                description="Task ends with an error or exception."
                                checked=${!!settings.notify_failed}
                                disabled=${notifDisabled || settingsSaving}
                                onChange=${(v) => handleSettingToggle('notify_failed', v)}
                            />
                            <${SettingRow}
                                label="Needs review"
                                description="Task timed out, lost its process, or ended without a result."
                                checked=${!!settings.notify_needs_review}
                                disabled=${notifDisabled || settingsSaving}
                                onChange=${(v) => handleSettingToggle('notify_needs_review', v)}
                            />
                            <${SettingRow}
                                label="CC posted a question"
                                description="Task is paused waiting for your input."
                                checked=${!!settings.notify_question}
                                disabled=${notifDisabled || settingsSaving}
                                onChange=${(v) => handleSettingToggle('notify_question', v)}
                            />
                            <${SettingRow}
                                label="Task completed"
                                description="Task finishes successfully. Off by default to reduce noise."
                                checked=${!!settings.notify_completed}
                                disabled=${notifDisabled || settingsSaving}
                                onChange=${(v) => handleSettingToggle('notify_completed', v)}
                            />
                        </div>
                    `}
                </div>
            </div>
        </div>
    `;
}

function SettingRow({ label, description, checked, disabled, onChange }) {
    return html`
        <div class="flex items-start justify-between gap-4">
            <div>
                <div class="text-sm" style="color: var(--text-primary)">${label}</div>
                <div class="text-xs mt-0.5" style="color: var(--text-muted)">${description}</div>
            </div>
            <div class="mt-0.5 flex-shrink-0">
                <${Toggle} checked=${checked} disabled=${disabled} onChange=${onChange} />
            </div>
        </div>
    `;
}
