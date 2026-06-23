import { useEffect, useRef } from "react";
import type { FeedItem } from "../types";

const KIND: Record<FeedItem["kind"], { glyph: string; tone: string; label: string }> = {
  thought: { glyph: "▸", tone: "text-accent", label: "COGITO" },
  action: { glyph: "⚙", tone: "text-gold", label: "ACT" },
  result: { glyph: "↩", tone: "text-ok", label: "OK" },
  sees: { glyph: "◉", tone: "text-fg", label: "VISION" },
  error: { glyph: "⚠", tone: "text-bad", label: "ERR" },
  estop: { glyph: "■", tone: "text-bad", label: "E-STOP" },
  approval: { glyph: "🔐", tone: "text-warn", label: "AUTH" },
  heard: { glyph: "👂", tone: "text-accent", label: "HEARD" },
};

/** Compact overlay: the last `n` thoughts/commands, Terminator-style, for the main video HUD. */
export function TerminatorFeed({ feed, n = 2 }: { feed: FeedItem[]; n?: number }) {
  const recent = feed.slice(-n);
  return (
    <div className="flex flex-col gap-1">
      {recent.length === 0 && <div className="text-[11px] hud-mono text-mut">▸ awaiting cognition…</div>}
      {recent.map((it) => {
        const k = KIND[it.kind];
        return (
          <div key={it.id} className="feed-in flex items-start gap-2 text-[12px] hud-mono leading-snug">
            <span className={`${k.tone}`}>{k.glyph}</span>
            <span className={`${k.tone} opacity-60 shrink-0`}>{k.label}</span>
            <span className="text-fg/90 break-words line-clamp-2">{it.text}</span>
          </div>
        );
      })}
    </div>
  );
}

/** Full scrollable activity log. */
export default function ThoughtFeed({ feed }: { feed: FeedItem[] }) {
  const ref = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (ref.current) ref.current.scrollTop = ref.current.scrollHeight;
  }, [feed]);

  return (
    <div ref={ref} className="scroll-thin overflow-auto flex flex-col gap-1.5 h-[clamp(220px,38vh,460px)] pr-1">
      {feed.length === 0 && <div className="text-mut text-sm hud-mono">▸ no activity yet</div>}
      {feed.map((it) => {
        const k = KIND[it.kind];
        const ts = new Date(it.ts * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });
        return (
          <div key={it.id} className="bg-card2/50 border border-line/60 rounded-md px-2.5 py-1.5">
            <div className="flex items-center gap-2 text-[9px] uppercase tracking-wider">
              <span className={k.tone}>{k.glyph} {k.label}</span>
              <span className="text-mut ml-auto hud-mono">{ts}</span>
            </div>
            <div className="text-[13px] whitespace-pre-wrap break-words mt-0.5">{it.text}</div>
            {it.detail && <div className="text-[11px] hud-mono text-accent/70 mt-1 break-all">{it.detail}</div>}
          </div>
        );
      })}
    </div>
  );
}
