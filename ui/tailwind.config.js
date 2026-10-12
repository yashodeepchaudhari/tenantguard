/** @type {import('tailwindcss').Config} */
export default {
  content: ['./index.html', './src/**/*.{ts,tsx}'],
  theme: {
    extend: {
      colors: {
        // Colour means status. One accent for interactive/selected things.
        ok: { DEFAULT: '#22c55e', dim: '#14532d' },
        warn: { DEFAULT: '#f59e0b', dim: '#713f12' },
        bad: { DEFAULT: '#ef4444', dim: '#7f1d1d' },
        accent: { DEFAULT: '#38bdf8', dim: '#0c4a6e' },
        ink: { 950: '#0b0f17', 900: '#111827', 800: '#1a2233', 700: '#273248', 500: '#64748b', 300: '#cbd5e1', 100: '#f1f5f9' },
      },
      fontFamily: {
        sans: ['"Segoe UI"', 'Inter', 'system-ui', 'sans-serif'],
        mono: ['"Cascadia Mono"', 'Consolas', 'ui-monospace', 'monospace'],
      },
    },
  },
  plugins: [],
}
