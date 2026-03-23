// Foreman Design System — Design Tokens
// Linear-inspired dark theme. Import these constants into any view.

export const colors = {
    // Backgrounds
    bg:           '#101114',
    surface:      '#18191d',
    surfaceHover: '#1e2025',
    surfaceActive:'#22242a',

    // Borders
    border:       '#2a2c32',
    borderSubtle: 'rgba(42, 44, 50, 0.6)',

    // Text
    text:          '#e8e9ea',
    textSecondary: '#9899a1',
    textTertiary:  '#5c5e66',

    // Accent
    accent:  '#7c5af6',  // purple

    // Status colors
    green:  '#3dd68c',
    yellow: '#f5a623',
    red:    '#f25c5c',
    blue:   '#4da3ff',

    // Status backgrounds (low opacity)
    greenBg:  'rgba(61, 214, 140, 0.12)',
    yellowBg: 'rgba(245, 166, 35, 0.12)',
    redBg:    'rgba(242, 92, 92, 0.12)',
    blueBg:   'rgba(77, 163, 255, 0.12)',
    accentBg: 'rgba(124, 90, 246, 0.15)',
};

export const typography = {
    fontBody: "'DM Sans', -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif",
    fontMono: "'JetBrains Mono', 'Fira Code', 'Cascadia Code', monospace",

    // Size scale (px values as strings for use in CSS)
    size: {
        xs:   '11px',
        sm:   '12px',
        base: '13px',
        md:   '14px',
        lg:   '16px',
        xl:   '18px',
        '2xl':'22px',
        '3xl':'28px',
    },

    weight: {
        light:   300,
        normal:  400,
        medium:  500,
        semibold:600,
        bold:    700,
    },

    lineHeight: {
        tight:  1.3,
        normal: 1.5,
        relaxed:1.7,
    },
};

export const spacing = {
    1:  '4px',
    2:  '8px',
    3:  '12px',
    4:  '16px',
    5:  '20px',
    6:  '24px',
    8:  '32px',
    10: '40px',
    12: '48px',
    16: '64px',
};

export const layout = {
    headerHeight: '52px',
    contentMaxWidth: '900px',
    contentPadding: '24px',
    sidebarWidth: '220px',
    borderRadius: {
        sm: '4px',
        md: '6px',
        lg: '10px',
        pill: '999px',
    },
};

export const animation = {
    durationFast:   '120ms',
    durationNormal: '200ms',
    durationSlow:   '350ms',
    easing: 'cubic-bezier(0.16, 1, 0.3, 1)',
};

// Status → color mapping for convenience
export const statusColors = {
    working:         colors.yellow,
    completed:       colors.blue,
    failed:          colors.red,
    'needs-review':  colors.yellow,
    cancelled:       colors.textTertiary,
    ready:           colors.textSecondary,
    'rate-limited':  colors.yellow,
    'turns-exhausted': colors.yellow,
};

export const statusBgs = {
    working:         colors.yellowBg,
    completed:       colors.blueBg,
    failed:          colors.redBg,
    'needs-review':  colors.yellowBg,
    cancelled:       'rgba(92, 94, 102, 0.12)',
    ready:           'rgba(152, 153, 161, 0.10)',
    'rate-limited':  colors.yellowBg,
    'turns-exhausted': colors.yellowBg,
};
