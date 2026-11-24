import { useEffect, useState } from "react";
import Sidebar, { type Page } from "./components/Sidebar";
import AskClaris from "./pages/AskClaris";
import { meta } from "./lib/api";

type Meta = {
  tables: Record<string, number>;
  champion: string;
  provider: string;
  model: string;
};

function Dashboard() {
  const [m, setM] = useState<Meta | null>(null);
  useEffect(() => {
    meta().then((x) => x && setM(x));
  }, []);
  const tables = m ? Object.entries(m.tables) : [];
  return (
    <div className="mx-auto max-w-5xl px-6 py-10">
      <h1 className="text-2xl font-semibold text-slate-900">Dashboard</h1>
      <p className="mt-1 text-sm text-slate-500">Governance database currently loaded (core portfolio).</p>
      <div className="mt-6 grid grid-cols-1 gap-4 sm:grid-cols-3">
        <div className="rounded-xl border border-slate-200 bg-white p-5">
          <div className="text-sm text-slate-500">Champion model</div>
          <div className="mt-1 text-xl font-semibold text-slate-900">{m ? m.champion : "—"}</div>
          <div className="text-xs text-slate-400">default when no model is named; fair &amp; explainable, weak AUC</div>
        </div>
        <div className="rounded-xl border border-slate-200 bg-white p-5">
          <div className="text-sm text-slate-500">Queryable tables</div>
          <div className="mt-1 text-xl font-semibold text-slate-900">{m ? tables.length : "—"}</div>
          <div className="text-xs text-slate-400">core · privacy · lineage · vec</div>
        </div>
        <div className="rounded-xl border border-slate-200 bg-white p-5">
          <div className="text-sm text-slate-500">NLQ provider</div>
          <div className="mt-1 text-xl font-semibold text-slate-900">{m ? m.provider : "—"}</div>
          <div className="text-xs text-slate-400">{m ? m.model : ""}</div>
        </div>
      </div>
      {m && (
        <div className="mt-6 rounded-xl border border-slate-200 bg-white p-5">
          <div className="mb-3 text-sm font-semibold text-slate-700">Tables</div>
          <div className="grid grid-cols-1 gap-x-8 gap-y-1 text-sm sm:grid-cols-2">
            {tables.map(([t, n]) => (
              <div key={t} className="flex justify-between border-b border-slate-50 py-1">
                <span className="text-slate-600">{t}</span>
                <span className="text-slate-400">{n} cols</span>
              </div>
            ))}
          </div>
        </div>
      )}
      <p className="mt-6 text-sm text-slate-500">
        Head to <span className="font-medium text-slate-700">Ask Claris</span> to query the governance data in plain
        English.
      </p>
    </div>
  );
}

export default function App() {
  const [page, setPage] = useState<Page>("ask");
  return (
    <div className="flex h-full">
      <Sidebar page={page} setPage={setPage} />
      <main className="h-full flex-1 overflow-y-auto">
        {page === "ask" && <AskClaris />}
        {page === "dashboard" && <Dashboard />}
      </main>
    </div>
  );
}
