import { useEffect, useRef } from "react";
import type { FeedItem } from "../types";

const STYLES: Record<FeedItem["kind"], { border: string; label: string; icon: string }> = {
  thought: { border: "border-l-accent", label: "thinking", icon: "💭" },
  action: { border: "border-l-ok", label: "action", icon: "⚙️" },
  result: { border: "border-l-ok/60", label: "result", icon: "↩" },
  sees: { border: "border-l-mut", label: "sees", icon: "👁" },
  error: { border: "border-l-bad", label: "error", icon: "⚠️" },
  estop: { border: "border-l-bad", label: "e-stop", icon: "🛑" },
  approval: { border: "border-l-warn", label: "approval", icon: "🔐" },
  heard: { border: "border-l-mut", label: "heard", icon: "👂" },
};

export default function ThoughtStream({ feed }: { feed: FeedItem[] }) {
  const ref = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (ref.current) ref.current.scrollTop = ref.current.scrollHeight;
  }, [feed]);

  return (
    <div ref={ref} className="scroll-thin overflow-auto flex flex-col gap-2 h-[clamp(280px,46vh,560px)] pr-1">
      {feed.length === 0 && (
        <div className="text-mut text-sm">No activity yet. Set a goal and switch autonomy to assist/auto, or single-step.</div>
      )}
      {feed.map((item) => {
        const s = STYLES[item.kind];
        return (
          <div key={item.id} className={`bg-card2 border border-line ${s.border} border-l-[3px] rounded-lg px-3 py-2`}>
            <div className="text-[10px] uppercase tracking-wider text-mut mb-0.5">
              {s.icon} {s.label}
            </div>
            <div className="text-sm whitespace-pre-wrap break-words">{item.text}</div>
            {item.detail && <div className="text-[11px] font-mono text-[#9fb3d1] mt-1 break-all">{item.detail}</div>}
          </div>
        );
      })}
    </div>
  );
}
