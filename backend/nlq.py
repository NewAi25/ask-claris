"""NLQ pipeline: question -> (deterministic scope/audit handlers) -> LLM plan ->
SELECT-only guard -> column/table auto-repair -> execute -> PII mask -> grounded answer.

Every number shown to the user comes from an executed query. On failure the engine
returns an honest "couldn't build that" — never a fabricated number.
"""
import json
import re
import time

from db import DB, ROW_LIMIT
from masking import mask_results, mask_value
from providers import make_provider
from schema_doc import build_schema_doc

# Gold examples that supplement vec.nlq_training without requiring a DB rebuild
# (covers every question the UI suggests). Merged with the DB seeds at startup.
EXTRA_SEEDS = [
    ("How many consent records are withdrawn or expired?",
     "SELECT consent_state, count(*) AS n FROM privacy.consent_status "
     "WHERE consent_state IN ('withdrawn','expired') GROUP BY consent_state",
     "bar"),
    ("Which segments have fairness metrics that were not computed?",
     "SELECT segment, metric, count(*) AS not_computed FROM core.fairness_metrics "
     "WHERE value IS NULL AND source = 'segment' GROUP BY segment, metric ORDER BY segment LIMIT 100",
     "table_only"),
    ("Compare AUC_ROC across all three models on validation.",
     "SELECT model_id, value AS auc_roc FROM core.performance_metrics "
     "WHERE dataset_norm = 'validation' AND metric = 'AUC_ROC' ORDER BY model_id",
     "bar"),
    ("Show the autoencoder's precision and recall by OOT month.",
     "SELECT period, metric, value FROM core.performance_metrics "
     "WHERE model_id = 'm4_autoencoder' AND dataset_norm = 'oot_monthly' "
     "AND metric IN ('Precision','Recall') ORDER BY period_start, metric",
     "line"),
    ("How many clients are in each customer segment?",
     "SELECT segment, count(*) AS clients FROM privacy.customer_master "
     "GROUP BY segment ORDER BY clients DESC",
     "bar"),
    ("How fair is the champion model on validation overall?",
     "SELECT metric, value FROM core.fairness_metrics WHERE model_id = 'm4_autoencoder' "
     "AND dataset = 'Validation' AND source = 'global' LIMIT 10",
     "bar"),
    ("How many disputes are in the validation period?",
     "SELECT file, new_disputes, min_date, max_date FROM lineage.data_splits "
     "WHERE file = 'val_oos.csv'",
     "table_only"),
]

CHAMPION = "m4_autoencoder"

HONESTY_RULES = """GOVERNANCE & HONESTY RULES (non-negotiable):
- Scope: in-scope dispute-risk models only. Models: m2_xgboost (XGBoost), m3_rf (Random Forest), m4_autoencoder (Autoencoder). The champion/default is m4_autoencoder — when the user does not name a model, query m4_autoencoder.
- NEVER present the autoencoder champion as high-performing. Its Validation AUC_ROC is ~0.53 (near random). Always report the real scorecard numbers for whichever model is asked about.
- "Which model is best?" has no single winner: XGBoost/RF reach higher validation AUC (~0.69) but are badly overfit (Train AUC 1.0) and have poor fairness (XGBoost ~20% statistical parity on validation); the autoencoder is fair (~97% statistical parity) and the only model with local explanations, but weakly discriminative. Present the trade-off, grounded in queried numbers.
- Exactly ONE feature-importance method exists per model (gain / mda / recon_error_gap). Never claim methods "agree", never invent SHAP/permutation comparisons.
- NULL fairness values mean the metric was NOT COMPUTED for that slice. Say "not computed" — never 0, never 100, never impute.
- Local (per-client) explanations exist ONLY for the autoencoder, as 20,000-row samples with partial client linkage. XGBoost/RF have no local explanations.
- Distribution drift / PSI is NOT computed in v1; only performance drift across Validation -> Internal OOT -> Oct/Nov/Dec 2025 is available.
- In core.cases, dispute rows (target=1) are oversampled 10x — use COUNT(DISTINCT client_id) when counting clients."""

