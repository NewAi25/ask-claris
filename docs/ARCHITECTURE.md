# Ask Claris — Architecture & Schema Reference

Ask Claris is a natural-language governance copilot over a model-risk database for the
**in-scope dispute-risk models**. A compliance officer, data scientist, or business user asks a
plain-English question; the system turns it into a real, guarded SQL query against a curated
DuckDB database and answers with a grounded summary + chart + table. **Every number on screen
comes from an executed query — nothing is fabricated.**

```
question ──► FastAPI /ask ──► intent routing ──► LLM plan (SQL + chart spec)
                                   │                    │
                deterministic handlers          SELECT-only guard + table allowlist
            (Segment B refusal, PSI deferral,                 │
             per-client case audit)             identifier auto-repair (cols + tables)
                                   │                    │
                                   └────────► DuckDB execute (LIMIT-capped)
                                                        │
                                              PII masking (default ON)
                                                        │
                                   grounded summary (LLM, numbers verbatim)
                                                        │
                                   answer card + chart + table  +  ops audit row
```

---

## 1. Repository layout

```
ask-claris-v2/
├── etl/build_db.py        one idempotent script: sources → data/claris.duckdb + load report
├── data/claris.duckdb     the governance database (DuckDB, single file)
├── data/load_report.txt   rows per table + quality checks from the last build
├── backend/               FastAPI app (Python, .venv at repo root)
│   ├── app.py             HTTP endpoints: POST /ask, GET /meta, GET /health
│   ├── nlq.py             the NLQ engine: routing, prompts, honesty rules, fallbacks
│   ├── db.py              DuckDB access: guard, allowlist, auto-repair, limits, audit
│   ├── providers.py       pluggable LLM: anthropic | groq | ollama (.env switch)
│   ├── schema_doc.py      schema documentation generated live from the DB for the prompt
│   ├── masking.py         PII maskers applied to every result by default
│   ├── _smoke.py          offline tests (guard/repair/masking/seeds) — no LLM needed
│   └── _acceptance.py     the 12-check end-to-end acceptance suite (needs running server)
└── frontend/              React + Vite + Tailwind + Recharts (port 5173, proxies /api → :8000)
```

Runtime topology: **frontend :5173** → Vite proxy `/api/*` → **backend :8000** → **claris.duckdb**.
DuckDB is single-writer: stop the backend before re-running the ETL, and never run two backends.

---

## 2. ETL (etl/build_db.py)

Reads the curated pipeline outputs under `Resources/` (never the raw root CSVs), validates
every header against expectations, builds the DB, and prints/saves a load report. Idempotent:
re-running drops and recreates everything **except `ops.*`** (query history/audit survive).

Source → table mapping:

| Source file | Target |
|---|---|
| `Part_A/Data/Data.csv` (150,339 clients) | `core.client_features` |
| `XAI/Client_Input/Common/dev_scored.csv` (93,480 rows) | `core.cases` |
| `Part_A/Data/variable_dictionary.csv` (277 vars) | `core.features` + `lineage.feature_lineage` |
| 3× `model_config.json` | `core.models`, `core.model_versions` |
| 3× `performance_scorecard.csv` | `core.performance_metrics` (long) |
| 3× `feature_importance.csv` | `core.feature_importance_global` (one method per model) |
| `XAI/Outputs/surrogate_local/*.csv` (2× 20k rows) | `core.local_explanations` (long, 10.84M rows) |
| 4× RAI fairness/impact CSVs | `core.fairness_metrics` (long) + `core.disparate_impact` |
| `llm_rai_xai_consolidated.csv`, `business_user_summary.md` | `core.rai_narratives`, `core.rai_documents` |
| `customer_master.csv`, `consent_log.csv` | `privacy.customer_master`, `privacy.consent_log` |
| `Segment_Master.xlsx`, `segment_definitions.csv` | `privacy.segment_master`, `privacy.activeness_segment_definitions` |
| `split_summary.csv` | `lineage.data_splits` |

Key ETL decisions (verified against the data, not assumed):

- **Engine: DuckDB** — single file, fast ingest of ~100MB CSVs, native UNPIVOT for long formats.
- **RF importance is MDA, not gain** — the shipped file has `Importance_MDA`; method codes are
  `gain` (m2), `mda` (m3), `recon_error_gap` (m4). One method per model; no consensus questions.
