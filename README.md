# Ask Claris

**An AI chatbot for ML model governance.** A compliance officer, data scientist or business user asks it questions in plain English about a set of machine-learning risk models, for example *"Why was this customer flagged?"*, *"Is the model fair across segments?"* or *"How did performance change month by month?"*, and gets a grounded answer card, chart and table.

**Every number on screen comes from an executed SQL query. The LLM never makes up a number.**

> Built in 2026 as a test project for a client in AI governance. The client's data, name and identifiers have been removed from this repository. The code, architecture and checks are unchanged.

---

## The problem

A modelling team had trained three dispute-risk models (XGBoost, Random Forest and an Autoencoder) and produced a large set of governance outputs: scored cases, customer and consent records, global feature importance, fairness and disparate-impact reports, performance scorecards, risk narratives and per-case explanation files.

The people who have to sign off on these models (risk, compliance, business owners) could not query any of this without a data scientist. And a normal LLM chatbot is not acceptable here, because in regulated work every figure must be traceable to real data.

## What I built

```
 ~20 pipeline output files (CSV / XLSX)
            │
            ▼
 etl/build_db.py  ── idempotent ETL, 16 automated quality checks
            │
            ▼
 claris.duckdb  (5 schemas: core · privacy · lineage · ops · vec, ~10.8M rows)
            │
            ▼
 backend/ (FastAPI)  question → LLM writes SQL → guard → auto-repair → run → mask PII → audit
            │
            ▼
 frontend/ (React + Vite + Tailwind + Recharts)  answer card + chart + table
```

### 1. Data pipeline (`etl/build_db.py`)

- Loads about 20 output files from the modelling pipeline into **one DuckDB database with 5 schemas**:

  | Schema | What it holds |
  |---|---|
  | `core` | models, features, scored cases, explanations, fairness, performance |
  | `privacy` | customer master (PII), consent log and a consent-status view, segments |
  | `lineage` | data splits, feature lineage, data-quality results |
  | `ops` | query history and a full audit log |
  | `vec` | seed questions with correct SQL, used as worked examples |

- **Idempotent.** Every run drops and rebuilds the tables, so rerunning always gives the same database with no duplicates. The `ops` audit tables are kept across rebuilds.
- **10.84 million-row explanation table.** The per-case explanation files came in wide format. I reshaped them with DuckDB's `UNPIVOT` into one row per case per feature.
- **16 automated data-quality checks** on every load, including row counts that must match the source files exactly, zero duplicate customer IDs, no cases missing from the customer master, and no missing IDs in the consent log. Results go to `lineage.data_quality` and a load report ([docs/load_report.txt](docs/load_report.txt)).
- **ID recovery.** The explanation files had no customer ID. I matched rows back to customers on score, target and segment, and accepted only matches that were unique. That recovered **97.1%** of the training-set rows. Where no safe match existed, the ID stays empty instead of being guessed.

### 2. Question engine (`backend/`)

- **The LLM writes SQL; code runs it.** The model returns structured JSON (SQL, chart type, columns used, or a clarifying question).
- **SQL guard.** Only a single read-only `SELECT`, from an allowlist of schemas, with a forbidden-keyword check and a 1,000-row limit.
- **Auto-repair.** Near-miss table or column names are snapped to real ones and checked again. Repair only changes names, so it can never weaken the guard. Every repair is shown to the user and logged.
- **Honest failure.** Validate, then repair, then one LLM retry, then *"I couldn't build a valid query for that."* Empty results are reported as empty, never guessed.
- **PII masking by default.** Name, email, ID number, phone, address and date of birth are masked in every answer. Revealing them needs an explicit toggle, and every reveal is written to the audit log.
- **Scope refusals before the LLM.** Out-of-scope questions get a clear, rule-based reply without an LLM call.
- **Full audit trail.** Every question is logged with the generated, repaired and final SQL, row count, PII-reveal flag, status and latency.
- **Pluggable LLM provider.** Anthropic, Groq or a local Ollama model, switched by one setting.

### 3. Testing

- `backend/_acceptance.py`: 10 launch questions end to end against the live backend (two with a second variant, 12 tests in total).
- `backend/_smoke.py`: offline checks of the SQL guard, auto-repair, PII masking and the gold SQL, with no LLM needed.

## Tech stack

Python · DuckDB · FastAPI · Pydantic · Anthropic / Groq / Ollama · React · TypeScript · Vite · Tailwind · Recharts

## Running it

The original client data is not included. The ETL expects the same file layout as the modelling pipeline (see `EXPECTED_LINES` and `REQUIRED_HEADERS` in `etl/build_db.py`).

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r backend/requirements.txt

python etl/build_db.py --resources <path-to-pipeline-outputs>   # builds data/claris.duckdb

cd backend && cp .env.example .env    # add your LLM key
uvicorn app:app --port 8000

cd ../frontend && npm install && npm run dev    # http://localhost:5173
```

## Design notes

- **Why DuckDB:** a single file that is easy to deploy, fast CSV ingest for the ~100 MB sources, and native `UNPIVOT` for reshaping.
- **Why rebuild instead of update:** a full rebuild is the simplest way to stay idempotent and avoid partial loads. At larger scale this would move to change data capture and a cloud warehouse, with the same checks on every load.
- **Why keep the LLM away from numbers:** in regulated work a figure must be repeatable and traceable to data. The LLM only turns the question into SQL and the result into words.

More detail: [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) and [docs/BUILD_NOTES.md](docs/BUILD_NOTES.md).

---

Built by [Manisha Sarkar](https://www.linkedin.com/in/manishasarkar-devops).
