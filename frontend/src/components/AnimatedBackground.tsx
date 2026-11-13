import { motion } from "framer-motion";

/**
 * Full-screen living backdrop: deep black, a drifting aurora, a slowly rotating
 * conic ring, and a masked grid. Pure decoration, sits behind everything.
 */
export default function AnimatedBackground() {
  return (
    <div className="pointer-events-none fixed inset-0 -z-10 overflow-hidden bg-ink-950">
      {/* Aurora blobs */}
      <motion.div
        className="absolute -left-40 -top-40 h-[42rem] w-[42rem] rounded-full blur-[120px]"
        style={{ background: "radial-gradient(circle, rgba(124,92,255,0.35), transparent 60%)" }}
        animate={{ x: [0, 60, -20, 0], y: [0, 40, 80, 0], scale: [1, 1.1, 0.95, 1] }}
        transition={{ duration: 22, repeat: Infinity, ease: "easeInOut" }}
      />
      <motion.div
        className="absolute -right-52 top-1/3 h-[38rem] w-[38rem] rounded-full blur-[130px]"
        style={{ background: "radial-gradient(circle, rgba(61,220,151,0.18), transparent 60%)" }}
        animate={{ x: [0, -50, 20, 0], y: [0, -30, 30, 0], scale: [1, 1.15, 1, 1] }}
        transition={{ duration: 26, repeat: Infinity, ease: "easeInOut" }}
      />

      {/* Rotating conic ring */}
      <div
        className="absolute left-1/2 top-1/2 h-[60rem] w-[60rem] -translate-x-1/2 -translate-y-1/2 rounded-full opacity-[0.07] animate-spin-slow"
        style={{
          background:
            "conic-gradient(from 0deg, transparent, #7c5cff, transparent 30%, #3ddc97, transparent 70%)",
          maskImage: "radial-gradient(circle, transparent 54%, #000 55%, #000 57%, transparent 58%)",
          WebkitMaskImage:
            "radial-gradient(circle, transparent 54%, #000 55%, #000 57%, transparent 58%)",
        }}
      />

      {/* Grid + vignette */}
      <div className="grid-bg absolute inset-0" />
      <div
        className="absolute inset-0"
        style={{ background: "radial-gradient(ellipse at 50% 30%, transparent 40%, rgba(0,0,0,0.7) 100%)" }}
      />
    </div>
  );
}
