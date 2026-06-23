import { useEffect, useRef, useState } from "react";
import { api } from "../api";
import type { SlamMap as SlamData } from "../types";

/**
 * SlamMap — top-down minimap of the VSLAM track. Polls /api/slam/map and draws the keyframe trail + the
 * robot's current pose (arrow = heading), auto-scaled to fit. This is the visual side of "we have VSLAM".
 */
export default function SlamMap() {
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const [data, setData] = useState<SlamData | null>(null);

  useEffect(() => {
    let alive = true;
    const tick = async () => {
      try {
        const d = await api.slamMap();
        if (alive) setData(d);
      } catch { /* */ }
    };
    tick();
    const id = window.setInterval(tick, 1000);
    return () => { alive = false; window.clearInterval(id); };
  }, []);

  useEffect(() => {
    const cv = canvasRef.current;
    if (!cv) return;
    const ctx = cv.getContext("2d");
    if (!ctx) return;
    const W = (cv.width = cv.clientWidth * devicePixelRatio);
    const H = (cv.height = cv.clientHeight * devicePixelRatio);
    ctx.clearRect(0, 0, W, H);

    // grid
    ctx.strokeStyle = "rgba(43,214,240,0.10)";
    ctx.lineWidth = 1;
    const step = 28 * devicePixelRatio;
    for (let x = 0; x < W; x += step) { ctx.beginPath(); ctx.moveTo(x, 0); ctx.lineTo(x, H); ctx.stroke(); }
    for (let y = 0; y < H; y += step) { ctx.beginPath(); ctx.moveTo(0, y); ctx.lineTo(W, y); ctx.stroke(); }

    const pts = data?.trail ?? [];
    const pose = data?.pose;
    const all: [number, number][] = [...pts];
    if (pose) all.push([pose.x, pose.y]);
    if (all.length === 0) return;

    // auto-fit bounds
    let minX = Infinity, maxX = -Infinity, minY = Infinity, maxY = -Infinity;
    for (const [x, y] of all) { minX = Math.min(minX, x); maxX = Math.max(maxX, x); minY = Math.min(minY, y); maxY = Math.max(maxY, y); }
    const pad = 1;
    minX -= pad; maxX += pad; minY -= pad; maxY += pad;
    const sx = W / Math.max(0.5, maxX - minX);
    const sy = H / Math.max(0.5, maxY - minY);
    const s = Math.min(sx, sy) * 0.85;
    const ox = W / 2 - ((minX + maxX) / 2) * s;
    const oy = H / 2 - ((minY + maxY) / 2) * s;
    const tx = (x: number) => ox + x * s;
    const ty = (y: number) => oy + y * s;

    // trail
    if (pts.length > 1) {
      ctx.strokeStyle = "rgba(43,214,240,0.7)";
      ctx.lineWidth = 2 * devicePixelRatio;
      ctx.beginPath();
      pts.forEach(([x, y], i) => (i ? ctx.lineTo(tx(x), ty(y)) : ctx.moveTo(tx(x), ty(y))));
      ctx.stroke();
    }
    // keyframe dots
    ctx.fillStyle = "rgba(245,196,81,0.8)";
    for (const [x, y] of pts) { ctx.beginPath(); ctx.arc(tx(x), ty(y), 2 * devicePixelRatio, 0, 7); ctx.fill(); }

    // current pose arrow
    if (pose) {
      const a = (-pose.yaw_deg * Math.PI) / 180;
      const px = tx(pose.x), py = ty(pose.y), r = 8 * devicePixelRatio;
      ctx.fillStyle = "var(--color-accent)";
      ctx.strokeStyle = "#2bd6f0";
      ctx.shadowColor = "#2bd6f0";
      ctx.shadowBlur = 10;
      ctx.beginPath();
      ctx.moveTo(px + Math.cos(a) * r, py + Math.sin(a) * r);
      ctx.lineTo(px + Math.cos(a + 2.5) * r, py + Math.sin(a + 2.5) * r);
      ctx.lineTo(px + Math.cos(a - 2.5) * r, py + Math.sin(a - 2.5) * r);
      ctx.closePath();
      ctx.fillStyle = "#2bd6f0";
      ctx.fill();
      ctx.shadowBlur = 0;
    }
  }, [data]);

  const enabled = data?.enabled ?? false;
  return (
    <div className="hud-panel p-3">
      <div className="flex items-center justify-between mb-2">
        <div className="text-[10px] uppercase tracking-[0.2em] text-accent text-glow">VSLAM Map</div>
        <div className="text-[10px] text-mut hud-mono">
          {enabled ? `${data?.keyframes ?? 0} kf · ${data?.frames ?? 0} f` : "offline"}
        </div>
      </div>
      <div className="hud-frame rounded-lg overflow-hidden border border-line bg-bg/60">
        <canvas ref={canvasRef} className="w-full h-[180px] block" />
      </div>
      {!enabled && <div className="text-[11px] text-mut mt-2">Mapping starts when the camera feed is live.</div>}
    </div>
  );
}
