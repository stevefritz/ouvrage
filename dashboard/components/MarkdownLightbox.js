// MarkdownLightbox — fullscreen overlay for rendering .md files
// Usage: html`<${MarkdownLightbox} src=${downloadUrl} filename=${name} onClose=${fn} />`
import { html } from './utils.js';
import { useState, useEffect, useCallback } from 'https://esm.sh/preact@10.25.4/hooks';
import { colors, typography, layout, animation, spacing } from '../tokens.js';

// ── Highlight.js setup ────────────────────────────────────────────────────────
// Import core + only the languages we need (avoids loading 190+ language bundles)
import hljs from 'https://esm.sh/highlight.js@11.9.0/lib/core';
import javascript from 'https://esm.sh/highlight.js@11.9.0/lib/languages/javascript';
import typescript from 'https://esm.sh/highlight.js@11.9.0/lib/languages/typescript';
import python from 'https://esm.sh/highlight.js@11.9.0/lib/languages/python';
import bash from 'https://esm.sh/highlight.js@11.9.0/lib/languages/bash';
import json from 'https://esm.sh/highlight.js@11.9.0/lib/languages/json';
import html_lang from 'https://esm.sh/highlight.js@11.9.0/lib/languages/xml';
import css from 'https://esm.sh/highlight.js@11.9.0/lib/languages/css';
import sql from 'https://esm.sh/highlight.js@11.9.0/lib/languages/sql';
import yaml from 'https://esm.sh/highlight.js@11.9.0/lib/languages/yaml';
import markdown from 'https://esm.sh/highlight.js@11.9.0/lib/languages/markdown';
import php from 'https://esm.sh/highlight.js@11.9.0/lib/languages/php';
import ruby from 'https://esm.sh/highlight.js@11.9.0/lib/languages/ruby';
import go from 'https://esm.sh/highlight.js@11.9.0/lib/languages/go';
import rust from 'https://esm.sh/highlight.js@11.9.0/lib/languages/rust';
import diff from 'https://esm.sh/highlight.js@11.9.0/lib/languages/diff';

hljs.registerLanguage('javascript', javascript);
hljs.registerLanguage('typescript', typescript);
hljs.registerLanguage('python', python);
hljs.registerLanguage('bash', bash);
hljs.registerLanguage('json', json);
hljs.registerLanguage('html', html_lang);
hljs.registerLanguage('xml', html_lang);
hljs.registerLanguage('css', css);
hljs.registerLanguage('sql', sql);
hljs.registerLanguage('yaml', yaml);
hljs.registerLanguage('markdown', markdown);
hljs.registerLanguage('php', php);
hljs.registerLanguage('ruby', ruby);
hljs.registerLanguage('go', go);
hljs.registerLanguage('rust', rust);
hljs.registerLanguage('diff', diff);

// Configure marked to use highlight.js for code blocks.
// marked and DOMPurify are loaded globally via CDN in the HTML.
if (typeof marked !== 'undefined') {
    marked.setOptions({
        highlight: (code, lang) => {
            if (lang && hljs.getLanguage(lang)) {
                return hljs.highlight(code, { language: lang }).value;
            }
            return hljs.highlightAuto(code).value;
        }
    });
}

// ── isMarkdownFile helper ─────────────────────────────────────────────────────

export function isMarkdownFile(filename) {
    if (!filename) return false;
    const ext = filename.split('.').pop().toLowerCase();
    return ['md', 'markdown'].includes(ext);
}

