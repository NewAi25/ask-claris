import { useMemo, useState, type KeyboardEvent } from "react";
import { Eye, EyeOff, History as HistoryIcon, Search, Send, Sparkles } from "lucide-react";
import { Select } from "../components/Field";
import AnswerCard from "../components/AnswerCard";
import { ask } from "../lib/api";
import { QUICK_SUGGESTIONS, RECOMMENDED, ROLES, type Role } from "../lib/suggestions";
import type { AskContext, AskResponse, HistoryItem } from "../lib/types";

const DETAIL_LEVELS = ["Concise", "Detailed"];

let counter = 0;
const newId = () => `q${++counter}-${Date.now()}`;

export default function AskClaris() {
  const [detailLevel, setDetailLevel] = useState("Concise");
  const [role, setRole] = useState<Role>("Compliance Officer");
  const [revealPii, setRevealPii] = useState(false);

  const [tab, setTab] = useState<"ask" | "history">("ask");
  const [question, setQuestion] = useState("");
  const [search, setSearch] = useState("");
  const [loading, setLoading] = useState(false);
  const [current, setCurrent] = useState<{ question: string; resp: AskResponse } | null>(null);
  const [history, setHistory] = useState<HistoryItem[]>([]);

  const ctx: AskContext = { role, detailLevel, revealPii };

  const recommended = useMemo(() => {
    const list = RECOMMENDED[role];
    if (!search.trim()) return list;
    const q = search.toLowerCase();
    return list.filter((s) => s.toLowerCase().includes(q));
  }, [role, search]);

  async function run(q: string) {
    const text = q.trim();
    if (!text || loading) return;
    setQuestion(text);
    setTab("ask");
    setLoading(true);
    setCurrent({ question: text, resp: {} });
    const resp = await ask(text, ctx);
    setCurrent({ question: text, resp });
    setLoading(false);
    setHistory((h) => [
      {
        id: newId(),
        question: text,
        context: { ...ctx },
        timestamp: new Date().toLocaleString(),
        response: resp,
      },
      ...h,
    ]);
  }

  function onKeyDown(e: KeyboardEvent<HTMLTextAreaElement>) {
    if ((e.metaKey || e.ctrlKey) && e.key === "Enter") {
      e.preventDefault();
      run(question);
    }
  }

  return (
    <div className="mx-auto max-w-6xl px-6 py-6">
      {/* Header */}
      <div className="flex flex-wrap items-start justify-between gap-4">
        <div className="flex items-start gap-3">
          <div className="mt-0.5 flex h-10 w-10 items-center justify-center rounded-xl bg-ink text-white">
            <Sparkles size={20} />
          </div>
          <div>
            <h1 className="text-2xl font-semibold text-slate-900">Ask Claris</h1>
            <p className="text-sm text-slate-500">
              Governance copilot for the dispute-risk models — explainability, fairness, case audits, consent and drift.
            </p>
          </div>
        </div>
        <div className="flex items-end gap-3">
          <div className="w-48">
            <Select label="Role" value={role} onChange={(v) => setRole(v as Role)} options={[...ROLES]} />
          </div>
          <div className="w-36">
            <Select label="Detail Level" value={detailLevel} onChange={setDetailLevel} options={DETAIL_LEVELS} />
          </div>
          <button
            onClick={() => setRevealPii((s) => !s)}
            title="PII is masked by default. Reveals are logged to the query audit."
            className={
              "inline-flex h-9 items-center gap-1.5 rounded-lg border px-3 text-xs font-medium transition " +
              (revealPii
                ? "border-red-300 bg-red-50 text-red-700"
                : "border-slate-200 bg-white text-slate-600 hover:bg-slate-50")
            }
          >
            {revealPii ? <Eye size={14} /> : <EyeOff size={14} />}
            {revealPii ? "PII revealed (audited)" : "PII masked"}
          </button>
        </div>
      </div>
      <div className="mt-3 text-xs text-slate-500">
        Scope: <span className="font-medium text-slate-700">core portfolio</span> · Champion:{" "}
        <span className="font-medium text-slate-700">Model 4 Autoencoder</span> · All three models queryable
      </div>

      {/* Body */}
      <div className="mt-6 grid grid-cols-1 gap-6 lg:grid-cols-3">
        {/* Left: recommended */}
        <div className="lg:col-span-1">
          <div className="relative">
            <Search size={16} className="absolute left-3 top-1/2 -translate-y-1/2 text-slate-400" />
            <input
              value={search}
              onChange={(e) => setSearch(e.target.value)}
              placeholder="Search questions…"
              className="w-full rounded-lg border border-slate-200 bg-white py-2 pl-9 pr-3 text-sm outline-none focus:border-slate-400"
            />
          </div>
          <h3 className="mb-2 mt-4 text-sm font-semibold text-slate-700">Recommended for Your Role</h3>
          <div className="max-h-[460px] space-y-2 overflow-y-auto pr-1">
            {recommended.map((q) => (
              <button
                key={q}
                onClick={() => run(q)}
                className="block w-full rounded-lg border border-slate-200 bg-white px-3 py-2 text-left text-sm text-slate-700 transition hover:border-slate-300 hover:bg-slate-50"
              >
                {q}
              </button>
            ))}
            {recommended.length === 0 && (
              <div className="text-sm text-slate-400">No matching questions.</div>
            )}
          </div>
        </div>

        {/* Right: ask + history */}
        <div className="lg:col-span-2">
          <div className="mb-4 flex items-center gap-2 border-b border-slate-200">
            <button
              onClick={() => setTab("ask")}
              className={
                "px-3 py-2 text-sm font-medium " +
                (tab === "ask" ? "border-b-2 border-ink text-slate-900" : "text-slate-500")
              }
            >
              <span className="inline-flex items-center gap-1.5">
                <Sparkles size={15} /> Ask Claris
              </span>
            </button>
            <button
              onClick={() => setTab("history")}
              className={
                "px-3 py-2 text-sm font-medium " +
                (tab === "history" ? "border-b-2 border-ink text-slate-900" : "text-slate-500")
              }
            >
              <span className="inline-flex items-center gap-1.5">
                <HistoryIcon size={15} /> History ({history.length})
              </span>
            </button>
          </div>

          {tab === "ask" ? (
            <>
              <div className="rounded-xl border border-slate-200 bg-white p-4">
                <textarea
                  value={question}
                  onChange={(e) => setQuestion(e.target.value)}
                  onKeyDown={onKeyDown}
                  rows={4}
                  placeholder="e.g. Why was client CUST_00042 flagged?  ·  Is the model fair across segments on validation?  ·  Has performance drifted over the OOT months?"
                  className="w-full resize-none text-sm outline-none placeholder:text-slate-400"
                />
                <div className="mt-2 flex items-center justify-between">
                  <span className="text-xs text-slate-400">Press ⌘/Ctrl + Enter to submit</span>
                  <button
                    onClick={() => run(question)}
                    disabled={loading || !question.trim()}
                    className="inline-flex items-center gap-2 rounded-lg bg-ink px-4 py-2 text-sm font-medium text-white transition hover:bg-slate-800 disabled:opacity-50"
                  >
                    <Send size={15} />
                    {loading ? "Asking…" : "Ask"}
                  </button>
                </div>
              </div>

              <div className="mt-3">
                <div className="mb-2 text-xs font-medium text-slate-500">Quick suggestions:</div>
                <div className="flex flex-col gap-2">
                  {QUICK_SUGGESTIONS.map((q) => (
                    <button
                      key={q}
                      onClick={() => run(q)}
                      className="rounded-lg border border-slate-200 bg-white px-3 py-2 text-left text-sm text-slate-600 hover:bg-slate-50"
                    >
                      {q}
                    </button>
                  ))}
                </div>
              </div>

              <div className="mt-5">
                {loading && (
                  <div className="animate-pulse rounded-xl border border-slate-200 bg-white p-5 text-sm text-slate-400">
                    Claris is querying the governance database…
                  </div>
                )}
                {!loading && current && <AnswerCard resp={current.resp} />}
              </div>
            </>
          ) : (
            <div className="space-y-3">
              {history.length === 0 && (
                <div className="rounded-xl border border-dashed border-slate-200 p-6 text-center text-sm text-slate-400">
                  No questions yet. Ask Claris something to build your history.
                </div>
              )}
              {history.map((h) => {
                const oneLine = h.response.summary || h.response.clarification || h.response.error || "";
                return (
                  <button
                    key={h.id}
                    onClick={() => {
                      setCurrent({ question: h.question, resp: h.response });
                      setTab("ask");
                    }}
                    className="block w-full rounded-xl border border-slate-200 bg-white p-4 text-left transition hover:border-slate-300"
                  >
                    <div className="text-sm font-medium text-slate-900">{h.question}</div>
                    <div className="mt-1 text-[11px] text-slate-400">
                      {h.context.role} · {h.context.detailLevel}
                      {h.context.revealPii ? " · PII revealed" : ""} · {h.timestamp}
                    </div>
                    <div className="mt-2 line-clamp-2 text-sm text-slate-600">{oneLine}</div>
                  </button>
                );
              })}
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
