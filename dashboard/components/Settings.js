import { html } from './utils.js';
import { useState, useEffect, useCallback, useRef } from 'https://esm.sh/preact@10.25.4/hooks';
import { api } from '../api.js';
import { colors } from '../tokens.js';

// ── Shared styles ────────────────────────────────────────────────────────

const styles = {
    sectionLabel: {
        fontSize: '11px',
        fontWeight: 500,
        letterSpacing: '0.08em',
        textTransform: 'uppercase',
        color: 'var(--f-text-tertiary)',
        marginBottom: '12px',
    },
    card: {
        background: 'transparent',
        border: '0.5px solid var(--f-border)',
        borderRadius: '8px',
        padding: '16px',
        marginBottom: '8px',
    },
    cardTitle: {
        fontSize: '14px',
        fontWeight: 500,
        color: 'var(--f-text)',
        margin: 0,
    },
    cardDesc: {
        fontSize: '13px',
        color: 'var(--f-text-secondary)',
        margin: '2px 0 0 0',
    },
    input: {
        background: 'var(--f-surface)',
        border: '1px solid var(--f-border)',
        borderRadius: '6px',
        color: 'var(--f-text)',
        fontSize: '13px',
        padding: '7px 10px',
        width: '100%',
        outline: 'none',
        boxSizing: 'border-box',
    },
    btnPrimary: {
        background: 'var(--f-accent)',
        color: '#fff',
        border: 'none',
        borderRadius: '6px',
        fontSize: '13px',
        fontWeight: 500,
        padding: '7px 14px',
        cursor: 'pointer',
    },
    btnSecondary: {
        background: 'transparent',
        color: 'var(--f-text-secondary)',
        border: '1px solid var(--f-border)',
        borderRadius: '6px',
        fontSize: '12px',
        fontWeight: 500,
        padding: '5px 12px',
        cursor: 'pointer',
    },
    btnDanger: {
        background: 'transparent',
        color: 'var(--f-red)',
        border: '1px solid color-mix(in srgb, var(--f-red) 40%, transparent)',
        borderRadius: '6px',
        fontSize: '12px',
        fontWeight: 500,
        padding: '5px 12px',
        cursor: 'pointer',
    },
    mono: {
        fontFamily: "'JetBrains Mono', 'Fira Code', monospace",
        fontSize: '12px',
    },
    greenDot: {
        width: '6px',
        height: '6px',
        borderRadius: '50%',
        background: colors.green,
        display: 'inline-block',
        flexShrink: 0,
    },
    successText: { color: colors.green, fontSize: '12px' },
    errorText: { color: colors.red, fontSize: '12px' },
    savedBadge: { color: colors.green, fontSize: '11px', fontWeight: 500 },
};


// ── Toggle (reused from old code, restyled) ──────────────────────────────

function Toggle({ checked, onChange, disabled = false }) {
    const bg = checked ? 'var(--f-accent)' : 'var(--f-border)';
    return html`
        <button
            role="switch"
            aria-checked=${checked}
            disabled=${disabled}
            onClick=${() => !disabled && onChange(!checked)}
            style=${{
                position: 'relative', display: 'inline-flex', alignItems: 'center',
                width: '36px', height: '20px', borderRadius: '10px',
                background: bg, border: 'none', cursor: disabled ? 'not-allowed' : 'pointer',
                opacity: disabled ? 0.4 : 1, transition: 'background 0.15s',
                padding: 0, flexShrink: 0,
            }}
        >
            <span style=${{
                display: 'block', width: '14px', height: '14px', borderRadius: '50%',
                background: '#fff', transition: 'transform 0.15s',
                transform: checked ? 'translateX(19px)' : 'translateX(3px)',
            }} />
        </button>
    `;
}


// ── Credential card (GitHub / Anthropic) ─────────────────────────────────

