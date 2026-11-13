interface LogoProps {
  size?: number;
}

/** Oreshnik mark: a glowing core wrapped by two counter-rotating rings. */
export default function Logo({ size = 72 }: LogoProps) {
  return (
    <div className="relative" style={{ width: size, height: size }}>
      <div className="absolute inset-0 rounded-full border border-accent/40 animate-spin-slow" />
      <div className="absolute inset-[6px] rounded-full border border-dashed border-signal-ok/30 animate-spin-rev" />
      <div
        className="absolute inset-[18px] rounded-full animate-float"
        style={{
          background: "radial-gradient(circle at 35% 30%, #9d86ff, #5b3df0 70%)",
          boxShadow: "0 0 30px 4px rgba(124,92,255,0.55)",
        }}
      />
      {/* orbiting dot */}
      <div className="absolute inset-0 animate-spin-slow">
        <span className="absolute left-1/2 top-0 h-1.5 w-1.5 -translate-x-1/2 rounded-full bg-signal-ok shadow-[0_0_10px_2px_rgba(61,220,151,0.8)]" />
      </div>
    </div>
  );
}
