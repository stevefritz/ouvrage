// LoginView — Switchboard login form
// Dark-theme Preact/htm component. Reads ?next= param, redirects after login.

import { h } from 'https://esm.sh/preact@10.25.4';
import { useState, useEffect } from 'https://esm.sh/preact@10.25.4/hooks';
import htm from 'https://esm.sh/htm@3.1.1';
import { colors, typography } from '../tokens.js';

const html = htm.bind(h);

export function LoginView() {
    const [email, setEmail] = useState('');
    const [password, setPassword] = useState('');
    const [error, setError] = useState('');
    const [locked, setLocked] = useState('');
    const [loading, setLoading] = useState(false);

    // Parse ?next= from URL
    const nextUrl = (() => {
        try {
            const params = new URLSearchParams(window.location.search);
            return params.get('next') || '/foreman/';
        } catch {
            return '/foreman/';
        }
    })();

    async function handleSubmit(e) {
        e.preventDefault();
        setError('');
        setLocked('');
        setLoading(true);

        try {
            const res = await fetch('/auth/login', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ email, password, next: nextUrl }),
            });

            const data = await res.json().catch(() => ({}));

            if (res.status === 429) {
                setLocked(data.message || 'Account temporarily locked. Please try again later.');
            } else if (!res.ok) {
                setError('Invalid email or password.');
            } else {
                // Success — redirect
                window.location.href = data.redirect || nextUrl;
            }
        } catch (err) {
            setError('Connection error. Please try again.');
        } finally {
            setLoading(false);
        }
    }

    const inputStyle = {
        width: '100%',
        padding: '10px 12px',
        background: colors.surfaceActive,
        border: `1px solid ${colors.border}`,
        borderRadius: '6px',
        color: colors.text,
        fontFamily: typography.fontBody,
        fontSize: typography.size.base,
        outline: 'none',
        boxSizing: 'border-box',
        transition: 'border-color 150ms ease',
    };

    const labelStyle = {
        display: 'block',
        fontSize: typography.size.sm,
        color: colors.textSecondary,
        marginBottom: '6px',
        fontWeight: typography.weight.medium,
    };

    return html`
        <div style=${{
            minHeight: '100vh',
            display: 'flex',
            alignItems: 'center',
            justifyContent: 'center',
            background: colors.bg,
            fontFamily: typography.fontBody,
            padding: '20px',
        }}>
            <div style=${{
                width: '100%',
                maxWidth: '380px',
            }}>
                <!-- Logo / wordmark -->
                <div style=${{
                    textAlign: 'center',
                    marginBottom: '32px',
                }}>
                    <div style=${{
                        fontSize: '22px',
                        fontWeight: typography.weight.semibold,
                        color: colors.text,
                        letterSpacing: '-0.3px',
                    }}>Switchboard</div>
                    <div style=${{
                        fontSize: typography.size.sm,
                        color: colors.textTertiary,
                        marginTop: '4px',
                    }}>Sign in to your account</div>
                </div>

                <!-- Card -->
                <form
                    onSubmit=${handleSubmit}
                    style=${{
                        background: colors.surface,
                        border: `1px solid ${colors.border}`,
                        borderRadius: '10px',
                        padding: '28px',
                    }}
                >
                    ${locked && html`
                        <div style=${{
                            background: 'rgba(245, 166, 35, 0.1)',
                            border: '1px solid rgba(245, 166, 35, 0.3)',
                            borderRadius: '6px',
                            padding: '10px 12px',
                            marginBottom: '20px',
                            fontSize: typography.size.sm,
                            color: '#f5a623',
                        }}>${locked}</div>
                    `}

                    ${error && html`
                        <div style=${{
                            background: colors.redBg,
                            border: `1px solid rgba(242, 92, 92, 0.3)`,
                            borderRadius: '6px',
                            padding: '10px 12px',
                            marginBottom: '20px',
                            fontSize: typography.size.sm,
                            color: colors.red,
                        }}>${error}</div>
                    `}

                    <div style=${{ marginBottom: '16px' }}>
                        <label style=${labelStyle} for="sb-email">Email</label>
                        <input
                            id="sb-email"
                            type="email"
                            required
                            autocomplete="email"
                            value=${email}
                            onInput=${e => setEmail(e.target.value)}
                            style=${inputStyle}
                            placeholder="you@example.com"
                            disabled=${loading}
                        />
                    </div>

                    <div style=${{ marginBottom: '24px' }}>
                        <label style=${labelStyle} for="sb-password">Password</label>
                        <input
                            id="sb-password"
                            type="password"
                            required
                            autocomplete="current-password"
                            value=${password}
                            onInput=${e => setPassword(e.target.value)}
                            style=${inputStyle}
                            placeholder="••••••••"
                            disabled=${loading}
                        />
                    </div>

                    <button
                        type="submit"
                        disabled=${loading}
                        style=${{
                            width: '100%',
                            padding: '10px 16px',
                            background: loading ? colors.surfaceActive : colors.accent,
                            border: 'none',
                            borderRadius: '6px',
                            color: loading ? colors.textTertiary : '#fff',
                            fontFamily: typography.fontBody,
                            fontSize: typography.size.base,
                            fontWeight: typography.weight.medium,
                            cursor: loading ? 'not-allowed' : 'pointer',
                            transition: 'background 150ms ease, opacity 150ms ease',
                        }}
                    >
                        ${loading ? 'Signing in…' : 'Sign in'}
                    </button>
                </form>
            </div>
        </div>
    `;
}
