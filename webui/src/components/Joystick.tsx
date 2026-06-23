import { useRef } from "react";

/**
 * Analog joystick: direction = 360°, distance = speed (scaled by maxSpeed). Sends a continuous /drive
 * vector at ~15 Hz while held; the bridge deadman-stops when updates cease. Ported from ebo.html.
 */
export default function Joystick({
  maxSpeed,
  onDrive,
  onStop,
  disabled,
}: {
  maxSpeed: number;
  onDrive: (ly: number, rx: number) => void;
  onStop: () => void;
  disabled?: boolean;
}) {
  const padRef = useRef<HTMLDivElement>(null);
  const knobRef = useRef<HTMLDivElement>(null);
  const timer = useRef<number | null>(null);
  const vec = useRef({ ly: 0, rx: 0 });
  const active = useRef(false);
  const R = 64;

  const setKnob = (dx: number, dy: number) => {
    if (knobRef.current) knobRef.current.style.transform = `translate(calc(-50% + ${dx}px), calc(-50% + ${dy}px))`;
  };

  const calc = (clientX: number, clientY: number) => {
    const pad = padRef.current!;
    const r = pad.getBoundingClientRect();
    let dx = clientX - (r.left + r.width / 2);
    let dy = clientY - (r.top + r.height / 2);
    const d = Math.hypot(dx, dy);
    if (d > R) {
      dx = (dx / d) * R;
      dy = (dy / d) * R;
    }
    setKnob(dx, dy);
    vec.current = {
      rx: +((dx / R) * maxSpeed).toFixed(3),
      ly: +((-dy / R) * maxSpeed).toFixed(3),
    };
  };

  const send = () => {
    if (active.current) onDrive(vec.current.ly, vec.current.rx);
  };

  const start = (e: React.PointerEvent) => {
    if (disabled) return;
    active.current = true;
    (e.target as HTMLElement).setPointerCapture?.(e.pointerId);
    calc(e.clientX, e.clientY);
    send();
    if (!timer.current) timer.current = window.setInterval(send, 66);
  };
  const move = (e: React.PointerEvent) => {
    if (active.current) calc(e.clientX, e.clientY);
  };
  const end = () => {
    if (!active.current) return;
    active.current = false;
    if (timer.current) {
      clearInterval(timer.current);
      timer.current = null;
    }
    vec.current = { ly: 0, rx: 0 };
    setKnob(0, 0);
    onStop();
  };

  return (
    <div
      ref={padRef}
      onPointerDown={start}
      onPointerMove={move}
      onPointerUp={end}
      onPointerCancel={end}
      className={`relative mx-auto rounded-full touch-none ${disabled ? "opacity-40" : "cursor-grab"}`}
      style={{
        width: 200,
        height: 200,
        background: "radial-gradient(circle at 50% 42%,#27304a 0%,#1a2030 62%,#11151e 100%)",
        border: "1px solid var(--color-line)",
        boxShadow: "inset 0 0 34px rgba(0,0,0,.65)",
      }}
    >
      <div
        ref={knobRef}
        className="absolute left-1/2 top-1/2"
        style={{
          width: 78,
          height: 78,
          borderRadius: "50%",
          transform: "translate(-50%,-50%)",
          background: "radial-gradient(circle at 36% 30%,#6aa3ff,#2563eb)",
          boxShadow: "0 6px 18px rgba(0,0,0,.6)",
        }}
      />
    </div>
  );
}