function CredentialCard({ name, icon, field, masked, onUpdate, onTest }) {
    const [editing, setEditing] = useState(false);
    const [value, setValue] = useState('');
    const [saving, setSaving] = useState(false);
    const [testing, setTesting] = useState(false);
    const [testResult, setTestResult] = useState(null);
    const [error, setError] = useState(null);
    const inputRef = useRef(null);

    const connected = !!masked;

    useEffect(() => {
        if (editing && inputRef.current) inputRef.current.focus();
    }, [editing]);

    const handleSave = async () => {
        if (!value.trim()) return;
        setSaving(true);
        setError(null);
        try {
            await onUpdate(field, value.trim());
            setValue('');
            setEditing(false);
        } catch (e) {
            setError(e.message);
        } finally {
            setSaving(false);
        }
    };

    const handleTest = async () => {
        setTesting(true);
        setTestResult(null);
        try {
            const result = await onTest(field);
            setTestResult(result);
        } catch (e) {
            setTestResult({ ok: false, error: e.message });
        } finally {
            setTesting(false);
        }
    };

    return html`
        <div style=${styles.card}>
            <div style=${{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', gap: '12px' }}>
                <div style=${{ display: 'flex', alignItems: 'center', gap: '10px', minWidth: 0 }}>
                    <span style=${{ fontSize: '16px', flexShrink: 0 }}>${icon}</span>
                    <span style=${{ fontSize: '14px', fontWeight: 500, color: 'var(--f-text)' }}>${name}</span>
                    ${connected && html`
                        <span style=${{ display: 'flex', alignItems: 'center', gap: '6px' }}>
                            <span style=${styles.greenDot}></span>
                            <span style=${{ fontSize: '12px', color: 'var(--f-text-tertiary)' }}>Connected</span>
                        </span>
                    `}
                    ${connected && html`
                        <span style=${{ ...styles.mono, color: 'var(--f-text-tertiary)' }}>${masked}</span>
                    `}
                </div>
                <div style=${{ display: 'flex', alignItems: 'center', gap: '6px', flexShrink: 0 }}>
                    ${connected && html`
                        <button style=${styles.btnSecondary} onClick=${handleTest} disabled=${testing}>
                            ${testing ? 'Testing…' : 'Test'}
                        </button>
                    `}
                    <button style=${styles.btnSecondary} onClick=${() => { setEditing(!editing); setError(null); setTestResult(null); }}>
                        ${editing ? 'Cancel' : 'Update'}
                    </button>
                </div>
            </div>

            ${testResult && html`
                <div style=${{ marginTop: '8px' }}>
                    ${testResult.ok
                        ? html`<span style=${styles.successText}>${testResult.detail || 'Valid'}</span>`
                        : html`<span style=${styles.errorText}>${testResult.error || 'Failed'}</span>`
                    }
                </div>
            `}

            ${editing && html`
                <div style=${{ marginTop: '12px', display: 'flex', gap: '8px', alignItems: 'center' }}>
                    <input
                        ref=${inputRef}
                        type="password"
                        placeholder=${field === 'github_pat' ? 'ghp_...' : 'sk-ant-...'}
                        value=${value}
                        onInput=${e => setValue(e.target.value)}
                        onKeyDown=${e => e.key === 'Enter' && handleSave()}
                        style=${{ ...styles.input, flex: 1 }}
                    />
                    <button style=${styles.btnPrimary} onClick=${handleSave} disabled=${saving || !value.trim()}>
                        ${saving ? 'Saving…' : 'Save'}
                    </button>
                </div>
            `}

            ${error && html`<div style=${{ ...styles.errorText, marginTop: '8px' }}>${error}</div>`}
        </div>
    `;
}


// ── OAuth card ───────────────────────────────────────────────────────────

