import { useState } from "react";
import { AlertCircle, ChevronDown, ChevronRight, Code2, HelpCircle, Info, ShieldCheck, Sparkles } from "lucide-react";
import type { AskResponse } from "../lib/types";
import ResultTable from "./ResultTable";
import ResultChart from "./ResultChart";

export default function AnswerCard({ resp }: { resp: AskResponse }) {
  const [showSql, setShowSql] = useState(false);

  if (resp.error) {
    return (
      <div className="flex items-start gap-3 rounded-xl border border-red-200 bg-red-50 p-4 text-sm text-red-800">
        <AlertCircle className="mt-0.5 shrink-0" size={18} />
        <div>{resp.error}</div>
      </div>
    );
  }

  if (resp.clarification) {
    return (
      <div className="flex items-start gap-3 rounded-xl border border-amber-200 bg-amber-50 p-4 text-sm text-amber-900">
        <HelpCircle className="mt-0.5 shrink-0" size={18} />
        <div>
          <div className="font-medium">I need a bit more detail</div>
          <div className="mt-1">{resp.clarification}</div>
        </div>
      </div>
    );
  }

  const columns = resp.columns ?? [];
  const rows = resp.rows ?? [];
  const notices = resp.notices ?? [];

  return (
    <div className="rounded-xl border border-slate-200 bg-white p-5">
      {resp.identity && (
        <div className="mb-3 flex items-center gap-2 rounded-lg bg-slate-50 px-3 py-2 text-xs text-slate-600">
          <ShieldCheck size={14} className="shrink-0 text-slate-400" />
          {resp.identity}
        </div>
      )}
      <div className="flex items-start gap-2">
        <Sparkles className="mt-0.5 shrink-0 text-blue-600" size={18} />
        <p className="whitespace-pre-line text-[15px] leading-relaxed text-slate-800">{resp.summary}</p>
      </div>

      {notices.map((n) => (
        <div key={n} className="mt-3 flex items-start gap-2 rounded-lg border border-blue-100 bg-blue-50 px-3 py-2 text-xs text-blue-900">
          <Info size={14} className="mt-0.5 shrink-0" />
          <span>{n}</span>
        </div>
      ))}

      {resp.chart && <ResultChart spec={resp.chart} columns={columns} rows={rows} />}
      {rows.length > 0 && <ResultTable columns={columns} rows={rows} />}

      {resp.sql_used && (
        <div className="mt-4 border-t border-slate-100 pt-3">
          <button
            onClick={() => setShowSql((s) => !s)}
            className="inline-flex items-center gap-1.5 text-xs font-medium text-slate-500 hover:text-slate-800"
          >
            {showSql ? <ChevronDown size={14} /> : <ChevronRight size={14} />}
            <Code2 size={14} /> Query details
          </button>
          {showSql && (
            <div className="mt-2 space-y-2">
              <pre className="overflow-x-auto rounded-lg bg-slate-900 p-3 text-xs leading-relaxed text-slate-100">
                {resp.sql_used}
              </pre>
              <div className="flex flex-wrap gap-2 text-xs text-slate-500">
                {(resp.repairs ?? []).map((r) => (
                  <span key={r} className="rounded bg-amber-100 px-2 py-0.5 text-amber-800">
                    auto-repaired: {r}
                  </span>
                ))}
                {(resp.columns_used ?? []).map((c) => (
                  <span key={c} className="rounded bg-slate-100 px-2 py-0.5">
                    {c}
                  </span>
                ))}
              </div>
            </div>
          )}
        </div>
      )}
    </div>
  );
}
