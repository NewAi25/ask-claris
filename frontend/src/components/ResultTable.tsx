import { useMemo, useState } from "react";
import { ArrowDown, ArrowUp } from "lucide-react";

type Props = {
  columns: string[];
  rows: (string | number | null)[][];
  pageSize?: number;
};

function fmt(v: string | number | null): string {
  if (v === null || v === undefined) return "—";
  if (typeof v === "number") {
    return Number.isInteger(v) ? v.toLocaleString() : v.toLocaleString(undefined, { maximumFractionDigits: 2 });
  }
  const n = Number(v);
  if (v !== "" && Number.isFinite(n) && /^-?\d+(\.\d+)?$/.test(v.trim())) {
    return Number.isInteger(n) ? n.toLocaleString() : n.toLocaleString(undefined, { maximumFractionDigits: 2 });
  }
  return v;
}

export default function ResultTable({ columns, rows, pageSize = 10 }: Props) {
  const [sortCol, setSortCol] = useState<number | null>(null);
  const [asc, setAsc] = useState(false);
  const [page, setPage] = useState(0);

  const sorted = useMemo(() => {
    if (sortCol === null) return rows;
    const copy = [...rows];
    copy.sort((a, b) => {
      const av = a[sortCol];
      const bv = b[sortCol];
      const an = Number(av);
      const bn = Number(bv);
      const bothNum = Number.isFinite(an) && Number.isFinite(bn) && av !== "" && bv !== "";
      let cmp: number;
      if (bothNum) cmp = an - bn;
      else cmp = String(av ?? "").localeCompare(String(bv ?? ""));
      return asc ? cmp : -cmp;
    });
    return copy;
  }, [rows, sortCol, asc]);

  const pages = Math.max(1, Math.ceil(sorted.length / pageSize));
  const current = Math.min(page, pages - 1);
  const slice = sorted.slice(current * pageSize, current * pageSize + pageSize);

  function sortBy(i: number) {
    if (sortCol === i) setAsc(!asc);
    else {
      setSortCol(i);
      setAsc(false);
    }
  }

  if (columns.length === 0) return null;

  return (
    <div className="mt-4">
      <div className="overflow-x-auto rounded-xl border border-slate-200">
        <table className="min-w-full text-sm">
          <thead className="bg-slate-50 text-left text-slate-600">
            <tr>
              {columns.map((c, i) => (
                <th
                  key={i}
                  onClick={() => sortBy(i)}
                  className="cursor-pointer select-none whitespace-nowrap px-3 py-2 font-medium hover:bg-slate-100"
                >
                  <span className="inline-flex items-center gap-1">
                    {c}
                    {sortCol === i && (asc ? <ArrowUp size={12} /> : <ArrowDown size={12} />)}
                  </span>
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {slice.map((r, ri) => (
              <tr key={ri} className="border-t border-slate-100 hover:bg-slate-50">
                {r.map((v, ci) => (
                  <td key={ci} className="whitespace-nowrap px-3 py-2 text-slate-700">
                    {fmt(v)}
                  </td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <div className="mt-2 flex items-center justify-between text-xs text-slate-500">
        <span>
          {sorted.length.toLocaleString()} row{sorted.length === 1 ? "" : "s"}
        </span>
        {pages > 1 && (
          <div className="flex items-center gap-2">
            <button
              className="rounded border border-slate-200 px-2 py-1 disabled:opacity-40"
              disabled={current === 0}
              onClick={() => setPage(current - 1)}
            >
              Prev
            </button>
            <span>
              Page {current + 1} / {pages}
            </span>
            <button
              className="rounded border border-slate-200 px-2 py-1 disabled:opacity-40"
              disabled={current >= pages - 1}
              onClick={() => setPage(current + 1)}
            >
              Next
            </button>
          </div>
        )}
      </div>
    </div>
  );
}
