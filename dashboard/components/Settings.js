import { html } from './utils.js';
import { useState, useEffect, useCallback, useRef } from 'https://esm.sh/preact@10.25.4/hooks';
import { api } from '../api.js';

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
    background: 'var(--f-surface)',
    border: '1px solid var(--f-border)',
    color: 'var(--f-text)',
};
const btnPrimary = 'px-3 py-1.5 text-xs rounded-md bg-indigo-600 text-white hover:bg-indigo-500 disabled:opacity-50 disabled:cursor-not-allowed';
const btnDanger = 'px-3 py-1.5 text-xs rounded-md disabled:opacity-50 disabled:cursor-not-allowed';
const btnDangerStyle = {
    background: 'transparent',
    border: '1px solid var(--f-red)',
    color: 'var(--f-red)',
};
const btnSecondary = 'px-3 py-1.5 text-xs rounded-md disabled:opacity-50 disabled:cursor-not-allowed';
const btnSecondaryStyle = {
    background: 'var(--f-surface)',
    border: '1px solid var(--f-border)',
    color: 'var(--f-text-secondary)',
};
const cardStyle = {
    background: 'transparent',
    border: '0.5px solid var(--f-border)',
    borderRadius: '8px',
    padding: '16px',
    marginBottom: '12px',
};
const sectionLabelStyle = {
    fontSize: '11px',
    fontWeight: '500',
    letterSpacing: '0.05em',
    textTransform: 'uppercase',
    color: 'var(--f-text-tertiary)',
    marginBottom: '12px',
};
const cardTitleStyle = {
    fontSize: '14px',
    fontWeight: '500',
    color: 'var(--f-text)',
    margin: '0',
};
const bodyTextStyle = {
    fontSize: '13px',
    color: 'var(--f-text-secondary)',
};
const labelStyle = {
    fontSize: '12px',
    fontWeight: '500',
    color: 'var(--f-text-secondary)',
};
const monoStyle = {
    fontSize: '13px',
    fontFamily: 'monospace',
    color: 'var(--f-text-secondary)',
    background: 'var(--f-surface)',
    border: '1px solid var(--f-border)',
    borderRadius: '6px',
    padding: '6px 10px',
};

// ── Feedback banner ───────────────────────────────────────────────────────

function FeedbackBanner({ message, type = 'success' }) {
    if (!message) return null;
    const color = type === 'success' ? 'var(--f-green)' : 'var(--f-red)';
    return html`<div style="font-size: 12px; color: ${color}; margin-top: 8px;">${message}</div>`;
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
        style=${{
            padding: '2px 8px',
            fontSize: '11px',
            borderRadius: '4px',
            background: 'var(--f-surface)',
            border: '1px solid var(--f-border)',
            color: copied ? 'var(--f-green)' : 'var(--f-text-tertiary)',
            cursor: 'pointer',
        }}
        >${copied ? 'Copied!' : 'Copy'}</button>`;
}

// ══════════════════════════════════════════════════════════════════════════
// Compact credential card (GitHub / Anthropic pattern)
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
        <div style=${cardStyle}>
            ${!editing && html`
                <div style="display: flex; align-items: center; justify-content: space-between;">
                    <div style="display: flex; align-items: center; gap: 10px;">
                        <span style="font-size: 16px;">🐙</span>
                        <span style=${cardTitleStyle}>GitHub</span>
                        ${github.connected && html`
                            <span style="font-size: 12px; color: var(--f-text-secondary);">Connected${github.username ? ` as ${github.username}` : ''}</span>
                        `}
                        ${!github.connected && github.pat_last4 && html`
                            <span style="font-size: 12px; color: var(--f-yellow);">Not connected</span>
                        `}
                        ${github.pat_last4 && html`
                            <span style="font-size: 12px; font-family: monospace; color: var(--f-text-tertiary);">····${github.pat_last4}</span>
                        `}
                    </div>
                    <div style="display: flex; align-items: center; gap: 8px;">
                        ${github.connected && html`
                            <span style="display: inline-block; width: 7px; height: 7px; border-radius: 50%; background: var(--f-green);"></span>
                        `}
                        <button class=${btnSecondary} style=${btnSecondaryStyle}
                            onClick=${() => setEditing(true)}>Update</button>
                        ${github.pat_last4 && html`
                            <button class=${btnSecondary} style=${btnSecondaryStyle}
                                onClick=${handleTest} disabled=${testing}>
                                ${testing ? 'Testing…' : 'Test'}
                            </button>
                        `}
                    </div>
                </div>
            `}

            ${editing && html`
                <div style="display: flex; align-items: center; gap: 10px; margin-bottom: 10px;">
                    <span style="font-size: 16px;">🐙</span>
                    <span style=${cardTitleStyle}>GitHub</span>
                </div>
                <div style="display: flex; gap: 8px;">
                    <input type="password" class=${inputClass} style=${inputStyle}
                        placeholder="ghp_xxxxxxxxxxxx"
                        value=${pat} onInput=${(e) => setPat(e.target.value)}
                        onKeyDown=${(e) => e.key === 'Enter' && handleSave()} />
                    <button class=${btnPrimary} onClick=${handleSave} disabled=${saving || !pat.trim()}>
                        ${saving ? 'Saving…' : (github.pat_last4 ? 'Update' : 'Connect')}
                    </button>
                    ${github.pat_last4 && html`
                        <button class=${btnSecondary} style=${btnSecondaryStyle}
                            onClick=${() => { setEditing(false); setPat(''); setFeedback(null); }}>Cancel</button>
                    `}
                </div>
            `}

            <${FeedbackBanner} message=${feedback?.message} type=${feedback?.type} />
        </div>
    `;
}

