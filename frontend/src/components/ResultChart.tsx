import {
  Bar,
  BarChart,
  CartesianGrid,
  Cell,
  Line,
  LineChart,
  Pie,
  PieChart,
  ResponsiveContainer,
  Scatter,
  ScatterChart,
  Tooltip,
  XAxis,
  YAxis,
  ZAxis,
} from "recharts";
import type { ChartSpec } from "../lib/types";

const COLORS = ["#0f172a", "#3b82f6", "#10b981", "#f59e0b", "#ef4444", "#8b5cf6", "#14b8a6", "#ec4899"];

type Props = {
  spec: ChartSpec;
  columns: string[];
  rows: (string | number | null)[][];
};

function toNum(v: unknown): number | null {
  if (v === null || v === undefined || v === "") return null;
  const n = Number(v);
  return Number.isFinite(n) ? n : null;
}

// Build [{x, y}] records, resolving the spec's x/y against actual columns,
// falling back sensibly when the model's axis hints don't match.
function buildData(spec: ChartSpec, columns: string[], rows: Props["rows"]) {
  const idx = (name: string) => columns.findIndex((c) => c.toLowerCase() === (name || "").toLowerCase());
  let xi = idx(spec.x);
  let yi = idx(spec.y);
  // Fallbacks: first non-numeric col for x, first numeric col for y.
  const numericCols = columns.map((_, i) => rows.some((r) => toNum(r[i]) !== null));
  if (xi < 0) xi = columns.findIndex((_, i) => !numericCols[i]);
  if (xi < 0) xi = 0;
  if (yi < 0) yi = columns.findIndex((_, i) => numericCols[i] && i !== xi);
  if (yi < 0) yi = columns.length > 1 ? 1 : 0;
  return {
    xi,
    yi,
    data: rows.slice(0, 50).map((r) => ({
      x: r[xi] === null ? "—" : String(r[xi]),
      y: toNum(r[yi]) ?? 0,
    })),
  };
}

export default function ResultChart({ spec, columns, rows }: Props) {
  if (!spec || spec.type === "table_only" || rows.length === 0 || columns.length === 0) return null;

  const { xi, yi, data } = buildData(spec, columns, rows);
  const xLabel = columns[xi] ?? spec.x;
  const yLabel = columns[yi] ?? spec.y;

  return (
    <div className="mt-4 rounded-xl border border-slate-200 bg-white p-4">
      <div className="mb-2 text-xs font-medium uppercase tracking-wide text-slate-500">
        {spec.type} chart · {yLabel} by {xLabel}
      </div>
      <ResponsiveContainer width="100%" height={300}>
        {spec.type === "line" ? (
          <LineChart data={data} margin={{ top: 8, right: 16, bottom: 24, left: 8 }}>
            <CartesianGrid strokeDasharray="3 3" stroke="#eef2f7" />
            <XAxis dataKey="x" tick={{ fontSize: 11 }} angle={-20} textAnchor="end" height={50} />
            <YAxis tick={{ fontSize: 11 }} width={70} />
            <Tooltip />
            <Line type="monotone" dataKey="y" name={yLabel} stroke="#3b82f6" strokeWidth={2} dot={false} />
          </LineChart>
        ) : spec.type === "scatter" ? (
          <ScatterChart margin={{ top: 8, right: 16, bottom: 24, left: 8 }}>
            <CartesianGrid stroke="#eef2f7" />
            <XAxis dataKey="x" name={xLabel} tick={{ fontSize: 11 }} />
            <YAxis dataKey="y" name={yLabel} tick={{ fontSize: 11 }} width={70} />
            <ZAxis range={[60, 60]} />
            <Tooltip cursor={{ strokeDasharray: "3 3" }} />
            <Scatter data={data} fill="#3b82f6" />
          </ScatterChart>
        ) : spec.type === "pie" ? (
          <PieChart>
            <Tooltip />
            <Pie data={data} dataKey="y" nameKey="x" cx="50%" cy="50%" outerRadius={110} label>
              {data.map((_, i) => (
                <Cell key={i} fill={COLORS[i % COLORS.length]} />
              ))}
            </Pie>
          </PieChart>
        ) : (
          <BarChart data={data} margin={{ top: 8, right: 16, bottom: 24, left: 8 }}>
            <CartesianGrid strokeDasharray="3 3" stroke="#eef2f7" />
            <XAxis dataKey="x" tick={{ fontSize: 11 }} angle={-20} textAnchor="end" height={50} />
            <YAxis tick={{ fontSize: 11 }} width={70} />
            <Tooltip />
            <Bar dataKey="y" name={yLabel} radius={[4, 4, 0, 0]}>
              {data.map((_, i) => (
                <Cell key={i} fill={COLORS[i % COLORS.length]} />
              ))}
            </Bar>
          </BarChart>
        )}
      </ResponsiveContainer>
    </div>
  );
}
