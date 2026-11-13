/** @type {import('tailwindcss').Config} */
export default {
  content: ["./index.html", "./src/**/*.{ts,tsx}"],
  theme: {
    extend: {
      colors: {
        ink: {
          950: "#050506",
          900: "#0a0a0c",
          800: "#111114",
          700: "#1a1a1f",
          600: "#26262d",
        },
        accent: {
          DEFAULT: "#7c5cff",
          soft: "#9d86ff",
          glow: "#5b3df0",
        },
        signal: {
          ok: "#3ddc97",
          warn: "#ffb020",
          bad: "#ff5470",
        },
      },
      fontFamily: {
        sans: ["Inter", "system-ui", "sans-serif"],
        mono: ["'JetBrains Mono'", "ui-monospace", "monospace"],
      },
      keyframes: {
        "spin-slow": {
          to: { transform: "rotate(360deg)" },
        },
        float: {
          "0%, 100%": { transform: "translateY(0)" },
          "50%": { transform: "translateY(-8px)" },
        },
        "pulse-ring": {
          "0%": { transform: "scale(0.8)", opacity: "0.7" },
          "100%": { transform: "scale(2.2)", opacity: "0" },
        },
        shimmer: {
          "100%": { transform: "translateX(100%)" },
        },
      },
      animation: {
        "spin-slow": "spin-slow 18s linear infinite",
        "spin-rev": "spin-slow 26s linear infinite reverse",
        float: "float 6s ease-in-out infinite",
        "pulse-ring": "pulse-ring 2.4s cubic-bezier(0.4,0,0.2,1) infinite",
      },
    },
  },
  plugins: [],
};