function OAuthCard({ oauth, onRegenerate }) {
    const [showSecret, setShowSecret] = useState(false);
    const [fullSecret, setFullSecret] = useState(null);
    const [confirmRegen, setConfirmRegen] = useState(false);
    const [regenerating, setRegenerating] = useState(false);
    const [copied, setCopied] = useState(null);

    const copyToClipboard = async (text, label) => {
        try {
            await navigator.clipboard.writeText(text);
            setCopied(label);
            setTimeout(() => setCopied(null), 2000);
        } catch {}
    };

    const handleRegenerate = async () => {
        setRegenerating(true);
        try {
            const result = await onRegenerate();
            setFullSecret(result.client_secret);
            setShowSecret(true);
            setConfirmRegen(false);
        } catch (e) {
            // error handling
        } finally {
            setRegenerating(false);
        }
    };

    const secretDisplay = showSecret && fullSecret
        ? fullSecret
        : (oauth?.client_secret_masked || '••••••••••••');

    return html`
        <div style=${styles.card}>
            <div style=${{ marginBottom: '4px' }}>
                <span style=${styles.cardTitle}>MCP Connection</span>
            </div>
            <p style=${{ ...styles.cardDesc, marginBottom: '14px' }}>OAuth 2.0 client for Claude MCP integration</p>

            <div style=${{ display: 'flex', flexDirection: 'column', gap: '10px' }}>
                <!-- Client ID row -->
                <div style=${{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', gap: '12px' }}>
                    <div style=${{ display: 'flex', alignItems: 'center', gap: '8px' }}>
                        <span style=${{ fontSize: '12px', color: 'var(--f-text-tertiary)', width: '80px' }}>Client ID</span>
                        <span style=${{ ...styles.mono, color: 'var(--f-text-secondary)' }}>${oauth?.client_id || '—'}</span>
                    </div>
                    ${oauth?.client_id && html`
                        <button
                            style=${styles.btnSecondary}
                            onClick=${() => copyToClipboard(oauth.client_id, 'id')}
                        >${copied === 'id' ? 'Copied!' : 'Copy'}</button>
                    `}
                </div>

                <!-- Client Secret row -->
                <div style=${{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', gap: '12px' }}>
                    <div style=${{ display: 'flex', alignItems: 'center', gap: '8px' }}>
                        <span style=${{ fontSize: '12px', color: 'var(--f-text-tertiary)', width: '80px' }}>Secret</span>
                        <span style=${{ ...styles.mono, color: 'var(--f-text-secondary)', wordBreak: 'break-all' }}>${secretDisplay}</span>
                    </div>
                    <div style=${{ display: 'flex', gap: '6px', flexShrink: 0 }}>
                        ${fullSecret && html`
                            <button style=${styles.btnSecondary} onClick=${() => setShowSecret(!showSecret)}>
                                ${showSecret ? 'Hide' : 'Show'}
                            </button>
                        `}
                        <button
                            style=${styles.btnSecondary}
                            onClick=${() => copyToClipboard(fullSecret || '', 'secret')}
                            disabled=${!fullSecret}
                        >${copied === 'secret' ? 'Copied!' : 'Copy'}</button>
                    </div>
                </div>
            </div>

            <!-- Regenerate -->
            <div style=${{ marginTop: '14px', borderTop: '0.5px solid var(--f-border)', paddingTop: '12px' }}>
                ${!confirmRegen
                    ? html`
                        <button style=${styles.btnDanger} onClick=${() => setConfirmRegen(true)}>
                            Regenerate Secret
                        </button>
                        <span style=${{ fontSize: '11px', color: 'var(--f-text-tertiary)', marginLeft: '8px' }}>
                            Disconnects existing MCP connections
                        </span>
                    `
                    : html`
                        <div style=${{ display: 'flex', alignItems: 'center', gap: '8px' }}>
                            <span style=${{ fontSize: '13px', color: 'var(--f-red)' }}>Are you sure?</span>
                            <button style=${styles.btnDanger} onClick=${handleRegenerate} disabled=${regenerating}>
                                ${regenerating ? 'Regenerating…' : 'Yes, regenerate'}
                            </button>
                            <button style=${styles.btnSecondary} onClick=${() => setConfirmRegen(false)}>Cancel</button>
                        </div>
                    `
                }
            </div>
        </div>
    `;
}


// ── Profile card ─────────────────────────────────────────────────────────

const TIMEZONES = [
    'America/Toronto', 'America/New_York', 'America/Chicago', 'America/Denver',
    'America/Los_Angeles', 'America/Vancouver', 'America/Halifax',
    'America/St_Johns', 'America/Winnipeg', 'America/Edmonton',
    'UTC', 'Europe/London', 'Europe/Paris', 'Europe/Berlin',
    'Asia/Tokyo', 'Asia/Shanghai', 'Australia/Sydney',
];