SQL_RULES = f"""SQL RULES:
- A single read-only DuckDB SELECT (or WITH ... SELECT). Never INSERT/UPDATE/DELETE/DROP/ALTER/CREATE/COPY/ATTACH/PRAGMA/SET.
- Use ONLY the schema-qualified tables and columns documented above. Do not invent columns.
- Always include a LIMIT (<= {ROW_LIMIT}).
- Quote columns containing spaces or special characters with double quotes.
- Use TRY_CAST (not CAST) when a text column needs numeric comparison.
- For trends over OOT months, ORDER BY period_start and chart type 'line'.
- Consent status: use privacy.consent_status and its consent_state column (values 'active', 'expired', 'withdrawn', 'unknown'). NEVER compare granted_date/expiry_date/withdrawn_date to status words — those are DATE columns.
- Chart: 'bar' for category comparisons / top-N, 'line' for time trends, 'scatter' for two numerics, 'pie' for small share breakdowns, 'table_only' when a chart adds nothing. x/y are result column aliases.
- If the question is genuinely ambiguous or unanswerable from this schema, set needs_clarification=true with a short follow-up; leave sql empty."""

SUMMARY_SYSTEM = (
    "You are Claris, a model-governance copilot for dispute-risk models. "
    "Summarize query results factually for the asked persona. Use the provided numbers "
    "EXACTLY as written — never transform them. NULLs mean 'not computed'. "
    + HONESTY_RULES
)

# ---------------------------------------------------------- intent detection
FO_RE = re.compile(r"\b(segment\s*b)\b", re.I)
PSI_RE = re.compile(r"\b(psi|population stability|distribution (drift|shift)|kl[- ]divergence|feature drift|data drift)\b", re.I)
CLIENT_RE = re.compile(r"\b(CUST_\d+)\b", re.I)
WHY_RE = re.compile(r"\b(why|flag|flagged|explain|driver|drivers|reason|contribut\w*)\b", re.I)
CONSENT_RE = re.compile(r"\b(consent|opt[- ]?in|opted|gdpr)\b", re.I)
BEST_MODEL_RE = re.compile(
    r"(which|what)\s+model\s+(is\s+)?(the\s+)?(best|better|strongest|right)"
    r"|\bbest\s+model\b|\bmodel\s+is\s+best\b|recommend\w*\s+model|model\s+would\s+you\s+recommend", re.I)

MODEL_PATTERNS = [
    (re.compile(r"\bxg\s*boost|xgboost|\bxgb\b|\bm2\b", re.I), "m2_xgboost", "XGBoost"),
    (re.compile(r"random\s*forest|\brf\b|\bm3\b", re.I), "m3_rf", "Random Forest"),
    (re.compile(r"auto\s*-?encoder|\bae\b|\bm4\b|champion", re.I), "m4_autoencoder", "Autoencoder"),
]


def detect_model(question: str):
    for pat, mid, name in MODEL_PATTERNS:
        if pat.search(question):
            return mid, name
    return None, None