// ══════════════════════════════════════════════════════════════════════════
// OAuth / MCP Connection
// ══════════════════════════════════════════════════════════════════════════

function OAuthCard({ oauth, onRegenerated }) {
    const [showSecret, setShowSecret] = useState(false);
    const [confirmRegen, setConfirmRegen] = useState(false);
    const [regenerating, setRegenerating] = useState(false);
    const [feedback, setFeedback] = useState(null);

    const handleRegenerate = useCallback(async () => {
        setConfirmRegen(false);
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
            <div style=${cardStyle}>
                <div style=${cardTitleStyle}>OAuth / MCP Connection</div>
                <div style=${{ ...bodyTextStyle, marginTop: '4px' }}>OAuth client not configured.</div>
            </div>
        `;
    }

    const maskedSecret = oauth.client_secret ? '•'.repeat(Math.min(oauth.client_secret.length, 32)) : '';

    return html`
        <div style=${cardStyle}>
            <div style=${{ ...cardTitleStyle, marginBottom: '12px' }}>OAuth / MCP Connection</div>

            <div style="display: flex; align-items: center; justify-content: space-between; padding: 8px 0; border-bottom: 0.5px solid var(--f-border-subtle);">
                <div style="display: flex; align-items: center; gap: 8px;">
                    <span style=${labelStyle}>Client ID</span>
                    <code style="font-size: 13px; font-family: monospace; color: var(--f-text-secondary);">${oauth.client_id}</code>
                </div>
                <${CopyButton} value=${oauth.client_id} />
            </div>

            <div style="display: flex; align-items: center; justify-content: space-between; padding: 8px 0;">
                <div style="display: flex; align-items: center; gap: 8px; min-width: 0;">
                    <span style=${labelStyle}>Client Secret</span>
                    <code style="font-size: 13px; font-family: monospace; color: var(--f-text-secondary); overflow: hidden; text-overflow: ellipsis; white-space: nowrap;">
                        ${showSecret ? oauth.client_secret : maskedSecret}
                    </code>
                </div>
                <div style="display: flex; align-items: center; gap: 6px; flex-shrink: 0;">
                    <button onClick=${() => setShowSecret(!showSecret)}
                        style=${{
                            padding: '2px 8px',
                            fontSize: '11px',
                            borderRadius: '4px',
                            background: 'var(--f-surface)',
                            border: '1px solid var(--f-border)',
                            color: 'var(--f-text-tertiary)',
                            cursor: 'pointer',
                        }}
                    >${showSecret ? 'Hide' : 'Show'}</button>
                    <${CopyButton} value=${oauth.client_secret} />
                </div>
            </div>

            <div style="margin-top: 12px; display: flex; align-items: center; gap: 12px;">
                ${!confirmRegen && html`
                    <button class=${btnDanger} style=${btnDangerStyle}
                        onClick=${() => setConfirmRegen(true)} disabled=${regenerating}>
                        ${regenerating ? 'Regenerating…' : 'Regenerate Secret'}
                    </button>
                    <span style="font-size: 11px; color: var(--f-text-tertiary);">Disconnects existing MCP connections</span>
                `}
                ${confirmRegen && html`
                    <span style="font-size: 12px; color: var(--f-red);">Are you sure?</span>
                    <button class=${btnDanger} style=${btnDangerStyle}
                        onClick=${handleRegenerate}>Yes, regenerate</button>
                    <button class=${btnSecondary} style=${btnSecondaryStyle}
                        onClick=${() => setConfirmRegen(false)}>Cancel</button>
                `}
            </div>

            <${FeedbackBanner} message=${feedback?.message} type=${feedback?.type} />
        </div>
    `;
}

// ══════════════════════════════════════════════════════════════════════════
// Anthropic API Key — compact single-line
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
        <div style=${cardStyle}>
            ${!editing && html`
                <div style="display: flex; align-items: center; justify-content: space-between;">
                    <div style="display: flex; align-items: center; gap: 10px;">
                        <span style="font-size: 16px;">🔑</span>
                        <span style=${cardTitleStyle}>Anthropic API Key</span>
                        ${anthropic.configured && html`
                            <span style="font-size: 12px; color: var(--f-text-secondary);">Configured</span>
                        `}
                        ${anthropic.key_last4 && html`
                            <span style="font-size: 12px; font-family: monospace; color: var(--f-text-tertiary);">····${anthropic.key_last4}</span>
                        `}
                    </div>
                    <div style="display: flex; align-items: center; gap: 8px;">
                        ${anthropic.configured && html`
                            <span style="display: inline-block; width: 7px; height: 7px; border-radius: 50%; background: var(--f-green);"></span>
                        `}
                        <button class=${btnSecondary} style=${btnSecondaryStyle}
                            onClick=${() => setEditing(true)}>Update</button>
                        ${anthropic.configured && html`
                            <button class=${btnSecondary} style=${btnSecondaryStyle}
                                onClick=${handleTest} disabled=${testing}>
                                ${testing ? 'Testing…' : 'Test'}
                            </button>
                        `}
                    </div>
                </div>
            `}

            ${editing && html`
                <div style="display: flex; align-items: center; gap: 10px; margin-bottom: 10px;">
                    <span style="font-size: 16px;">🔑</span>
                    <span style=${cardTitleStyle}>Anthropic API Key</span>
                </div>
                <div style="display: flex; gap: 8px;">
                    <input type="password" class=${inputClass} style=${inputStyle}
                        placeholder="sk-ant-xxxxxxxxxxxx"
                        value=${key} onInput=${(e) => setKey(e.target.value)}
                        onKeyDown=${(e) => e.key === 'Enter' && handleSave()} />
                    <button class=${btnPrimary} onClick=${handleSave} disabled=${saving || !key.trim()}>
                        ${saving ? 'Saving…' : 'Save'}
                    </button>
                    ${anthropic.configured && html`
                        <button class=${btnSecondary} style=${btnSecondaryStyle}
                            onClick=${() => { setEditing(false); setKey(''); setFeedback(null); }}>Cancel</button>
                    `}
                </div>
            `}

            <${FeedbackBanner} message=${feedback?.message} type=${feedback?.type} />
        </div>
    `;
}

// ══════════════════════════════════════════════════════════════════════════
// Profile — two-column grid
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
        <div style=${cardStyle}>
            <div style=${{ ...cardTitleStyle, marginBottom: '12px' }}>Profile</div>

            <div style="display: grid; grid-template-columns: 1fr 1fr; gap: 12px;">
                <div>
                    <label style=${labelStyle}>Name</label>
                    <input type="text" class=${inputClass} style=${{ ...inputStyle, marginTop: '4px' }}
                        value=${name} onInput=${(e) => setName(e.target.value)} />
                </div>
                <div>
                    <label style=${labelStyle}>Email</label>
                    <input type="email" class=${inputClass} style=${{ ...inputStyle, marginTop: '4px' }}
                        value=${email} onInput=${(e) => setEmail(e.target.value)} />
                </div>
            </div>

            <div style="margin-top: 12px;">
                <label style=${labelStyle}>Timezone</label>
                <select class=${inputClass} style=${{ ...inputStyle, marginTop: '4px' }}
                    value=${timezone} onChange=${(e) => setTimezone(e.target.value)}>
                    <option value="">Select timezone…</option>
                    ${TIMEZONES.map(tz => html`<option key=${tz} value=${tz}>${tz}</option>`)}
                </select>
            </div>

            <div style="margin-top: 14px;">
                <button class=${btnPrimary} onClick=${handleSave} disabled=${saving}>
                    ${saving ? 'Saving…' : 'Save profile'}
                </button>
            </div>

            <${FeedbackBanner} message=${feedback?.message} type=${feedback?.type} />
        </div>
    `;
}

// ══════════════════════════════════════════════════════════════════════════
// Change Password — contained width
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
        <div style=${cardStyle}>
            <div style=${{ ...cardTitleStyle, marginBottom: '12px' }}>Change Password</div>

            <div style="max-width: 280px; display: flex; flex-direction: column; gap: 10px;">
                <div>
                    <label style=${labelStyle}>Current Password</label>
                    <input type="password" class=${inputClass} style=${{ ...inputStyle, marginTop: '4px' }}
                        value=${currentPassword} onInput=${(e) => setCurrentPassword(e.target.value)} />
                </div>
                <div>
                    <label style=${labelStyle}>New Password</label>
                    <input type="password" class=${inputClass} style=${{ ...inputStyle, marginTop: '4px' }}
                        value=${newPassword} onInput=${(e) => setNewPassword(e.target.value)} />
                </div>
                <div>
                    <label style=${labelStyle}>Confirm New Password</label>
                    <input type="password" class=${inputClass} style=${{ ...inputStyle, marginTop: '4px' }}
                        value=${confirmPassword} onInput=${(e) => setConfirmPassword(e.target.value)}
                        onKeyDown=${(e) => e.key === 'Enter' && handleSubmit()} />
                </div>
            </div>

            <div style="margin-top: 14px;">
                <button class=${btnPrimary} onClick=${handleSubmit}
                    disabled=${saving || !currentPassword || !newPassword || !confirmPassword}>
                    ${saving ? 'Updating…' : 'Update password'}
                </button>
            </div>

            <${FeedbackBanner} message=${feedback?.message} type=${feedback?.type} />
        </div>
    `;
}

// ══════════════════════════════════════════════════════════════════════════
// Notification checkbox row
// ══════════════════════════════════════════════════════════════════════════

function NotifCheckbox({ label, description, checked, disabled, onChange }) {
    return html`
        <label style="display: flex; align-items: flex-start; gap: 10px; cursor: ${disabled ? 'not-allowed' : 'pointer'}; opacity: ${disabled ? '0.5' : '1'};">
            <input type="checkbox"
                checked=${checked}
                disabled=${disabled}
                onChange=${(e) => onChange(e.target.checked)}
                style="margin-top: 2px; accent-color: var(--f-accent);"
            />
            <div>
                <div style="font-size: 13px; font-weight: 500; color: var(--f-text);">${label}</div>
                <div style="font-size: 12px; color: var(--f-text-tertiary); margin-top: 1px;">${description}</div>
            </div>
        </label>
    `;
}

// ══════════════════════════════════════════════════════════════════════════
// Main Settings component
// ══════════════════════════════════════════════════════════════════════════

export function Settings() {
    const [push, setPush] = useState({ supported: false, subscribed: false, subscription: null });
    const [pushLoading, setPushLoading] = useState(false);
    const [pushError, setPushError] = useState(null);
    const [settings, setSettings] = useState(null);
    const [settingsSaving, setSettingsSaving] = useState(false);
    const [settingsError, setSettingsError] = useState(null);
    const [saved, setSaved] = useState(false);

    const [userSettings, setUserSettings] = useState(null);
    const [userLoading, setUserLoading] = useState(true);
    const [userError, setUserError] = useState(null);

    const [instanceSettings, setInstanceSettings] = useState(null);
    const [instanceLoading, setInstanceLoading] = useState(false);
    const [instanceError, setInstanceError] = useState(null);

    const isAdmin = userSettings?.profile?.role === 'owner' || userSettings?.profile?.role === 'admin';

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

    useEffect(() => {
        if (isAdmin) loadInstanceSettings();
    }, [isAdmin]);

    const handlePushToggle = useCallback(async (enable) => {
        setPushError(null);
        setPushLoading(true);
        try {
            if (!enable) {
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
        return html`<div style="padding: 24px; max-width: 800px;">
            <div style="display: flex; align-items: center; gap: 12px; padding: 32px 0;">
                <span class="loading-spinner"></span>
                <span style="font-size: 13px; color: var(--f-text-secondary);">Loading settings…</span>
            </div>
        </div>`;
    }

    return html`
        <div style="padding: 24px; max-width: 800px;">

            ${userError && html`
                <div style="font-size: 13px; color: var(--f-red); margin-bottom: 16px;">Failed to load user settings: ${userError}</div>
            `}

            <!-- ═══ INSTANCE section (admin/owner only) ═══ -->
            ${isAdmin && html`
                <div style="margin-bottom: 24px;">
                    <div style=${sectionLabelStyle}>INSTANCE</div>

                    ${instanceLoading && html`
                        <div style="display: flex; align-items: center; gap: 12px; padding: 16px 0;">
                            <span class="loading-spinner"></span>
                            <span style="font-size: 13px; color: var(--f-text-secondary);">Loading…</span>
                        </div>
                    `}

                    ${instanceError && html`
                        <div style="font-size: 13px; color: var(--f-red); margin-bottom: 12px;">${instanceError}</div>
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

            <!-- ═══ ACCOUNT section ═══ -->
            <div style="margin-bottom: 24px;">
                <div style=${sectionLabelStyle}>ACCOUNT</div>

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

            <!-- ═══ NOTIFICATIONS section ═══ -->
            <div style="margin-bottom: 24px;">
                <div style=${sectionLabelStyle}>NOTIFICATIONS</div>

                <div style=${cardStyle}>
                    <!-- Push toggle -->
                    ${!push.supported && html`
                        <div style="font-size: 13px; color: var(--f-yellow);">
                            Push notifications are not supported in this browser.
                        </div>
                    `}

                    ${push.supported && !push.serverConfigured && html`
                        <div style="font-size: 13px; color: var(--f-yellow);">
                            Push notifications require server configuration (VAPID keys not set).
                        </div>
                    `}

                    ${push.supported && push.serverConfigured && html`
                        <${NotifCheckbox}
                            label="Push notifications"
                            description=${push.subscribed ? 'Active on this device' : 'Receive browser notifications. This device only.'}
                            checked=${push.subscribed}
                            disabled=${pushLoading}
                            onChange=${handlePushToggle}
                        />

                        ${pushError && html`
                            <div style="font-size: 12px; color: var(--f-red); margin-top: 6px; margin-left: 26px;">${pushError}</div>
                        `}

                        ${push.subscribed && settings && html`
                            <div style="margin-top: 14px; padding-top: 12px; border-top: 0.5px solid var(--f-border-subtle); display: flex; flex-direction: column; gap: 10px;">
                                ${saved && html`<span style="font-size: 11px; color: var(--f-green); align-self: flex-end;">Saved</span>`}
                                ${settingsError && html`
                                    <div style="font-size: 12px; color: var(--f-red);">${settingsError}</div>
                                `}
                                <${NotifCheckbox}
                                    label="Task failed"
                                    description="Task ends with an error or exception."
                                    checked=${!!settings.notify_failed}
                                    disabled=${settingsSaving}
                                    onChange=${(v) => handleSettingToggle('notify_failed', v)}
                                />
                                <${NotifCheckbox}
                                    label="Needs review"
                                    description="Task timed out, lost its process, or ended without a result."
                                    checked=${!!settings.notify_needs_review}
                                    disabled=${settingsSaving}
                                    onChange=${(v) => handleSettingToggle('notify_needs_review', v)}
                                />
                                <${NotifCheckbox}
                                    label="CC posted a question"
                                    description="Task is paused waiting for your input."
                                    checked=${!!settings.notify_question}
                                    disabled=${settingsSaving}
                                    onChange=${(v) => handleSettingToggle('notify_question', v)}
                                />
                                <${NotifCheckbox}
                                    label="Task completed"
                                    description="Task finishes successfully. Off by default to reduce noise."
                                    checked=${!!settings.notify_completed}
                                    disabled=${settingsSaving}
                                    onChange=${(v) => handleSettingToggle('notify_completed', v)}
                                />
                            </div>
                        `}

                        ${push.subscribed && !settings && !settingsError && html`
                            <div style="font-size: 13px; color: var(--f-text-secondary); margin-top: 12px;">Loading notification preferences…</div>
                        `}

                        ${!push.subscribed && html`
                            <div style="font-size: 12px; color: var(--f-text-tertiary); margin-top: 6px; margin-left: 26px;">Enable push above to configure notification types.</div>
                        `}
                    `}
                </div>
            </div>
        </div>
    `;
}
