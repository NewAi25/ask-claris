import type { ReactNode } from "react";
import { LayoutDashboard, Sparkles } from "lucide-react";

export type Page = "dashboard" | "ask";

const NAV: { id: Page; label: string; icon: ReactNode }[] = [
  { id: "dashboard", label: "Dashboard", icon: <LayoutDashboard size={18} /> },
  { id: "ask", label: "Ask Claris", icon: <Sparkles size={18} /> },
];

export default function Sidebar({ page, setPage }: { page: Page; setPage: (p: Page) => void }) {
  return (
    <aside className="flex w-64 shrink-0 flex-col border-r border-slate-200 bg-white px-4 py-5">
      <div className="mb-8 flex items-center gap-2 px-2">
        <div className="flex h-9 w-9 items-center justify-center rounded-lg bg-ink text-white">
          <Sparkles size={18} />
        </div>
        <div>
          <div className="text-sm font-semibold leading-tight text-slate-900">Claris</div>
          <div className="text-[11px] leading-tight text-slate-500">Model Governance</div>
        </div>
      </div>

      <nav className="flex flex-col gap-1">
        {NAV.map((n) => {
          const active = page === n.id;
          return (
            <button
              key={n.id}
              onClick={() => setPage(n.id)}
              className={
                "flex items-center gap-3 rounded-lg px-3 py-2 text-sm font-medium transition " +
                (active ? "bg-ink text-white" : "text-slate-600 hover:bg-slate-100")
              }
            >
              {n.icon}
              {n.label}
            </button>
          );
        })}
      </nav>

      <div className="mt-auto px-2 text-[11px] leading-relaxed text-slate-400">
        Natural-language governance copilot for the dispute-risk models. Every number comes from a real query.
      </div>
    </aside>
  );
}