function ProfileCard({ profile: initial, onSave }) {
    const [profile, setProfile] = useState(initial);
    const [saving, setSaving] = useState(false);
    const [saved, setSaved] = useState(false);
    const [error, setError] = useState(null);

    useEffect(() => { setProfile(initial); }, [initial]);

    const handleSave = async () => {
        setSaving(true);
        setError(null);
        try {
            const updated = await onSave(profile);
            setProfile(updated);
            setSaved(true);
            setTimeout(() => setSaved(false), 2000);
        } catch (e) {
            setError(e.message);
        } finally {
            setSaving(false);
        }
    };

    if (!profile) return null;

    return html`
        <div style=${styles.card}>
            <div style=${{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: '14px' }}>
                <span style=${styles.cardTitle}>Profile</span>
                ${saved && html`<span style=${styles.savedBadge}>Saved</span>`}
            </div>

            <div style=${{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: '10px', marginBottom: '10px' }}>
                <div>
                    <label style=${{ fontSize: '12px', color: 'var(--f-text-tertiary)', display: 'block', marginBottom: '4px' }}>Name</label>
                    <input
                        style=${styles.input}
                        value=${profile.name || ''}
                        onInput=${e => setProfile({ ...profile, name: e.target.value })}
                    />
                </div>
                <div>
                    <label style=${{ fontSize: '12px', color: 'var(--f-text-tertiary)', display: 'block', marginBottom: '4px' }}>Email</label>
                    <input
                        style=${styles.input}
                        value=${profile.email || ''}
                        onInput=${e => setProfile({ ...profile, email: e.target.value })}
                    />
                </div>
            </div>

            <div style=${{ marginBottom: '14px' }}>
                <label style=${{ fontSize: '12px', color: 'var(--f-text-tertiary)', display: 'block', marginBottom: '4px' }}>Timezone</label>
                <select
                    style=${{ ...styles.input, appearance: 'auto' }}
                    value=${profile.timezone || 'America/Toronto'}
                    onChange=${e => setProfile({ ...profile, timezone: e.target.value })}
                >
                    ${TIMEZONES.map(tz => html`<option value=${tz}>${tz}</option>`)}
                </select>
            </div>

            <button style=${styles.btnPrimary} onClick=${handleSave} disabled=${saving}>
                ${saving ? 'Saving…' : 'Save profile'}
            </button>
            ${error && html`<div style=${{ ...styles.errorText, marginTop: '8px' }}>${error}</div>`}
        </div>
    `;
}


// ── Password card ────────────────────────────────────────────────────────

function PasswordCard() {
    const [current, setCurrent] = useState('');
    const [newPw, setNewPw] = useState('');
    const [confirm, setConfirm] = useState('');
    const [saving, setSaving] = useState(false);
    const [result, setResult] = useState(null);

    const handleSubmit = async () => {
        setSaving(true);
        setResult(null);
        try {
            await api.changePassword(current, newPw, confirm);
            setResult({ ok: true });
            setCurrent('');
            setNewPw('');
            setConfirm('');
        } catch (e) {
            setResult({ ok: false, error: e.message });
        } finally {
            setSaving(false);
        }
    };

    return html`
        <div style=${styles.card}>
            <span style=${styles.cardTitle}>Change Password</span>
            <div style=${{ maxWidth: '280px', marginTop: '14px', display: 'flex', flexDirection: 'column', gap: '8px' }}>
                <input
                    type="password" placeholder="Current password"
                    style=${styles.input} value=${current}
                    onInput=${e => setCurrent(e.target.value)}
                />
                <input
                    type="password" placeholder="New password"
                    style=${styles.input} value=${newPw}
                    onInput=${e => setNewPw(e.target.value)}
                />
                <input
                    type="password" placeholder="Confirm new password"
                    style=${styles.input} value=${confirm}
                    onInput=${e => setConfirm(e.target.value)}
                />
                <div>
                    <button
                        style=${styles.btnPrimary}
                        onClick=${handleSubmit}
                        disabled=${saving || !current || !newPw || !confirm}
                    >
                        ${saving ? 'Updating…' : 'Update password'}
                    </button>
                </div>
                ${result && html`
                    <div style=${result.ok ? styles.successText : styles.errorText}>
                        ${result.ok ? 'Password updated' : result.error}
                    </div>
                `}
            </div>
        </div>
    `;
}


