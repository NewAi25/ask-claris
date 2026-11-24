export type ChartSpec = {
  type: "bar" | "line" | "scatter" | "pie" | "table_only";
  x: string;
  y: string;
};

export type AskContext = {
  role: string;
  detailLevel: string;
  revealPii: boolean;
};

export type AskResponse = {
  summary?: string;
  columns?: string[];
  rows?: (string | number | null)[][];
  chart?: ChartSpec;
  sql_used?: string | null;
  columns_used?: string[];
  row_count?: number;
  notices?: string[];
  repairs?: string[];
  identity?: string;
  clarification?: string;
  error?: string;
};

export type HistoryItem = {
  id: string;
  question: string;
  context: AskContext;
  timestamp: string;
  response: AskResponse;
};
