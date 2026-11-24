# Ask Claris — build notes

Natural-language governance copilot over a model-risk database for a set of
**dispute-risk models**. A compliance officer, data scientist, or business user asks a plain-English
question — explainability, fairness, case audits, consent, performance — and gets a grounded
answer: **LLM → SQL → real query → answer card + chart + table**. Every number on screen comes
from an executed query. Never fabricated.

This is the rebuild over the **curated pipeline outputs** (Part_A / XAI / RAI), not the earlier
raw-CSV prototype (`../ask-claris`, throwaway). Carried over from the prototype: the SELECT-only
SQL guard, the identifier auto-repair, and the retry-then-honest-failure pattern.

## Layout

```
ask-claris-v2/
  etl/build_db.py     idempotent ETL -> data/claris.duckdb (+ data/load_report.txt)
  data/claris.duckdb  the governance database (DuckDB; chosen for single-file deploys,
                      fast ~100MB CSV ingest and native UNPIVOT)
  backend/            FastAPI: /ask /meta /health  (port 8000)
  frontend/           React + Vite + Tailwind + Recharts (port 5173, proxies /api -> :8000)
```

## Setup

```powershell
python -m venv .venv
.venv\Scripts\pip install -r backend\requirements.txt

# 1. Build the database (validates headers + row counts, prints a load report)
.venv\Scripts\python etl\build_db.py

# 2. Backend
cd backend
copy .env.example .env     # set ANTHROPIC_API_KEY (or switch LLM_PROVIDER to groq/ollama)
..\.venv\Scripts\python -m uvicorn app:app --port 8000

# 3. Frontend
cd ..\frontend
npm install
npm run dev                # http://localhost:5173
```

Note: DuckDB is single-writer — stop the backend before re-running the ETL.

## Database (schemas)

- **core** — `models`/`model_versions` (champion = `m4_autoencoder`), `features` (277-entry
  semantic glossary), `feature_importance_global` (ONE method per model: gain / **mda** / 
  recon_error_gap — the RF file ships `Importance_MDA`, not gain as the spec table said),
  `cases` (93,480 scored dev rows; disputes oversampled 10×), `client_features` (canonical
  Data.csv), `local_explanations` (10.84M long-format rows, Autoencoder only),
  `fairness_metrics` (long; NULL = "not computed", never imputed), `disparate_impact`
  (+ `is_alert`; currently zero alerts), `performance_metrics` (long, with `period`/
  `period_start` for the OOT months), `rai_narratives`, `rai_documents`.
- **privacy** — `customer_master` (PII), `consent_log` + `consent_status` view (active /
  expired / withdrawn as of query date), `segment_master`, `activeness_segment_definitions`.
- **lineage** — `data_splits`, `feature_lineage` (from the variable dictionary),
  `data_quality` (ETL checks).
- **ops** — `query_history`, `query_audit` (every NLQ: question, generated/repaired/final SQL,
  rows, PII-reveal flag, status, latency). Survives ETL rebuilds.
- **vec** — `nlq_training`: the seed questions + gold SQL, injected as worked examples.

### Client_ID recovery (local explanations)

The surrogate contrib files have **no Client_ID** (20k-row samples). At ETL time, dev-sample
rows are matched against `dev_scored` ⨝ `customer_master` on
`(round(score_m4_autoencoder, 9), target, Segment, Activeness_Segment)`; only keys mapping to
exactly one client are accepted. **Recovery: 97.1% of dev rows.** The Validation_OOS sample has
no scored source file (dev_scored covers the dev population only), so recovery is not attempted
there — any key "match" would be coincidence; those rows keep `client_id = NULL`.

## NLQ behaviour

- **Guard**: single read-only SELECT/CTE, forbidden-keyword check, table allowlist
  (core/privacy/lineage/vec only — ops is not queryable), enforced LIMIT 1000.
- **Auto-repair**: near-miss column *and* table identifiers are snapped to real ones and
  re-validated. Repair substitutes identifiers only — it can never weaken the guard. Repairs
  are surfaced in the answer and logged to `ops.query_audit`.
- **Honest failure**: validate → repair → one LLM retry → "I couldn't build a valid query for
  that" — never a wrong number. Empty results are reported as empty, never guessed.
- **Champion default**: unnamed-model questions go to `m4_autoencoder`; all three models stay
  queryable. Claris is instructed to never present the champion as high-performing (Validation
  AUC ≈ 0.53) and to answer "which model is best" as a trade-off.
- **PII masking**: name/email/national_id/phone/address/DOB are masked in every answer by
  default; the UI "Reveal PII" toggle sends `revealPii: true`, which is logged to the audit.
- **Scope refusals** (deterministic, pre-LLM):
  - Segment B questions → clear core-portfolio-only scope message.
  - PSI / distribution drift → honest "not computed in v1" + the performance-drift trend
    (AUC/KS across Internal OOT → Oct/Nov/Dec 2025) offered instead.
- **Case audits** (`why was CUST_XXXXX flagged?`): per-client top contributions when a
  recovered explanation row exists; otherwise an honest **cohort-level** fallback
  (segment × autoencoder risk decile), clearly labeled, never presented as per-client.
  XGBoost/RF local-explanation requests get a notice that only the Autoencoder has them.

## Acceptance tests

`backend/_acceptance.py` runs the 10 launch questions end-to-end against the live backend
(test 2 and 3 each have two variants). Run with the server up:

```powershell
.venv\Scripts\python backend\_acceptance.py
```

Offline checks (guard / repair / masking / gold-SQL validation, no LLM): `backend/_smoke.py`.

## v2 backlog

- PSI/KL distribution-drift job over `Part_A/Modeling_Data_Enriched/oot_2025*.csv`.
- Segment B (blocked on Segment B model/XAI/RAI artifacts).
- Embedding-based retrieval over `vec.nlq_training` (today all seeds fit in the prompt).