// ── Inline CSS for markdown + syntax highlighting ────────────────────────────
// Scoped to .md-lightbox-content to avoid leaking into the rest of the dashboard.
// Syntax colors use Copper Forge palette on dark background (#0d0b09).
const MARKDOWN_STYLES = `
.md-lightbox-content {
    font-family: 'DM Sans', -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
    font-size: 14px;
    line-height: 1.7;
    color: #e8e9ea;
}
.md-lightbox-content h1,
.md-lightbox-content h2,
.md-lightbox-content h3,
.md-lightbox-content h4,
.md-lightbox-content h5,
.md-lightbox-content h6 {
    margin: 1.5em 0 0.5em;
    font-weight: 600;
    line-height: 1.3;
    color: #e8e9ea;
}
.md-lightbox-content h1 { font-size: 22px; border-bottom: 1px solid #2a2c32; padding-bottom: 0.3em; }
.md-lightbox-content h2 { font-size: 18px; border-bottom: 1px solid #2a2c32; padding-bottom: 0.2em; }
.md-lightbox-content h3 { font-size: 16px; }
.md-lightbox-content h4, .md-lightbox-content h5, .md-lightbox-content h6 { font-size: 14px; }
.md-lightbox-content p { margin: 0.8em 0; }
.md-lightbox-content a { color: #7c5af6; text-decoration: none; }
.md-lightbox-content a:hover { text-decoration: underline; }
.md-lightbox-content ul, .md-lightbox-content ol {
    margin: 0.8em 0;
    padding-left: 1.8em;
}
.md-lightbox-content li { margin: 0.3em 0; }
.md-lightbox-content blockquote {
    margin: 1em 0;
    padding: 0.5em 1em;
    border-left: 3px solid #7c5af6;
    background: rgba(124, 90, 246, 0.08);
    color: #b0b1ba;
    border-radius: 0 4px 4px 0;
}
.md-lightbox-content code {
    font-family: 'JetBrains Mono', 'Fira Code', 'Cascadia Code', monospace;
    font-size: 12px;
    background: rgba(13, 11, 9, 0.8);
    padding: 1px 5px;
    border-radius: 3px;
    color: #f5a623;
}
.md-lightbox-content pre {
    margin: 1em 0;
    padding: 1rem 1.2rem;
    background: #0d0b09;
    border-radius: 6px;
    overflow-x: auto;
    border: 1px solid #2a2c32;
}
.md-lightbox-content pre code {
    background: transparent;
    padding: 0;
    color: #e8e9ea;
    font-size: 13px;
    line-height: 1.6;
}
.md-lightbox-content table {
    width: 100%;
    border-collapse: collapse;
    margin: 1em 0;
    font-size: 13px;
}
.md-lightbox-content th, .md-lightbox-content td {
    padding: 8px 12px;
    border: 1px solid #2a2c32;
    text-align: left;
}
.md-lightbox-content th {
    background: #18191d;
    font-weight: 600;
    color: #b0b1ba;
}
.md-lightbox-content tr:nth-child(even) { background: rgba(42, 44, 50, 0.3); }
.md-lightbox-content img { max-width: 100%; border-radius: 6px; }
.md-lightbox-content hr {
    border: none;
    border-top: 1px solid #2a2c32;
    margin: 1.5em 0;
}

/* Copper Forge syntax highlight colors — dark background #0d0b09 */
.md-lightbox-content .hljs-keyword,
.md-lightbox-content .hljs-selector-tag,
.md-lightbox-content .hljs-built_in,
.md-lightbox-content .hljs-name,
.md-lightbox-content .hljs-tag { color: #7c5af6; }

.md-lightbox-content .hljs-string,
.md-lightbox-content .hljs-attr,
.md-lightbox-content .hljs-symbol,
.md-lightbox-content .hljs-bullet,
.md-lightbox-content .hljs-addition { color: #3dd68c; }

.md-lightbox-content .hljs-comment,
.md-lightbox-content .hljs-quote,
.md-lightbox-content .hljs-deletion { color: #8a93a2; font-style: italic; }

.md-lightbox-content .hljs-number,
.md-lightbox-content .hljs-regexp,
.md-lightbox-content .hljs-literal,
.md-lightbox-content .hljs-type,
.md-lightbox-content .hljs-link { color: #4da3ff; }

.md-lightbox-content .hljs-function,
.md-lightbox-content .hljs-title,
.md-lightbox-content .hljs-selector-id,
.md-lightbox-content .hljs-selector-class { color: #f5a623; }

.md-lightbox-content .hljs-variable,
.md-lightbox-content .hljs-template-variable,
.md-lightbox-content .hljs-params { color: #e8e9ea; }

.md-lightbox-content .hljs-meta { color: #b0b1ba; }
`;

// Inject styles once into document head (idempotent)
let _stylesInjected = false;
function ensureStyles() {
    if (_stylesInjected) return;
    const style = document.createElement('style');
    style.textContent = MARKDOWN_STYLES;
    document.head.appendChild(style);
    _stylesInjected = true;
}

// ── MarkdownLightbox component ────────────────────────────────────────────────
// Props:
//   src      — URL to fetch markdown from (used by Files page)
//   content  — raw markdown string to render directly (used by TaskView spec messages)
//   filename — display name shown in header (for src path)
//   title    — display name shown in header (for content path)
//   onClose  — close handler

