"""Pluggable LLM provider for the NLQ pipeline (carried over from the prototype).

One interface (LLMProvider) with two methods — plan() and summarize() — selected at
startup by LLM_PROVIDER = anthropic | groq | ollama. Anthropic gets structured output
via forced tool use; Groq/Ollama (OpenAI-compatible) via JSON mode. Malformed output
is coerced to an empty plan so the validate -> one-reprompt loop fires, and a still-bad
result degrades to an honest error rather than a wrong number.
"""
import json
import os
from abc import ABC, abstractmethod

PLAN_TOOL = {
    "name": "emit_plan",
    "description": "Return the query plan for the user's governance question.",
    "input_schema": {
        "type": "object",
        "properties": {
            "needs_clarification": {
                "type": "boolean",
                "description": "True only if the question is too vague/ambiguous to answer with a single query.",
            },
            "clarification": {
                "type": "string",
                "description": "If needs_clarification is true, a short friendly follow-up question. Otherwise empty.",
            },
            "sql": {
                "type": "string",
                "description": "A single read-only DuckDB SELECT over the documented schema-qualified tables. Empty if needs_clarification.",
            },
            "chart": {
                "type": "object",
                "properties": {
                    "type": {"type": "string", "enum": ["bar", "line", "scatter", "pie", "table_only"]},
                    "x": {"type": "string", "description": "Result column for the x axis / category (or empty)."},
                    "y": {"type": "string", "description": "Result column for the y axis / value (or empty)."},
                },
                "required": ["type", "x", "y"],
            },
            "columns_used": {"type": "array", "items": {"type": "string"}},
        },
        "required": ["needs_clarification", "clarification", "sql", "chart", "columns_used"],
    },
}

JSON_INSTRUCTION = (
    "Respond with ONLY a JSON object (no prose, no markdown fences) with these exact keys: "
    "needs_clarification (boolean), clarification (string), "
    "sql (string — a single read-only SELECT over the documented tables), "
    "chart (object with keys type, x, y), columns_used (array of strings)."
)


def _parse_json_loose(content: str) -> dict:
    if not content:
        return {}
    s = content.strip()
    if s.startswith("```"):
        s = s.strip("`")
        if s[:4].lower() == "json":
            s = s[4:]
        s = s.strip()
    try:
        return json.loads(s)
    except Exception:
        pass
    i, j = s.find("{"), s.rfind("}")
    if i != -1 and j != -1 and j > i:
        try:
            return json.loads(s[i:j + 1])
        except Exception:
            return {}
    return {}


def _coerce_plan(d) -> dict:
    if not isinstance(d, dict):
        d = {}
    chart = d.get("chart") if isinstance(d.get("chart"), dict) else {}
    cols = d.get("columns_used")
    return {
        "needs_clarification": bool(d.get("needs_clarification", False)),
        "clarification": str(d.get("clarification") or ""),
        "sql": str(d.get("sql") or ""),
        "chart": {
            "type": str(chart.get("type") or "table_only"),
            "x": str(chart.get("x") or ""),
            "y": str(chart.get("y") or ""),
        },
        "columns_used": cols if isinstance(cols, list) else [],
    }


class LLMProvider(ABC):
    name: str = "base"
    model: str = ""

    @abstractmethod
    def plan(self, system: str, user_message: str, retry_error: str | None = None) -> dict:
        """Return a dict matching the emit_plan schema."""

    @abstractmethod
    def summarize(self, system: str, prompt: str) -> str:
        """Return a plain-text natural-language summary grounded in the rows."""


class AnthropicProvider(LLMProvider):
    name = "anthropic"

    def __init__(self):
        from anthropic import Anthropic

        self.client = Anthropic()  # reads ANTHROPIC_API_KEY
        self.model = os.getenv("ANTHROPIC_MODEL", "claude-sonnet-4-6")

    def plan(self, system, user_message, retry_error=None):
        um = user_message
        if retry_error:
            um += (
                f"\n\nYour previous query failed validation: {retry_error}\n"
                "Return a corrected plan that uses only real tables/columns and a single read-only SELECT."
            )
        resp = self.client.messages.create(
            model=self.model,
            max_tokens=1500,
            system=system,
            tools=[PLAN_TOOL],
            tool_choice={"type": "tool", "name": "emit_plan"},
            messages=[{"role": "user", "content": um}],
        )
        for block in resp.content:
            if block.type == "tool_use" and block.name == "emit_plan":
                return _coerce_plan(block.input)
        raise RuntimeError("Model did not return a plan.")

    def summarize(self, system, prompt):
        resp = self.client.messages.create(
            model=self.model,
            max_tokens=600,
            system=system,
            messages=[{"role": "user", "content": prompt}],
        )
        return "".join(b.text for b in resp.content if b.type == "text").strip()


class _OpenAICompatProvider(LLMProvider):
    def __init__(self, base_url: str, api_key: str, model: str):
        from openai import OpenAI

        self.client = OpenAI(base_url=base_url, api_key=api_key)
        self.model = model

    def plan(self, system, user_message, retry_error=None):
        sys = system + "\n\n" + JSON_INSTRUCTION
        um = user_message
        if retry_error:
            um += (
                f"\n\nYour previous response failed validation: {retry_error}\n"
                "Return a corrected JSON plan that uses only real tables/columns and a single read-only SELECT."
            )
        resp = self.client.chat.completions.create(
            model=self.model,
            messages=[{"role": "system", "content": sys}, {"role": "user", "content": um}],
            response_format={"type": "json_object"},
            temperature=0,
        )
        content = resp.choices[0].message.content or ""
        return _coerce_plan(_parse_json_loose(content))

    def summarize(self, system, prompt):
        resp = self.client.chat.completions.create(
            model=self.model,
            messages=[{"role": "system", "content": system}, {"role": "user", "content": prompt}],
            temperature=0.2,
        )
        return (resp.choices[0].message.content or "").strip()


class GroqProvider(_OpenAICompatProvider):
    name = "groq"

    def __init__(self):
        super().__init__(
            base_url="https://api.groq.com/openai/v1",
            api_key=os.getenv("GROQ_API_KEY") or "groq-key-not-set",
            model=os.getenv("GROQ_MODEL", "llama-3.3-70b-versatile"),
        )


class OllamaProvider(_OpenAICompatProvider):
    name = "ollama"

    def __init__(self):
        super().__init__(
            base_url=os.getenv("OLLAMA_BASE_URL", "http://localhost:11434/v1"),
            api_key="ollama",
            model=os.getenv("OLLAMA_MODEL", "qwen2.5-coder:7b"),
        )


_REGISTRY = {"anthropic": AnthropicProvider, "groq": GroqProvider, "ollama": OllamaProvider}


def make_provider() -> LLMProvider:
    name = os.getenv("LLM_PROVIDER", "anthropic").strip().lower()
    cls = _REGISTRY.get(name)
    if cls is None:
        raise ValueError(f"Unknown LLM_PROVIDER '{name}'. Use one of: {', '.join(_REGISTRY)}.")
    return cls()