- **Row counts**: the build prompt's counts for 3 files were physical line counts *including*
  header. The ETL asserts both the line count and the loaded data rows (16 checks, all green).
- **Oversampling**: dev disputes appear 10× (205 clients → 2,050 rows), matching split_summary.
  Asserted: non-dispute client_ids unique; distinct clients = 91,635.
- **Client_ID recovery** for local explanations (source files have NO Client_ID): join on
  `(round(score_m4,9), target, segment, activeness_segment)` against scored dev population;
  only keys mapping to exactly one client are accepted → **97.1% recovered** for the dev sample.
  The Validation_OOS sample has no scored source file, so recovery is not attempted (all NULL).
- **Blanks stay NULL** in fairness metrics — reported as "not computed", never 0/100.

---

## 3. Database schema (data/claris.duckdb)

Five schemas: `core` (model governance), `privacy` (PII + consent), `lineage` (provenance),
`ops` (app-written telemetry, not NLQ-queryable), `vec` (NLQ training seeds).

### core

**core.models** — the 3 models; champion flag drives the default.
`model_id` (m2_xgboost | m3_rf | m4_autoencoder), `model_name`, `algorithm`,
`is_champion` (true only for m4_autoencoder), `champion_rationale`.

**core.model_versions** — `model_id`, `version`, `n_features` (271), `config_json`
(full hyperparameters/architecture), `training_note`.

**core.features** — semantic glossary, 277 rows.
`variable_name`, `var_group`, `sub_group`, `time_horizon`, `variable_type`, `data_type`,
`is_target`, `description`, `calculation_logic`, `source_variables`.

**core.feature_importance_global** — 813 rows (271 × 3 models).
`model_id`, `feature`, `method` (gain | mda | recon_error_gap), `value`, `rank` (1 = top),
`normal_recon_error`, `dispute_recon_error` (m4 only; value = dispute − normal gap).

**core.cases** — scored dev population, 93,480 rows.
`client`, `client_id`, `target` (0/1; 1s oversampled 10×), `score_m2_xgboost`, `score_m3_rf`,
`score_m4_autoencoder`, `pred_m2_xgboost`, `pred_m3_rf`, `pred_m4_autoencoder`,
`segment`, `activeness_segment`, `group_status`, `decile_m4` (1–10 by AE score; 10 = riskiest).

**core.client_features** — canonical raw dataset, 150,339 rows, 186 cols.
`client`, `client_id`, 180 raw feature columns (named exactly like `core.features.variable_name`,
rolling windows end `_l_NNNd`), `dispute` ('D'/'N'), `snapshot_date`, `segment`, `activeness_segment`.

**core.local_explanations** — AUTOENCODER ONLY, long format, 10,840,000 rows.
`row_id`, `dataset` ('dev' | 'validation_oos'; 20k sample rows each), `client_id`
(recovered for ~97% of dev, NULL otherwise), `segment`, `activeness_segment`, `target`,
`score_m4_autoencoder`, `decile` (10 = riskiest), `feature`, `contribution`.

**core.fairness_metrics** — long format, 225 rows (65 NULL values preserved).
`dataset` (Dev | Validation | Internal OOT), `model_id`, `model_name`, `segment`
(NULL for global; Privileged / Privileged (Control) / Protected / Vulnerable for per-segment),
`metric` (statistical_parity_pct, equal_opportunity_pct, predictive_parity_pct,
calibration_parity_pct, treatment_equality_pct, sample_size, disparate_impact_ratio),
`value` (% where 100 = perfect parity; NULL = not computed), `status`, `source`
(global | segment | disparate_impact).

**core.disparate_impact** — 36 rows, detail behind the impact-ratio entries.
`dataset`, `model_name`, `segment`, `group_status`, `approval_rate`, `baseline_rate`,
`impact_ratio`, `status`, `is_alert` (alerts file was empty ⇒ all false — zero alerts is a fact).

**core.performance_metrics** — long format, 270 rows (15 metrics × 6 datasets × 3 models).
`model_id`, `dataset_label` (raw scorecard label), `dataset_norm`
(train | validation | internal_oot | oot_monthly), `period` (Aug-Sep | Oct | Nov | Dec 2025),
`period_start` (DATE, for ordering), `metric` (AUC_ROC, AUC_PR, Gini, KS, Top_Decile_Recall,
Best_Threshold, Precision, Recall, F1, TP, FP, TN, FN, Rows, Disputes), `value`.
This table powers the performance-drift category.