export function MarkdownLightbox({ src, content: rawContent, filename, title, onClose }) {
    const [rendered, setRendered] = useState(null);   // null = loading, string = ready
    const [error, setError] = useState(null);

    // Inject scoped CSS once
    useEffect(() => { ensureStyles(); }, []);

    // If raw content is provided directly, render it without fetching
    useEffect(() => {
        if (!rawContent) return;
        setRendered(null);
        setError(null);
        try {
            if (typeof marked === 'undefined') {
                throw new Error('marked library not available');
            }
            const raw = marked.parse(rawContent);
            const clean = typeof DOMPurify !== 'undefined'
                ? DOMPurify.sanitize(raw)
                : raw;
            setRendered(clean);
        } catch (e) {
            setError(e.message);
        }
    }, [rawContent]);

    // Fetch markdown content from URL
    useEffect(() => {
        if (!src) return;
        setRendered(null);
        setError(null);
        fetch(src)
            .then(r => {
                if (!r.ok) throw new Error(`Failed to load file (${r.status})`);
                return r.text();
            })
            .then(text => {
                // Re-check marked availability at render time (module-level config runs on import)
                if (typeof marked === 'undefined') {
                    throw new Error('marked library not available');
                }
                const raw = marked.parse(text);
                const clean = typeof DOMPurify !== 'undefined'
                    ? DOMPurify.sanitize(raw)
                    : raw;
                setRendered(clean);
            })
            .catch(e => setError(e.message));
    }, [src]);

    // Close on Escape
    const handleKeyDown = useCallback((e) => {
        if (e.key === 'Escape') onClose();
    }, [onClose]);

    useEffect(() => {
        document.addEventListener('keydown', handleKeyDown);
        return () => document.removeEventListener('keydown', handleKeyDown);
    }, [handleKeyDown]);

    const backdropStyle = {
        position: 'fixed',
        inset: 0,
        zIndex: 9999,
        background: 'rgba(0,0,0,0.85)',
        display: 'flex',
        alignItems: 'flex-start',
        justifyContent: 'center',
        padding: `${spacing[8]} ${spacing[4]}`,
        overflowY: 'auto',
    };

    const closeBtnStyle = {
        position: 'fixed',
        top: '16px',
        right: '16px',
        width: '36px',
        height: '36px',
        borderRadius: layout.borderRadius.pill,
        background: 'rgba(255,255,255,0.15)',
        border: 'none',
        color: '#fff',
        fontSize: '18px',
        lineHeight: '36px',
        textAlign: 'center',
        cursor: 'pointer',
        display: 'flex',
        alignItems: 'center',
        justifyContent: 'center',
        fontFamily: 'inherit',
        transition: `background ${animation.durationFast}`,
        zIndex: 10000,
        flexShrink: 0,
    };

    const containerStyle = {
        width: '100%',
        maxWidth: '800px',
        background: colors.surface,
        borderRadius: layout.borderRadius.lg,
        padding: '2rem 2.5rem',
        boxSizing: 'border-box',
        margin: '0 auto',
    };

    const filenameStyle = {
        fontFamily: typography.fontMono,
        fontSize: typography.size.xs,
        color: colors.textTertiary,
        marginBottom: spacing[4],
        paddingBottom: spacing[3],
        borderBottom: `1px solid ${colors.border}`,
    };

    return html`
        <div
            style=${backdropStyle}
            onClick=${onClose}
        >
            <button
                style=${closeBtnStyle}
                onClick=${onClose}
                title="Close (Esc)"
            >✕</button>

            <div
                style=${containerStyle}
                onClick=${(e) => e.stopPropagation()}
            >
                ${(filename || title) && html`
                    <div style=${filenameStyle}>${filename || title}</div>
                `}

                ${error
                    ? html`<div style=${{
                        padding: spacing[4],
                        color: colors.red,
                        background: colors.redBg,
                        borderRadius: layout.borderRadius.md,
                        fontSize: typography.size.sm,
                    }}>Failed to load: ${error}</div>`
                    : rendered === null
                        ? html`<div style=${{
                            padding: spacing[8],
                            textAlign: 'center',
                            color: colors.textTertiary,
                            fontSize: typography.size.sm,
                        }}>Loading...</div>`
                        : html`<div
                            class="md-lightbox-content"
                            dangerouslySetInnerHTML=${{ __html: rendered }}
                        />`
                }
            </div>
        </div>
    `;
}