class NLQEngine:
    def __init__(self, db: DB):
        self.db = db
        self.provider = make_provider()
        # Small local models have ~4k-token contexts; trim the schema doc for them.
        self.schema_doc = build_schema_doc(db, compact=(self.provider.name == "ollama"))
        db_seeds = db.con.execute(
            "SELECT question, gold_sql, chart_type FROM vec.nlq_training").fetchall()
        seen = set()
        self.seeds = []
        for q, sql, chart in list(db_seeds) + EXTRA_SEEDS:
            if q.lower() not in seen:
                seen.add(q.lower())
                self.seeds.append((q, sql, chart))
        print(f"[claris] provider={self.provider.name} model={self.provider.model} "
              f"seeds={len(self.seeds)}")

    def _select_examples(self, question: str, k: int = 5) -> str:
        """Pick the k seed examples most lexically similar to the question, so the
        prompt stays small enough for ~4k-token local models as the seed pool grows."""
        qt = set(re.findall(r"[a-z0-9_]+", question.lower()))
        scored = []
        for q, sql, chart in self.seeds:
            st = set(re.findall(r"[a-z0-9_]+", q.lower()))
            scored.append((len(qt & st) / (len(st) ** 0.5 + 1.0), q, sql, chart))
        scored.sort(key=lambda x: x[0], reverse=True)
        lines = ["WORKED EXAMPLES (real, validated queries — copy the exact table/column spellings):"]
        for _, q, sql, chart in scored[:k]:
            lines.append(f"  Q: {q}\n    sql: {sql}\n    chart: {chart}")
        return "\n".join(lines)

    # ------------------------------------------------------------- public API
    def ask(self, question: str, ctx: dict) -> dict:
        t0 = time.time()
        reveal = bool(ctx.get("revealPii", False))
        q = question.strip()

        if FO_RE.search(q):
            return self._refuse_fo(q, reveal, t0)
        if PSI_RE.search(q):
            return self._psi_deferred(q, ctx, reveal, t0)
        if CLIENT_RE.search(q) and CONSENT_RE.search(q):
            return self._consent(q, reveal, t0)
        if CLIENT_RE.search(q) and WHY_RE.search(q):
            return self._case_audit(q, ctx, reveal, t0)
        if BEST_MODEL_RE.search(q):
            return self._model_tradeoff(q, reveal, t0)
        return self._llm_path(q, ctx, reveal, t0)

    # -------------------------------------------------------- scope handlers
    def _refuse_fo(self, q, reveal, t0):
        msg = ("Segment B is out of scope for Ask Claris v1: the governance "
               "database covers the in-scope dispute-risk models only — no Segment B model, "
               "explainability, or fairness artifacts exist yet. I can answer the same "
               "question for the core portfolio if you'd like.")
        self._audit(q, None, None, None, 0, reveal, "refused_scope_fo", None, t0)
        return {"summary": msg, "columns": [], "rows": [], "row_count": 0,
                "chart": {"type": "table_only", "x": "", "y": ""},
                "sql_used": None, "notices": ["Segment B out of scope (v1 is core-portfolio-only)."]}

    def _psi_deferred(self, q, ctx, reveal, t0):
        mid, mname = detect_model(q)
        mid = mid or CHAMPION
        sql = ("SELECT period, period_start, metric, value FROM core.performance_metrics "
               f"WHERE model_id = '{mid}' AND dataset_norm = 'oot_monthly' "
               "AND metric IN ('AUC_ROC','KS') ORDER BY period_start, metric LIMIT 100")
        cols, rows = self.db.run(sql)
        notice = ("Distribution-drift monitoring (PSI/KL) is not yet computed in v1 — it is a "
                  "backlog item over the enriched OOT extracts. Showing the available "
                  "performance-drift trend instead.")
        summary = self._summarize(
            q, ctx,
            cols, rows,
            extra=f"IMPORTANT: PSI/distribution drift is NOT computed in v1. Open by saying so, "
                  f"then present this performance trend (AUC_ROC and KS across the OOT months) "
                  f"for {mid} as the available alternative.")
        self._audit(q, None, None, sql, len(rows), reveal, "psi_deferred", None, t0)
        self.db.history(q, summary)
        return {"summary": summary, "columns": cols, "rows": rows, "row_count": len(rows),
                "chart": {"type": "line", "x": "period", "y": "value"},
                "sql_used": sql, "notices": [notice]}

    # ------------------------------------------------------------ consent
    def _consent(self, q, reveal, t0):
        """Deterministic: the consent verdict is computed from the queried dates in code,
        so a weak summarizer can never contradict the data."""
        client_id = CLIENT_RE.search(q).group(1).upper()
        purposes = [r[0] for r in self.db.con.execute(
            "SELECT DISTINCT purpose FROM privacy.consent_log").fetchall()]
        asked = [p for p in purposes if p and p.lower() in q.lower()]
        filt, params = "client_id = ?", [client_id]
        if asked:
            filt += " AND purpose IN (" + ",".join("?" for _ in asked) + ")"
            params += asked
        sql = ("SELECT client_id, purpose, granted_date, granted_channel, expiry_date, "
               "withdrawn_date, withdrawn_channel, consent_state "
               f"FROM privacy.consent_status WHERE {filt} ORDER BY purpose, granted_date")
        cols = ["client_id", "purpose", "granted_date", "granted_channel", "expiry_date",
                "withdrawn_date", "withdrawn_channel", "consent_state"]
        rows = [list(r) for r in self.db.con.execute(sql, params).fetchall()]
        today = self.db.con.execute("SELECT current_date").fetchone()[0]
        if not rows:
            scope = f" for purpose(s) {', '.join(asked)}" if asked else ""
            summary = (f"No consent records exist for client {client_id}{scope} — "
                       f"so there is no active consent on record (as of {today}).")
        else:
            lines = []
            for r in rows:
                d = dict(zip(cols, r))
                p = d["purpose"]
                if d["consent_state"] == "active":
                    lines.append(f"{p}: ACTIVE — granted {d['granted_date']} via "
                                 f"{d['granted_channel']}, valid until {d['expiry_date']}.")
                elif d["consent_state"] == "withdrawn":
                    lines.append(f"{p}: NOT active — withdrawn on {d['withdrawn_date']} via "
                                 f"{d['withdrawn_channel']} (originally granted "
                                 f"{d['granted_date']}, expiry {d['expiry_date']}).")
                elif d["consent_state"] == "expired":
                    lines.append(f"{p}: NOT active — expired on {d['expiry_date']} "
                                 f"(granted {d['granted_date']}, never withdrawn).")
                else:
                    lines.append(f"{p}: status unknown — dates incomplete in the consent log.")
            summary = f"Consent status for client {client_id} as of {today}:\n" + "\n".join(lines)
        self._audit(q, None, None, sql, len(rows), reveal, "consent_deterministic", None, t0)
        self.db.history(q, summary)
        return {"summary": summary, "columns": cols, "rows": rows, "row_count": len(rows),
                "chart": {"type": "table_only", "x": "", "y": ""},
                "sql_used": sql, "notices": []}

    # ------------------------------------------------- which model is best
    def _model_tradeoff(self, q, reveal, t0):
        """Deterministic trade-off answer built from queried scorecard + fairness numbers —
        never a single winner."""
        sql = ("""
            SELECT m.model_id, m.model_name,
                   max(CASE WHEN p.dataset_norm='train' AND p.metric='AUC_ROC' THEN p.value END) AS train_auc,
                   max(CASE WHEN p.dataset_norm='validation' AND p.metric='AUC_ROC' THEN p.value END) AS validation_auc,
                   max(CASE WHEN f.dataset='Validation' AND f.source='global'
                            AND f.metric='statistical_parity_pct' THEN f.value END) AS validation_stat_parity_pct,
                   max(CASE WHEN m.is_champion THEN 1 ELSE 0 END) = 1 AS is_champion
            FROM core.models m
            LEFT JOIN core.performance_metrics p ON p.model_id = m.model_id
            LEFT JOIN core.fairness_metrics f ON f.model_id = m.model_id
            GROUP BY m.model_id, m.model_name ORDER BY m.model_id
        """).strip()
        cols, rows = self.db.run(sql)
        by_id = {r[0]: dict(zip(cols, r)) for r in rows}

        def fmt(mid, extra=""):
            d = by_id[mid]
            sp = (f"{d['validation_stat_parity_pct']:.1f}%"
                  if d["validation_stat_parity_pct"] is not None else "not computed")
            return (f"- {d['model_name']} ({mid}): Train AUC {d['train_auc']}, Validation AUC "
                    f"{d['validation_auc']}, validation statistical parity {sp}.{extra}")

        summary = "\n".join([
            "There is no single best model — it is a trade-off (all numbers queried from the scorecards and fairness summaries):",
            fmt("m2_xgboost", " Higher AUC but badly overfit (Train 1.0 vs Validation ~0.69) with poor fairness."),
            fmt("m3_rf", " Similar picture: overfit on train, weak statistical parity."),
            fmt("m4_autoencoder", " Near-random discriminative power — but by far the fairest, and the only model with per-client local explanations. That is why it is the champion/default for governance use."),
            "Pick XGBoost/RF if raw ranking power matters most (accepting overfit + fairness risk); the Autoencoder is the champion for explainability and fairness, and its weak AUC must be stated, not hidden.",
        ])
        self._audit(q, None, None, sql, len(rows), reveal, "model_tradeoff", None, t0)
        self.db.history(q, summary)
        return {"summary": summary, "columns": cols, "rows": rows, "row_count": len(rows),
                "chart": {"type": "bar", "x": "model_name", "y": "validation_auc"},
                "sql_used": sql, "notices": []}

    # ------------------------------------------------------ local case audit
    def _case_audit(self, q, ctx, reveal, t0):
        client_id = CLIENT_RE.search(q).group(1).upper()
        notices = []
        mid, mname = detect_model(q)
        if mid in ("m2_xgboost", "m3_rf"):
            notices.append(f"Local explanations exist only for the Autoencoder; {mname} has none. "
                           "Showing the Autoencoder's explanation.")

        ident = self.db.con.execute(
            "SELECT name, segment, activeness_segment, group_status "
            "FROM privacy.customer_master WHERE client_id = ?", [client_id]).fetchall()
        if not ident:
            msg = (f"I couldn't find client {client_id} in the customer master, so there is "
                   "nothing to explain. Check the ID format (CUST_XXXXX).")
            self._audit(q, None, None, None, 0, reveal, "case_audit_unknown_client", None, t0)
            return {"summary": msg, "columns": [], "rows": [], "row_count": 0,
                    "chart": {"type": "table_only", "x": "", "y": ""},
                    "sql_used": None, "notices": notices}
        name, segment, activeness, group_status = ident[0]
        shown_name = str(name) if reveal else mask_value("name", name)
        identity = (f"Client {client_id} ({shown_name}) — segment {segment}, "
                    f"activeness {activeness}, group status {group_status}.")

        # Per-client explanation: one sample row (oversampled disputes can map to
        # several identical sample rows — take the first).
        sql_local = (
            "SELECT feature, contribution, score_m4_autoencoder, decile "
            "FROM core.local_explanations "
            "WHERE client_id = ? AND dataset = 'dev' "
            "AND row_id = (SELECT min(row_id) FROM core.local_explanations "
            "              WHERE client_id = ? AND dataset = 'dev') "
            "ORDER BY abs(contribution) DESC LIMIT 10")
        rows = self.db.con.execute(sql_local, [client_id, client_id]).fetchall()
        if rows:
            cols = ["feature", "contribution", "score_m4_autoencoder", "decile"]
            rows = [list(r) for r in rows]
            glossed = self._gloss([r[0] for r in rows])
            summary = self._summarize(
                q, ctx, cols, rows,
                extra=f"{identity}\nThese are the client's own top Autoencoder surrogate "
                      f"contributions (per-client explanation). Feature meanings: {glossed}\n"
                      "Explain the top drivers in plain language. Do not overstate model quality.")
            level = "client"
            shown_sql = sql_local.replace("?", f"'{client_id}'")
        else:
            # Honest fallback: cohort-level drivers for the client's segment/decile.
            case = self.db.con.execute(
                "SELECT decile_m4, score_m4_autoencoder FROM core.cases "
                "WHERE client_id = ? LIMIT 1", [client_id]).fetchall()
            if case:
                decile = case[0][0]
                cohort_filter, cohort_desc = (
                    "segment = ? AND decile = ?",
                    f"segment {segment}, autoencoder risk decile {decile} of 10")
                params = [segment, decile]
            else:
                notices.append(f"{client_id} is not in the scored dev population; cohort is "
                               "segment-level only.")
                cohort_filter, cohort_desc = ("segment = ?", f"segment {segment}")
                params = [segment]
            sql_cohort = (
                "SELECT feature, avg(contribution) AS avg_contribution, count(*) AS sample_rows "
                f"FROM core.local_explanations WHERE dataset = 'dev' AND {cohort_filter} "
                "GROUP BY feature ORDER BY abs(avg(contribution)) DESC LIMIT 10")
            rows = self.db.con.execute(sql_cohort, params).fetchall()
            cols = ["feature", "avg_contribution", "sample_rows"]
            rows = [list(r) for r in rows]
            notices.append("No per-client explanation row exists for this client (the local "
                           "explanation set is a 20,000-row sample). Showing COHORT-LEVEL "
                           f"drivers for {cohort_desc} — not a per-client explanation.")
            glossed = self._gloss([r[0] for r in rows]) if rows else ""
            summary = self._summarize(
                q, ctx, cols, rows,
                extra=f"{identity}\nIMPORTANT: no per-client explanation exists for this client. "
                      f"These are COHORT-LEVEL average contributions for {cohort_desc}. State this "
                      f"clearly and label everything cohort-level, never per-client. "
                      f"Feature meanings: {glossed}")
            level = "cohort"
            shown_sql = sql_cohort
        self._audit(q, None, None, shown_sql, len(rows), reveal, f"case_audit_{level}", None, t0)
        self.db.history(q, summary)
        return {"summary": summary, "columns": cols, "rows": rows, "row_count": len(rows),
                "chart": {"type": "bar", "x": "feature",
                          "y": "contribution" if level == "client" else "avg_contribution"},
                "sql_used": shown_sql, "notices": notices, "identity": identity}

    def _gloss(self, features: list[str]) -> str:
        if not features:
            return ""
        ph = ",".join("?" for _ in features)
        rows = self.db.con.execute(
            f"SELECT variable_name, description FROM core.features WHERE variable_name IN ({ph})",
            features).fetchall()
        return "; ".join(f"{n}: {d}" for n, d in rows)

    # ------------------------------------------------------------- LLM path
    def _system_prompt(self, question: str) -> str:
        return (
            "You are Claris, a natural-language governance copilot over a model-risk database "
            "for in-scope dispute-risk models. You translate plain-English governance questions "
            "(explainability, fairness, case audits, consent, lineage, performance) into a "
            "SINGLE read-only DuckDB SQL query.\n\n"
            + HONESTY_RULES + "\n\n" + self.schema_doc + "\n\n"
            + self._select_examples(question) + "\n\n" + SQL_RULES)

    def _llm_path(self, q, ctx, reveal, t0):
        system = self._system_prompt(q)
        parts = [f"Question: {q}"]
        if ctx.get("role"):
            parts.append(f"Asking persona: {ctx['role']}")
        parts.append("Write one query against the documented schema. Use ONLY real tables/columns.")
        user_msg = "\n".join(parts)

        plan = self.provider.plan(system, user_msg)
        if plan.get("needs_clarification"):
            self._audit(q, plan.get("sql"), None, None, 0, reveal, "clarification", None, t0)
            return {"clarification": plan.get("clarification") or "Could you clarify what you'd like to see?"}

        generated_sql = plan.get("sql", "")
        ok, sql, repairs, err = self._validate_or_repair(generated_sql)
        if not ok:
            plan = self.provider.plan(system, user_msg, retry_error=err)
            if plan.get("needs_clarification"):
                self._audit(q, generated_sql, None, None, 0, reveal, "clarification", err, t0)
                return {"clarification": plan.get("clarification")}
            ok, sql, repairs, err = self._validate_or_repair(plan.get("sql", ""))
            if not ok:
                self._audit(q, generated_sql, None, None, 0, reveal, "failed", err, t0)
                return {"error": f"I couldn't build a valid query for that. ({err}) "
                                 "Try rephrasing — e.g. ask about top features, fairness by "
                                 "segment, performance trends, consent status, or a specific client."}

        final_sql = self.db.enforce_limit(sql)
        try:
            cols, rows = self.db.run(final_sql)
        except Exception as e:
            err = str(e).splitlines()[0]
            self._audit(q, generated_sql, sql if repairs else None, final_sql, 0, reveal, "error", err, t0)
            return {"error": f"The query ran into an error: {err}. Try rephrasing your question."}

        rows, masked_cols = (rows, []) if reveal else mask_results(cols, rows)
        summary = self._summarize(q, ctx, cols, rows)
        self._audit(q, generated_sql, sql if repairs else None, final_sql,
                    len(rows), reveal, "ok", None, t0)
        self.db.history(q, summary)
        notices = []
        if repairs:
            notices.append("Auto-repaired identifiers: " + "; ".join(repairs))
        if masked_cols:
            notices.append("PII masked: " + ", ".join(masked_cols))
        return {"summary": summary, "columns": cols, "rows": rows, "row_count": len(rows),
                "chart": plan.get("chart", {"type": "table_only", "x": "", "y": ""}),
                "sql_used": final_sql, "columns_used": plan.get("columns_used", []),
                "notices": notices, "repairs": repairs}

    def _validate_or_repair(self, sql: str):
        """(ok, sql, repairs, err). Guard -> validate -> identifier auto-repair -> re-validate.
        Repair only substitutes identifiers, so the read-only guard is never weakened."""
        ok, err = self.db.guard(sql)
        if not ok:
            # a near-miss table name fails the allowlist; try repair before giving up
            fixed, rok, repairs = self.db.repair(sql)
            if rok:
                gok, gerr = self.db.guard(fixed)
                if gok:
                    return True, fixed, repairs, None
            return False, sql, [], err
        vok, verr = self.db.validate(sql)
        if vok:
            return True, sql, [], None
        fixed, rok, repairs = self.db.repair(sql)
        if rok:
            gok, _ = self.db.guard(fixed)
            vok2, verr2 = self.db.validate(fixed)
            if gok and vok2:
                return True, fixed, repairs, None
        return False, sql, [], verr

    # ------------------------------------------------------------- summaries
    def _summarize(self, question, ctx, cols, rows, extra: str = "") -> str:
        if not rows:
            return ("The query returned no rows. That usually means no records match — "
                    "I won't guess at numbers. Try widening the question.")
        detail = ctx.get("detailLevel", "Concise")
        length = ("Answer in 2-4 short sentences." if detail == "Concise"
                  else "Answer in a short paragraph (4-7 sentences) with the key figures.")
        # small local models: keep the grounding blob inside a ~4k-token context
        sample = rows[:25] if self.provider.name == "ollama" else rows[:60]
        data_blob = json.dumps({"columns": cols, "rows": sample,
                                "total_rows_returned": len(rows)}, default=str)
        prompt = (
            f"User question: {question}\n"
            f"Persona: {ctx.get('role', 'Compliance officer')}. {length}\n"
            + (extra + "\n" if extra else "")
            + "Here are the ACTUAL query results. Use these numbers EXACTLY as written — copy "
              "verbatim; never multiply, add, scale, or transform. NULL means 'not computed'.\n"
            f"{data_blob}\n\n"
            "Write the answer as Claris. Plain text, no markdown headers."
        )
        return self.provider.summarize(SUMMARY_SYSTEM, prompt)

    def _audit(self, q, gen_sql, rep_sql, final_sql, n, reveal, status, error, t0):
        try:
            self.db.audit(q, gen_sql, rep_sql, final_sql, n, reveal, status, error,
                          int((time.time() - t0) * 1000))
        except Exception as e:
            print(f"[claris] audit write failed: {e}")