**core.rai_narratives** — 30 pre-written persona narratives.
`dataset`, `model_name`, `model_id`, `persona`, `narrative`.

**core.rai_documents** — `doc_name` ('business_user_summary'), `content` (full markdown).

### privacy

**privacy.customer_master** — 150,339 rows, contains PII (masked downstream by default).
`client_id`, `name`, `email`, `national_id`, `phone_no`, `address`, `zip_code`,
`date_of_birth`, `gender`, `onboarding_date`, `segment` (nickname, e.g. 'Sages'),
`activeness_segment`, `group_status` (Privileged/Protected/…), `segment_name`,
`demographic_profile`, `socio_economic_definition`, `behavioral_traits`, `composition_details`.

**privacy.consent_log** — 429,212 rows, multiple purposes per client.
`client_id`, `purpose`, `granted_date`, `granted_channel`, `expiry_date`,
`withdrawn_date`, `withdrawn_channel`, `last_audit_date`.

**privacy.consent_status** — VIEW over consent_log adding `consent_state`
(active | expired | withdrawn | unknown) **computed against current_date at query time**.
This is the canonical way to answer consent questions.

**privacy.segment_master** — the 10 customer segments (from Segment_Master.xlsx).
`segment`, `group_status`, `segment_name`, `demographic_profile`,
`socio_economic_definition`, `behavioral_traits`, `composition_details` (RAI testing context).

**privacy.activeness_segment_definitions** — the 6 activity tiers (Dormant → Power User).
`activeness_segment`, `tier`, `quantile_upper`, `trading_days_threshold`, `description`.

### lineage

**lineage.data_splits** — 6 rows from split_summary.
`file`, `original_rows`, `new_rows`, `original_disputes`, `new_disputes`, `min_date`, `max_date`.

**lineage.feature_lineage** — 533 edges from the variable dictionary.
`feature`, `source_variable`, `calculation_logic`.

**lineage.data_quality** — ETL quality checks from the latest build.
`table_name`, `check_name`, `value`, `status`.

### ops (app-written; NOT reachable from generated SQL)

**ops.query_audit** — one row per /ask call.
`id`, `ts`, `question`, `generated_sql` (LLM's first attempt), `repaired_sql` (if auto-repair
fired), `final_sql` (what actually ran), `rows_returned`, `pii_reveal` (every reveal is logged),
`status` (ok | refused_scope_fo | psi_deferred | case_audit_client | case_audit_cohort |
clarification | failed | error), `error`, `latency_ms`.

**ops.query_history** — `id`, `ts`, `question`, `answer_summary`.

### vec

**vec.nlq_training** — seed questions with validated gold SQL.
`question`, `category`, `gold_sql`, `chart_type`. Loaded at backend startup, merged with
code-side extras (`EXTRA_SEEDS` in nlq.py), and the 5 most lexically similar examples are
injected into each prompt (keeps the prompt inside small local-model context windows).

---

## 4. Backend request flow (backend/nlq.py)

`POST /ask {question, role, detailLevel, revealPii}` →

1. **Deterministic intent routing** (regex, before any LLM call):
   - **Segment B** → scope refusal (v1 is core-portfolio-only). No query, audited.
   - **PSI / distribution drift / KL** → honest "not computed in v1" + runs the
     performance-drift trend (AUC_ROC + KS by OOT month) for the named model (default champion)
     as the offered alternative. Audited as `psi_deferred`.
   - **`CUST_xxxxx` + why/flag/explain/driver** → the case-audit handler (below).
   - Everything else → the LLM path.