// ── Push notification toggle ─────────────────────────────────────────────

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
    const sub = await reg.pushManager.subscribe({ userVisibleOnly: true, applicationServerKey });
    const json = sub.toJSON();
    await api.pushSubscribe({ endpoint: json.endpoint, p256dh: json.keys.p256dh, auth: json.keys.auth });
    return sub;
}

async function unsubscribePush(subscription) {
    await api.pushUnsubscribe({ endpoint: subscription.endpoint });
    await subscription.unsubscribe();
}


// ── Notification type row ────────────────────────────────────────────────

function NotifRow({ label, description, checked, disabled, onChange }) {
    return html`
        <label style=${{
            display: 'flex', alignItems: 'flex-start', gap: '10px', cursor: disabled ? 'not-allowed' : 'pointer',
            opacity: disabled ? 0.5 : 1, padding: '4px 0',
        }}>
            <input
                type="checkbox"
                checked=${checked}
                disabled=${disabled}
                onChange=${e => onChange(e.target.checked)}
                style=${{ marginTop: '2px', accentColor: 'var(--f-accent)', flexShrink: 0 }}
            />
            <div>
                <div style=${{ fontSize: '13px', color: 'var(--f-text)' }}>${label}</div>
                <div style=${{ fontSize: '12px', color: 'var(--f-text-tertiary)', marginTop: '1px' }}>${description}</div>
            </div>
        </label>
    `;
}


// ── Main Settings component ──────────────────────────────────────────────

