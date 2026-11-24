import type { AskContext, AskResponse } from "./types";

const BASE = import.meta.env.VITE_API_URL ?? "/api";

export async function ask(question: string, ctx: AskContext): Promise<AskResponse> {
  const res = await fetch(`${BASE}/ask`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ question, ...ctx }),
  });
  if (!res.ok) {
    return { error: `Server error (${res.status}). Is the backend running on :8000?` };
  }
  return res.json();
}

export async function meta() {
  try {
    const res = await fetch(`${BASE}/meta`);
    if (!res.ok) return null;
    return res.json();
  } catch {
    return null;
  }
}