2. **Case-audit handler** (deterministic SQL, LLM only writes the prose):
   - Identity from `privacy.customer_master`, name masked unless revealPii.
   - If the client has a recovered row in `core.local_explanations` (dev) → top 10
     contributions by |contribution| = **per-client explanation**.
   - Else → **cohort fallback**: average contributions for the client's segment ×
     AE-risk decile (segment-only if the client isn't scored), explicitly labeled
     "COHORT-LEVEL … not a per-client explanation".
   - If XGBoost/RF local explanations are requested → states plainly they exist only
     for the Autoencoder.

3. **LLM path**: system prompt = role description + **honesty rules** + live schema doc
   (generated from the DB at startup; compact mode for small local models) + the 5 most
   relevant worked examples + SQL rules. The provider returns a JSON plan
   `{sql, chart{type,x,y}, needs_clarification, …}` (forced tool-use on Anthropic,
   JSON mode on Groq/Ollama).

4. **Guard** (db.py): single statement, must start SELECT/WITH, forbidden-keyword regex
   (INSERT/UPDATE/DELETE/DROP/CREATE/COPY/ATTACH/PRAGMA/SET/…), and every referenced table
   must be in the allowlist (`core`, `privacy`, `lineage`, `vec` — never `ops`).

5. **Validate + auto-repair**: `EXPLAIN` binds the query without scanning. On unknown
   identifiers, repair snaps near-miss **column and table** names to the closest real ones
   (binder candidate bindings first, then normalized fuzzy match), re-validating each step,
   max 6 iterations. Repair only substitutes identifiers — it can never weaken the guard.
   If still invalid → one LLM re-prompt with the error → repair again → otherwise an honest
   "couldn't build that" error. Never a wrong number.

6. **Execute** with an enforced `LIMIT 1000` wrapper if the query has no LIMIT.

7. **PII masking** (masking.py): result columns named name/email/national_id/phone/address/
   date_of_birth are masked by default (`E*** S***`, `em***@mail.com`, `***-**-3286`,
   `1987-**-**`). `revealPii=true` returns full values **and is logged to ops.query_audit**.

8. **Grounded summary**: the LLM is given the actual result rows and instructed to copy
   numbers verbatim (never transform), treat NULL as "not computed", and obey the honesty
   rules. Empty results → "no rows matched; I won't guess".

9. **Audit + history** written to `ops.*`; response = `{summary, columns, rows, chart,
   sql_used, notices, repairs, row_count, identity?}`.

### Honesty rules (enforced in every prompt + deterministic handlers)

- Champion (m4_autoencoder) is the *default*, never presented as high-performing
  (Validation AUC ≈ 0.53). All three models stay queryable.
- "Which model is best?" → always the trade-off (XGBoost: higher AUC ~0.69 but overfit
  Train AUC 1.0 + ~20% statistical parity; AE: ~97% parity + only model with local
  explanations, but near-random AUC), computed deterministically from the scorecards.
- One importance method per model; no SHAP/consensus claims.
- NULL fairness = "not computed", never 0 or 100.
- Dispute oversampling → COUNT(DISTINCT client_id) for client counts.

### Provider layer (backend/providers.py)

`LLM_PROVIDER` in `backend/.env` selects anthropic | groq | ollama; model names are also
env-configured. `.env.example` is committed, secrets are not. The provider only ever
*proposes* SQL and writes prose — guard, repair, execution, and masking are provider-independent.

---

## 5. Frontend (frontend/)

React + Vite + Tailwind + Recharts. One page (AskClaris) + Dashboard.

- Role selector (Compliance Officer / Data Scientist / Business User) → role-tailored
  recommended questions (each backed by a gold SQL seed) and persona-aware summaries.
- Detail level (Concise/Detailed), **PII toggle** (default masked; reveal is visibly
  flagged "audited"), in-session history tab.
- Answer card: grounded summary, notices (scope refusals, cohort-level labels, auto-repair
  and masking disclosures), chart (bar/line/scatter/pie per the plan's chart spec, with
  sensible axis fallbacks), results table, and a collapsible "Query details" pane showing
  the exact SQL that ran.

---

## 6. Known limitations (v1, by design)

- **core portfolio only** — Segment B questions are refused with a scope message.
- **No PSI/distribution drift** — performance drift only; PSI over the enriched OOT
  extracts is a v2 backlog item.
- **Local explanations**: Autoencoder only; 20k-row samples; per-client coverage partial
  (97% of the dev sample linked; validation sample unlinkable) — hence the cohort fallback.
- **DuckDB single-writer**: one backend at a time; stop it before re-running the ETL.
- **Summary prose quality scales with the provider** — numbers are always grounded, but a
  small local model (qwen2.5-coder:1.5b) can phrase trends imprecisely or under-summarize.
  SQL generation is protected by seeds/rules/repair; high-stakes answers (consent state,
  model comparison, case audits, scope refusals) are deterministic code, not LLM judgment.
