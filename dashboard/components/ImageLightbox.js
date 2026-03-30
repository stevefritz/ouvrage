// ImageLightbox — reusable fullscreen image overlay
// Usage: html`<${ImageLightbox} src=${url} alt=${name} onClose=${fn} />`
import { html } from './utils.js';
import { useEffect, useCallback } from 'https://esm.sh/preact@10.25.4/hooks';
import { colors, layout, animation } from '../tokens.js';

// Returns true for image file extensions we can preview inline.
export function isImageFile(filename) {
    if (!filename) return false;
    const ext = filename.split('.').pop().toLowerCase();
    return ['png', 'jpg', 'jpeg', 'gif', 'webp', 'svg'].includes(ext);
}

export function ImageLightbox({ src, alt, onClose }) {
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
        alignItems: 'center',
        justifyContent: 'center',
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

    const imgStyle = {
        maxWidth: '90vw',
        maxHeight: '90vh',
        objectFit: 'contain',
        display: 'block',
        borderRadius: layout.borderRadius.md,
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
            <img
                src=${src}
                alt=${alt || ''}
                style=${imgStyle}
                onClick=${(e) => e.stopPropagation()}
            />
        </div>
    `;
}