export function Settings() {
    // Push state
    const [push, setPush] = useState({ supported: false, subscribed: false, subscription: null });
    const [pushLoading, setPushLoading] = useState(false);
    const [pushError, setPushError] = useState(null);

    // Notification types
    const [settings, setSettings] = useState(null);
    const [settingsSaving, setSettingsSaving] = useState(false);
    const [settingsError, setSettingsError] = useState(null);

    // Profile, credentials, oauth
    const [profile, setProfile] = useState(null);
    const [credentials, setCredentials] = useState(null);
    const [oauth, setOAuth] = useState(null);

    // Load all data on mount
    useEffect(() => {
        getPushState().then(setPush);
        api.getNotificationSettings().then(setSettings).catch(e => setSettingsError(e.message));
        api.getProfile().then(setProfile).catch(() => {});
        api.getCredentials().then(setCredentials).catch(() => {});
        api.getOAuth().then(setOAuth).catch(() => {});
    }, []);

    const handlePushToggle = useCallback(async () => {
        setPushError(null);
        setPushLoading(true);
        try {
            if (push.subscribed) {
                await unsubscribePush(push.subscription);
            } else {
                const perm = await Notification.requestPermission();
                if (perm !== 'granted') throw new Error('Notification permission denied');
                await subscribePush();
            }
            setPush(await getPushState());
        } catch (e) {
            setPushError(e.message);
        } finally {
            setPushLoading(false);
        }
    }, [push]);

    const handleSettingToggle = useCallback(async (key, value) => {
        setSettingsError(null);
        const prev = settings;
        setSettings({ ...settings, [key]: value });
        setSettingsSaving(true);
        try {
            const saved = await api.updateNotificationSettings({ [key]: value });
            setSettings(saved);
        } catch (e) {
            setSettingsError(e.message);
            setSettings(prev);
        } finally {
            setSettingsSaving(false);
        }
    }, [settings]);

    const handleUpdateCredential = async (field, value) => {
        const updated = await api.updateCredential(field, value);
        setCredentials(updated);
    };

    const handleTestCredential = async (field) => {
        return await api.testCredential(field);
    };

    const handleSaveProfile = async (data) => {
        const updated = await api.updateProfile(data);
        return updated;
    };

    const handleRegenerateSecret = async () => {
        const result = await api.regenerateOAuthSecret();
        setOAuth({ client_id: result.client_id, client_secret_masked: null });
        return result;
    };

    const notifDisabled = !push.subscribed;

    return html`
        <div style=${{ padding: '24px', maxWidth: '680px' }}>

            <!-- ── INSTANCE ──────────────────────────────────── -->
            <div style=${styles.sectionLabel}>Instance</div>

            <${OAuthCard} oauth=${oauth} onRegenerate=${handleRegenerateSecret} />

            <div style=${{ marginBottom: '28px' }} />

            <!-- ── ACCOUNT ───────────────────────────────────── -->
            <div style=${styles.sectionLabel}>Account</div>

            <${CredentialCard}
                name="GitHub" icon="🐙" field="github_pat"
                masked=${credentials?.github_pat}
                onUpdate=${handleUpdateCredential}
                onTest=${handleTestCredential}
            />
            <${CredentialCard}
                name="Anthropic" icon="🔑" field="anthropic_api_key"
                masked=${credentials?.anthropic_api_key}
                onUpdate=${handleUpdateCredential}
                onTest=${handleTestCredential}
            />

            <${ProfileCard} profile=${profile} onSave=${handleSaveProfile} />
            <${PasswordCard} />

            <div style=${{ marginBottom: '28px' }} />

            <!-- ── NOTIFICATIONS ──────────────────────────────── -->
            <div style=${styles.sectionLabel}>Notifications</div>

            <div style=${styles.card}>
                <!-- Push toggle -->
                <div style=${{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: '4px' }}>
                    <div>
                        <div style=${{ fontSize: '14px', fontWeight: 500, color: 'var(--f-text)' }}>Push Notifications</div>
                        <div style=${{ fontSize: '12px', color: 'var(--f-text-tertiary)', marginTop: '1px' }}>
                            ${!push.supported
                                ? 'Not supported in this browser'
                                : !push.serverConfigured
                                    ? 'VAPID keys not configured on server'
                                    : push.subscribed ? 'Active on this device' : 'Not subscribed'
                            }
                        </div>
                    </div>
                    ${push.supported && push.serverConfigured && html`
                        <${Toggle}
                            checked=${push.subscribed}
                            onChange=${handlePushToggle}
                            disabled=${pushLoading}
                        />
                    `}
                </div>

                ${pushError && html`<div style=${{ ...styles.errorText, marginTop: '6px' }}>${pushError}</div>`}

                ${push.subscribed && html`
                    <div style=${{ borderTop: '0.5px solid var(--f-border)', marginTop: '12px', paddingTop: '12px' }}>
                        ${settingsError && html`<div style=${{ ...styles.errorText, marginBottom: '8px' }}>${settingsError}</div>`}

                        ${!settings && !settingsError && html`
                            <div style=${{ fontSize: '13px', color: 'var(--f-text-tertiary)' }}>Loading…</div>
                        `}

                        ${settings && html`
                            <div style=${{ display: 'flex', flexDirection: 'column', gap: '4px' }}>
                                <${NotifRow}
                                    label="Task failed"
                                    description="Task ends with an error or exception."
                                    checked=${!!settings.notify_failed}
                                    disabled=${settingsSaving}
                                    onChange=${v => handleSettingToggle('notify_failed', v)}
                                />
                                <${NotifRow}
                                    label="Needs review"
                                    description="Task timed out, lost its process, or ended without a result."
                                    checked=${!!settings.notify_needs_review}
                                    disabled=${settingsSaving}
                                    onChange=${v => handleSettingToggle('notify_needs_review', v)}
                                />
                                <${NotifRow}
                                    label="CC posted a question"
                                    description="Task is paused waiting for your input."
                                    checked=${!!settings.notify_question}
                                    disabled=${settingsSaving}
                                    onChange=${v => handleSettingToggle('notify_question', v)}
                                />
                                <${NotifRow}
                                    label="Task completed"
                                    description="Task finishes successfully. Off by default to reduce noise."
                                    checked=${!!settings.notify_completed}
                                    disabled=${settingsSaving}
                                    onChange=${v => handleSettingToggle('notify_completed', v)}
                                />
                            </div>
                        `}
                    </div>
                `}
            </div>
        </div>
    `;
}
