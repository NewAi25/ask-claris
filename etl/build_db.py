"""Ask Claris ETL — builds data/claris.duckdb from the curated pipeline outputs.

Idempotent: drops and recreates everything except ops.* (query history/audit survive
rebuilds). Validates headers and row counts against the source files and prints a
load report. Engine choice: DuckDB (single file, fast CSV ingest of the ~100MB
sources, native UNPIVOT for the long-format tables).

Run:  python etl/build_db.py            (uses ../  layout: Resources/ask-claris-v2/etl)
      python etl/build_db.py --resources <path> --db <path>
"""
import argparse
import json
import os
import re
import sys

import duckdb
import openpyxl

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_RESOURCES = os.path.abspath(os.path.join(HERE, "..", ".."))
DEFAULT_DB = os.path.join(HERE, "..", "data", "claris.duckdb")

# ---------------------------------------------------------------- expectations
# The build-prompt counts for Data.csv / consent_log / customer_master are physical
# line counts INCLUDING the header (verified against the files); dev_scored's count
# excludes it. We assert both the physical line count and the loaded data rows.
EXPECTED_LINES = {  # physical lines in the file (header included)
    "Part_A/Data/Data.csv": 150_340,
    "XAI/Client_Input/Common/dev_scored.csv": 93_481,
    "Part_A/Data/consent_log.csv": 429_213,
    "Part_A/Data/customer_master.csv": 150_340,
}
EXPECTED_ROWS = {rel: n - 1 for rel, n in EXPECTED_LINES.items()}  # data rows

REQUIRED_HEADERS = {
    "Part_A/Data/Data.csv": ["Client", "Client_ID", "Dispute", "Date", "Segment", "Activeness_Segment"],
    "XAI/Client_Input/Common/dev_scored.csv": [
        "Client", "Client_ID", "target",
        "score_m2_xgboost", "score_m3_rf", "score_m4_autoencoder",
        "Pred_m2_xgboost", "Pred_m3_rf", "Pred_m4_autoencoder",
    ],
    "Part_A/Data/variable_dictionary.csv": [
        "Variable_Name", "Group", "Sub_Group", "Time_Horizon", "Variable_Type",
        "Data_Type", "Is_Target", "Description", "Calculation_Logic", "Source_Variables",
    ],
    "Part_A/Data/customer_master.csv": [
        "Client_ID", "Name", "Email", "National_ID", "PhoneNo", "Address", "ZipCode",
        "Date_Of_Birth", "Gender", "Customer_onboarding_Date", "Segment",
        "Activeness_Segment", "Status", "Segment Name",
    ],
    "Part_A/Data/consent_log.csv": [
        "Client_ID", "Purpose", "GrantedDate", "GrantedChannel", "ExpiryDate",
        "WithdrawnDate", "WithdrawnChannel", "LastAuditDate",
    ],
    "Part_A/Model_Outputs/Model2_XGBoost/feature_importance.csv": ["Feature", "Importance_Gain", "Rank"],
    # NOTE: spec said gain for m3 but the file actually ships MDA — adapt, don't invent.
    "Part_A/Model_Outputs/Model3_RandomForest/feature_importance.csv": ["Feature", "Importance_MDA", "Rank"],
    "Part_A/Model_Outputs/Model4_Autoencoder/feature_importance.csv": [
        "Feature", "Normal_Recon_Error", "Dispute_Recon_Error", "Gap (Dispute-Normal)", "Rank",
    ],
    "RAI/outputs/fairness_metrics_summary.csv": [
        "Evaluating Dataset", "Model", "Statistical Parity (%)", "Equal Opportunity (%)",
        "Predictive Parity (%)", "Calibration Parity (%)", "Treatment Equality (%)",
    ],
    "RAI/outputs/segment_fairness_metrics_summary.csv": [
        "Dataset", "Segment", "Model", "Sample Size", "Statistical Parity (%)",
        "Equal Opportunity (%)", "Predictive Parity (%)", "Calibration Parity (%)",
        "Treatment Equality (%)",
    ],
    "RAI/outputs/disparate_impact_summary.csv": [
        "Dataset", "Model", "Segment", "Group_Status", "Approval_Rate", "Baseline_Rate",
        "Impact_Ratio", "Status",
    ],
    "RAI/outputs/llm_rai_reports/llm_rai_xai_consolidated.csv": ["Dataset", "Model", "Persona", "Narrative"],
    "Part_A/Modeling_Data/split_summary.csv": [
        "File", "Original Rows", "New Rows", "Original Disputes", "New Disputes", "Min Date", "Max Date",
    ],
}

MODEL_DIRS = {
    "m2_xgboost": ("Model2_XGBoost", "XGBoost", "Gradient-boosted trees (XGBoost)"),
    "m3_rf": ("Model3_RandomForest", "Random Forest", "Random forest (SMOTE-balanced)"),
    "m4_autoencoder": ("Model4_Autoencoder", "Autoencoder", "Reconstruction-error autoencoder (trained on normal rows only)"),
}
IMPORTANCE_METHOD = {"m2_xgboost": "gain", "m3_rf": "mda", "m4_autoencoder": "recon_error_gap"}
RAI_MODEL_NAME_TO_ID = {"XGBoost": "m2_xgboost", "Random Forest": "m3_rf", "Autoencoder": "m4_autoencoder"}

CHAMPION = "m4_autoencoder"
CHAMPION_RATIONALE = (
    "Only model with per-row local explanations and the strongest, most stable fairness "
    "profile (~97% statistical parity on validation vs ~20% for XGBoost; XGBoost is also "
    "badly overfit: Train AUC 1.0 vs Validation 0.69). Caveat: its discriminative "
    "performance is weak (Validation AUC ~0.53) and must never be presented as high-performing."
)

report: list[str] = []


def log(msg: str):
    print(msg, flush=True)
    report.append(msg)


def csv_header(path: str) -> list[str]:
    import csv as _csv
    with open(path, encoding="utf-8-sig", newline="") as f:
        return next(_csv.reader(f))


def verify_headers(res: str):
    log("== Header verification ==")
    problems = []
    for rel, required in REQUIRED_HEADERS.items():
        path = os.path.join(res, rel)
        if not os.path.exists(path):
            problems.append(f"MISSING FILE: {rel}")
            continue
        have = csv_header(path)
        missing = [c for c in required if c not in have]
        if missing:
            problems.append(f"{rel}: missing required columns {missing}")
        else:
            log(f"  ok: {rel} ({len(have)} cols)")
    if problems:
        for p in problems:
            log("  FAIL " + p)
        sys.exit("Header verification failed — adapt the ETL, do not invent data.")


def p(res: str, rel: str) -> str:
    return os.path.join(res, rel).replace("\\", "/")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--resources", default=DEFAULT_RESOURCES)
    ap.add_argument("--db", default=os.path.abspath(DEFAULT_DB))
    args = ap.parse_args()
    res = args.resources

    verify_headers(res)

    os.makedirs(os.path.dirname(args.db), exist_ok=True)
    con = duckdb.connect(args.db)
    con.execute("SET preserve_insertion_order=false")

    for schema in ("core", "privacy", "lineage", "ops", "vec"):
        con.execute(f"CREATE SCHEMA IF NOT EXISTS {schema}")

    # ----------------------------------------------------------- core.models
    log("== core.models / core.model_versions ==")
    con.execute("DROP TABLE IF EXISTS core.models")
    con.execute("""CREATE TABLE core.models (
        model_id VARCHAR PRIMARY KEY, model_name VARCHAR, algorithm VARCHAR,
        is_champion BOOLEAN, champion_rationale VARCHAR)""")
    con.execute("DROP TABLE IF EXISTS core.model_versions")
    con.execute("""CREATE TABLE core.model_versions (
        model_id VARCHAR, version VARCHAR, n_features INTEGER,
        config_json VARCHAR, training_note VARCHAR)""")
    for mid, (mdir, name, algo) in MODEL_DIRS.items():
        cfg_path = os.path.join(res, "Part_A", "Model_Outputs", mdir, "model_config.json")
        cfg = json.load(open(cfg_path, encoding="utf-8"))
        con.execute("INSERT INTO core.models VALUES (?,?,?,?,?)",
                    [mid, name, algo, mid == CHAMPION, CHAMPION_RATIONALE if mid == CHAMPION else None])
        con.execute("INSERT INTO core.model_versions VALUES (?,?,?,?,?)",
                    [mid, "v1", cfg.get("n_features"), json.dumps(cfg),
                     cfg.get("training_note") or cfg.get("feature_set")])
    log(f"  models: 3 (champion={CHAMPION})")

    # --------------------------------------------------------- core.features
    log("== core.features (semantic glossary) ==")
    con.execute("DROP TABLE IF EXISTS core.features")
    con.execute(f"""
        CREATE TABLE core.features AS
        SELECT "Variable_Name" AS variable_name, "Group" AS var_group, "Sub_Group" AS sub_group,
               "Time_Horizon" AS time_horizon, "Variable_Type" AS variable_type,
               "Data_Type" AS data_type, "Is_Target" AS is_target,
               "Description" AS description, "Calculation_Logic" AS calculation_logic,
               "Source_Variables" AS source_variables
        FROM read_csv_auto('{p(res, "Part_A/Data/variable_dictionary.csv")}', sample_size=-1)
    """)
    n = con.execute("SELECT count(*) FROM core.features").fetchone()[0]
    log(f"  features: {n}")

    # --------------------------------------- core.feature_importance_global
    log("== core.feature_importance_global ==")
    con.execute("DROP TABLE IF EXISTS core.feature_importance_global")
    con.execute("""CREATE TABLE core.feature_importance_global (
        model_id VARCHAR, feature VARCHAR, method VARCHAR, value DOUBLE, rank INTEGER,
        normal_recon_error DOUBLE, dispute_recon_error DOUBLE)""")
    fi2 = p(res, "Part_A/Model_Outputs/Model2_XGBoost/feature_importance.csv")
    fi3 = p(res, "Part_A/Model_Outputs/Model3_RandomForest/feature_importance.csv")
    fi4 = p(res, "Part_A/Model_Outputs/Model4_Autoencoder/feature_importance.csv")
    con.execute(f"""
        INSERT INTO core.feature_importance_global
        SELECT 'm2_xgboost', Feature, 'gain', Importance_Gain, Rank, NULL, NULL
        FROM read_csv_auto('{fi2}', sample_size=-1)
    """)
    con.execute(f"""
        INSERT INTO core.feature_importance_global
        SELECT 'm3_rf', Feature, 'mda', Importance_MDA, Rank, NULL, NULL
        FROM read_csv_auto('{fi3}', sample_size=-1)
    """)
    con.execute(f"""
        INSERT INTO core.feature_importance_global
        SELECT 'm4_autoencoder', Feature, 'recon_error_gap', "Gap (Dispute-Normal)", Rank,
               Normal_Recon_Error, Dispute_Recon_Error
        FROM read_csv_auto('{fi4}', sample_size=-1)
    """)
    for mid in MODEL_DIRS:
        c = con.execute("SELECT count(*) FROM core.feature_importance_global WHERE model_id=?", [mid]).fetchone()[0]
        log(f"  {mid} ({IMPORTANCE_METHOD[mid]}): {c} features")

    # --------------------------------------------- privacy.customer_master
    log("== privacy.customer_master ==")
    con.execute("DROP TABLE IF EXISTS privacy.customer_master")
    con.execute(f"""
        CREATE TABLE privacy.customer_master AS
        SELECT "Client_ID" AS client_id, "Name" AS name, "Email" AS email,
               "National_ID" AS national_id, "PhoneNo" AS phone_no, "Address" AS address,
               "ZipCode" AS zip_code, "Date_Of_Birth" AS date_of_birth, "Gender" AS gender,
               "Customer_onboarding_Date" AS onboarding_date,
               "Segment" AS segment, "Activeness_Segment" AS activeness_segment,
               "Status" AS group_status, "Segment Name" AS segment_name,
               "Demographic Profile" AS demographic_profile,
               "Socio-Economic Definition" AS socio_economic_definition,
               "Behavioral Traits" AS behavioral_traits,
               "Composition Details (RAI Testing Context)" AS composition_details
        FROM read_csv_auto('{p(res, "Part_A/Data/customer_master.csv")}', sample_size=-1)
    """)

    # ------------------------------------------------- privacy.consent_log
    log("== privacy.consent_log ==")
    con.execute("DROP TABLE IF EXISTS privacy.consent_log")
    con.execute(f"""
        CREATE TABLE privacy.consent_log AS
        SELECT "Client_ID" AS client_id, "Purpose" AS purpose,
               TRY_CAST("GrantedDate" AS DATE) AS granted_date, "GrantedChannel" AS granted_channel,
               TRY_CAST("ExpiryDate" AS DATE) AS expiry_date,
               TRY_CAST("WithdrawnDate" AS DATE) AS withdrawn_date,
               "WithdrawnChannel" AS withdrawn_channel,
               TRY_CAST("LastAuditDate" AS DATE) AS last_audit_date
        FROM read_csv_auto('{p(res, "Part_A/Data/consent_log.csv")}', sample_size=-1, all_varchar=true)
    """)
    # Convenience view: consent state as of the query date.
    con.execute("DROP VIEW IF EXISTS privacy.consent_status")
    con.execute("""
        CREATE VIEW privacy.consent_status AS
        SELECT *,
            CASE
              WHEN withdrawn_date IS NOT NULL AND withdrawn_date <= current_date THEN 'withdrawn'
              WHEN expiry_date IS NOT NULL AND expiry_date < current_date THEN 'expired'
              WHEN granted_date IS NOT NULL AND granted_date <= current_date THEN 'active'
              ELSE 'unknown'
            END AS consent_state
        FROM privacy.consent_log
    """)

    # -------------------------------------------- privacy.segment_master(s)
    log("== privacy.segment_master / activeness_segment_definitions ==")
    con.execute("DROP TABLE IF EXISTS privacy.segment_master")
    con.execute("""CREATE TABLE privacy.segment_master (
        segment VARCHAR, group_status VARCHAR, segment_name VARCHAR,
        demographic_profile VARCHAR, socio_economic_definition VARCHAR,
        behavioral_traits VARCHAR, composition_details VARCHAR)""")
    wb = openpyxl.load_workbook(os.path.join(res, "Part_A", "Data", "Segment_Master.xlsx"))
    ws = wb.active
    rows = list(ws.iter_rows(min_row=2, values_only=True))
    con.executemany("INSERT INTO privacy.segment_master VALUES (?,?,?,?,?,?,?)",
                    [list(r[:7]) for r in rows if r[0]])
    con.execute("DROP TABLE IF EXISTS privacy.activeness_segment_definitions")
    con.execute(f"""
        CREATE TABLE privacy.activeness_segment_definitions AS
        SELECT "Segment_Label" AS activeness_segment, "Tier" AS tier,
               "Quantile_Upper" AS quantile_upper,
               "Trading_Days_Threshold" AS trading_days_threshold, "Description" AS description
        FROM read_csv_auto('{p(res, "Part_A/Data/segment_definitions.csv")}', sample_size=-1)
    """)
    log(f"  segments: {con.execute('SELECT count(*) FROM privacy.segment_master').fetchone()[0]} "
        f"/ activeness tiers: {con.execute('SELECT count(*) FROM privacy.activeness_segment_definitions').fetchone()[0]}")

    # ------------------------------------------------------------ core.cases
    log("== core.cases (scored population, dev) ==")
    con.execute("DROP TABLE IF EXISTS core.cases")
    con.execute(f"""
        CREATE TABLE core.cases AS
        SELECT d."Client" AS client, d."Client_ID" AS client_id, d.target,
               d.score_m2_xgboost, d.score_m3_rf, d.score_m4_autoencoder,
               d."Pred_m2_xgboost" AS pred_m2_xgboost, d."Pred_m3_rf" AS pred_m3_rf,
               d."Pred_m4_autoencoder" AS pred_m4_autoencoder,
               cm.segment, cm.activeness_segment, cm.group_status,
               NTILE(10) OVER (ORDER BY d.score_m4_autoencoder) AS decile_m4
        FROM read_csv_auto('{p(res, "XAI/Client_Input/Common/dev_scored.csv")}', sample_size=-1) d
        LEFT JOIN privacy.customer_master cm ON cm.client_id = d."Client_ID"
    """)

    # ----------------------------------------------- core.client_features
    log("== core.client_features (canonical Data.csv) ==")
    con.execute("DROP TABLE IF EXISTS core.client_features")
    con.execute(f"""
        CREATE TABLE core.client_features AS
        SELECT * RENAME ("Client" AS client, "Client_ID" AS client_id,
                         "Dispute" AS dispute, "Date" AS snapshot_date,
                         "Segment" AS segment, "Activeness_Segment" AS activeness_segment)
        FROM read_csv_auto('{p(res, "Part_A/Data/Data.csv")}', sample_size=-1)
    """)

    # -------------------------------------------- core.local_explanations
    log("== core.local_explanations (surrogate contribs, Autoencoder only) ==")
    con.execute("DROP TABLE IF EXISTS core.local_explanations")
    con.execute("""CREATE TABLE core.local_explanations (
        row_id INTEGER, dataset VARCHAR, client_id VARCHAR,
        segment VARCHAR, activeness_segment VARCHAR, target INTEGER,
        score_m4_autoencoder DOUBLE, decile INTEGER,
        feature VARCHAR, contribution DOUBLE)""")

    # Key used for Client_ID recovery: the float score plus target/segment/activeness.
    # Only keys that map to exactly ONE client in the scored dev population are accepted.
    con.execute("DROP TABLE IF EXISTS _recovery_keys")
    con.execute("""
        CREATE TEMP TABLE _recovery_keys AS
        SELECT round(score_m4_autoencoder, 9) AS k_score, target, segment, activeness_segment,
               min(client_id) AS client_id, count(DISTINCT client_id) AS n_clients
        FROM core.cases
        GROUP BY 1, 2, 3, 4
    """)

    recovery_stats = {}
    for dataset, rel in (("dev", "XAI/Outputs/surrogate_local/surrogate_contrib_Autoencoder_Dev.csv"),
                         ("validation_oos", "XAI/Outputs/surrogate_local/surrogate_contrib_Autoencoder_Validation_OOS.csv")):
        con.execute("DROP TABLE IF EXISTS _surr")
        con.execute(f"""
            CREATE TEMP TABLE _surr AS
            SELECT row_number() OVER () AS row_id, *
            FROM read_csv_auto('{p(res, rel)}', sample_size=-1)
        """)
        if dataset == "dev":
            # Recovery is only meaningful for the dev sample: dev_scored covers the
            # dev population only. Validation_OOS clients are not in dev_scored, so a
            # key "match" there would be coincidence — leave those client_id NULL.
            con.execute("""
                CREATE OR REPLACE TEMP TABLE _surr_linked AS
                SELECT s.*, CASE WHEN k.n_clients = 1 THEN k.client_id END AS rec_client_id
                FROM _surr s
                LEFT JOIN _recovery_keys k
                  ON k.k_score = round(s.score_m4_autoencoder, 9)
                 AND k.target = s.target
                 AND k.segment = s."Segment"
                 AND k.activeness_segment = s."Activeness_Segment"
            """)
        else:
            con.execute("""
                CREATE OR REPLACE TEMP TABLE _surr_linked AS
                SELECT s.*, CAST(NULL AS VARCHAR) AS rec_client_id FROM _surr s
            """)
        total, recovered = con.execute(
            "SELECT count(*), count(rec_client_id) FROM _surr_linked").fetchone()
        recovery_stats[dataset] = (total, recovered)
        con.execute(f"""
            INSERT INTO core.local_explanations
            SELECT row_id, '{dataset}', rec_client_id, "Segment", "Activeness_Segment",
                   target, score_m4_autoencoder, "Decile", name, value
            FROM (
                UNPIVOT _surr_linked
                ON COLUMNS('.*_contrib$')
                INTO NAME name VALUE value
            )
        """)
        con.execute("""
            UPDATE core.local_explanations
            SET feature = regexp_replace(feature, '_contrib$', '')
            WHERE dataset = ? AND feature LIKE '%_contrib'
        """, [dataset])
        log(f"  {dataset}: {total} sample rows, client_id recovered for {recovered} "
            f"({(100.0 * recovered / total):.1f}%)"
            + ("" if dataset == "dev" else " — no scored source for validation; recovery not attempted"))

    n = con.execute("SELECT count(*) FROM core.local_explanations").fetchone()[0]
    log(f"  long-format rows: {n:,}")

    # ------------------------------------------------ core.fairness_metrics
    log("== core.fairness_metrics / core.disparate_impact ==")
    con.execute("DROP TABLE IF EXISTS core.fairness_metrics")
    con.execute("""CREATE TABLE core.fairness_metrics (
        dataset VARCHAR, model_id VARCHAR, model_name VARCHAR, segment VARCHAR,
        metric VARCHAR, value DOUBLE, status VARCHAR, source VARCHAR)""")
    parity_cols = {
        "Statistical Parity (%)": "statistical_parity_pct",
        "Equal Opportunity (%)": "equal_opportunity_pct",
        "Predictive Parity (%)": "predictive_parity_pct",
        "Calibration Parity (%)": "calibration_parity_pct",
        "Treatment Equality (%)": "treatment_equality_pct",
    }
    for src_col, metric in parity_cols.items():
        con.execute(f"""
            INSERT INTO core.fairness_metrics
            SELECT "Evaluating Dataset", NULL, "Model", NULL, '{metric}', "{src_col}", NULL, 'global'
            FROM read_csv_auto('{p(res, "RAI/outputs/fairness_metrics_summary.csv")}', sample_size=-1)
        """)
        con.execute(f"""
            INSERT INTO core.fairness_metrics
            SELECT "Dataset", NULL, "Model", "Segment", '{metric}', "{src_col}", NULL, 'segment'
            FROM read_csv_auto('{p(res, "RAI/outputs/segment_fairness_metrics_summary.csv")}', sample_size=-1)
        """)
    con.execute(f"""
        INSERT INTO core.fairness_metrics
        SELECT "Dataset", NULL, "Model", "Segment", 'sample_size', "Sample Size", NULL, 'segment'
        FROM read_csv_auto('{p(res, "RAI/outputs/segment_fairness_metrics_summary.csv")}', sample_size=-1)
    """)
    di_summary = p(res, "RAI/outputs/disparate_impact_summary.csv")
    di_alerts = p(res, "RAI/outputs/disparate_impact_alerts.csv")
    con.execute("DROP TABLE IF EXISTS core.disparate_impact")
    con.execute(f"""
        CREATE TABLE core.disparate_impact AS
        SELECT s."Dataset" AS dataset, s."Model" AS model_name, s."Segment" AS segment,
               s."Group_Status" AS group_status, s."Approval_Rate" AS approval_rate,
               s."Baseline_Rate" AS baseline_rate, s."Impact_Ratio" AS impact_ratio,
               s."Status" AS status,
               (a."Dataset" IS NOT NULL) AS is_alert
        FROM read_csv_auto('{di_summary}', sample_size=-1) s
        LEFT JOIN read_csv_auto('{di_alerts}', sample_size=-1,
                                columns={{'Dataset':'VARCHAR','Model':'VARCHAR','Segment':'VARCHAR',
                                          'Group_Status':'VARCHAR','Approval_Rate':'DOUBLE',
                                          'Baseline_Rate':'DOUBLE','Impact_Ratio':'DOUBLE','Status':'VARCHAR'}}) a
          ON a."Dataset"=s."Dataset" AND a."Model"=s."Model" AND a."Segment"=s."Segment"
         AND a."Group_Status"=s."Group_Status"
    """)
    con.execute(f"""
        INSERT INTO core.fairness_metrics
        SELECT dataset, NULL, model_name, segment || ' / ' || group_status,
               'disparate_impact_ratio', impact_ratio, status, 'disparate_impact'
        FROM core.disparate_impact
    """)
    con.execute("UPDATE core.fairness_metrics SET model_id = CASE model_name "
                + " ".join(f"WHEN '{k}' THEN '{v}'" for k, v in RAI_MODEL_NAME_TO_ID.items())
                + " END")
    n_alerts = con.execute("SELECT count(*) FROM core.disparate_impact WHERE is_alert").fetchone()[0]
    nulls = con.execute("SELECT count(*) FROM core.fairness_metrics WHERE value IS NULL").fetchone()[0]
    log(f"  fairness rows: {con.execute('SELECT count(*) FROM core.fairness_metrics').fetchone()[0]} "
        f"(NULL values preserved: {nulls}); disparate-impact rows: "
        f"{con.execute('SELECT count(*) FROM core.disparate_impact').fetchone()[0]}, alerts: {n_alerts}")

    # --------------------------------------------- core.performance_metrics
    log("== core.performance_metrics ==")
    con.execute("DROP TABLE IF EXISTS core.performance_metrics")
    con.execute("""CREATE TABLE core.performance_metrics (
        model_id VARCHAR, dataset_label VARCHAR, dataset_norm VARCHAR,
        period VARCHAR, period_start DATE, metric VARCHAR, value DOUBLE)""")
    metric_cols = ["Rows", "Disputes", "AUC_ROC", "AUC_PR", "Gini", "KS", "Top_Decile_Recall",
                   "Best_Threshold", "Precision", "Recall", "F1", "TP", "FP", "TN", "FN"]
    for mid, (mdir, _, _) in MODEL_DIRS.items():
        sc = p(res, f"Part_A/Model_Outputs/{mdir}/performance_scorecard.csv")
        unpivot_cols = ", ".join(f'"{c}"' for c in metric_cols)
        con.execute(f"""
            INSERT INTO core.performance_metrics
            SELECT '{mid}', "Dataset",
                CASE
                  WHEN "Dataset" LIKE 'Train%' THEN 'train'
                  WHEN "Dataset" LIKE 'Validation%' THEN 'validation'
                  WHEN "Dataset" LIKE 'Internal OOT%' THEN 'internal_oot'
                  WHEN "Dataset" LIKE 'OOT Oct 2025' THEN 'oot_monthly'
                  WHEN "Dataset" LIKE 'OOT Nov 2025' THEN 'oot_monthly'
                  WHEN "Dataset" LIKE 'OOT Dec 2025' THEN 'oot_monthly'
                END,
                CASE
                  WHEN "Dataset" LIKE 'Internal OOT%' THEN 'Aug-Sep 2025'
                  WHEN "Dataset" = 'OOT Oct 2025' THEN 'Oct 2025'
                  WHEN "Dataset" = 'OOT Nov 2025' THEN 'Nov 2025'
                  WHEN "Dataset" = 'OOT Dec 2025' THEN 'Dec 2025'
                END,
                CASE
                  WHEN "Dataset" LIKE 'Internal OOT%' THEN DATE '2025-08-01'
                  WHEN "Dataset" = 'OOT Oct 2025' THEN DATE '2025-10-01'
                  WHEN "Dataset" = 'OOT Nov 2025' THEN DATE '2025-11-01'
                  WHEN "Dataset" = 'OOT Dec 2025' THEN DATE '2025-12-01'
                END,
                name, value
            FROM (
                UNPIVOT (SELECT * FROM read_csv_auto('{sc}', sample_size=-1))
                ON {unpivot_cols}
                INTO NAME name VALUE value
            )
        """)
    log(f"  performance rows: {con.execute('SELECT count(*) FROM core.performance_metrics').fetchone()[0]}")

    # ---------------------------------------------------- core.rai_narratives
    log("== core.rai_narratives / core.rai_documents ==")
    con.execute("DROP TABLE IF EXISTS core.rai_narratives")
    con.execute(f"""
        CREATE TABLE core.rai_narratives AS
        SELECT "Dataset" AS dataset, "Model" AS model_name,
               CASE "Model" {" ".join(f"WHEN '{k}' THEN '{v}'" for k, v in RAI_MODEL_NAME_TO_ID.items())} END AS model_id,
               "Persona" AS persona, "Narrative" AS narrative
        FROM read_csv_auto('{p(res, "RAI/outputs/llm_rai_reports/llm_rai_xai_consolidated.csv")}', sample_size=-1)
    """)
    con.execute("DROP TABLE IF EXISTS core.rai_documents")
    con.execute("CREATE TABLE core.rai_documents (doc_name VARCHAR, content VARCHAR)")
    md = open(os.path.join(res, "RAI", "outputs", "llm_rai_reports", "business_user_summary.md"),
              encoding="utf-8").read()
    con.execute("INSERT INTO core.rai_documents VALUES (?, ?)", ["business_user_summary", md])
    log(f"  narratives: {con.execute('SELECT count(*) FROM core.rai_narratives').fetchone()[0]}")

    # ------------------------------------------------------ lineage schema
    log("== lineage ==")
    con.execute("DROP TABLE IF EXISTS lineage.data_splits")
    con.execute(f"""
        CREATE TABLE lineage.data_splits AS
        SELECT "File" AS file, "Original Rows" AS original_rows, "New Rows" AS new_rows,
               "Original Disputes" AS original_disputes, "New Disputes" AS new_disputes,
               TRY_CAST("Min Date" AS DATE) AS min_date, TRY_CAST("Max Date" AS DATE) AS max_date
        FROM read_csv_auto('{p(res, "Part_A/Modeling_Data/split_summary.csv")}', sample_size=-1)
    """)
    con.execute("DROP TABLE IF EXISTS lineage.feature_lineage")
    con.execute("""
        CREATE TABLE lineage.feature_lineage AS
        SELECT variable_name AS feature, trim(unnest(string_split(source_variables, ','))) AS source_variable,
               calculation_logic
        FROM core.features
        WHERE source_variables IS NOT NULL AND source_variables <> ''
    """)
    log(f"  splits: {con.execute('SELECT count(*) FROM lineage.data_splits').fetchone()[0]} | "
        f"feature lineage edges: {con.execute('SELECT count(*) FROM lineage.feature_lineage').fetchone()[0]}")

    # ------------------------------------------------- ops (persist across rebuilds)
    con.execute("""CREATE TABLE IF NOT EXISTS ops.query_audit (
        id BIGINT, ts TIMESTAMP, question VARCHAR, generated_sql VARCHAR,
        repaired_sql VARCHAR, final_sql VARCHAR, rows_returned INTEGER,
        pii_reveal BOOLEAN, status VARCHAR, error VARCHAR, latency_ms INTEGER)""")
    con.execute("""CREATE TABLE IF NOT EXISTS ops.query_history (
        id BIGINT, ts TIMESTAMP, question VARCHAR, answer_summary VARCHAR)""")

    # ------------------------------------------------- vec.nlq_training seeds
    log("== vec.nlq_training ==")
    con.execute("DROP TABLE IF EXISTS vec.nlq_training")
    con.execute("""CREATE TABLE vec.nlq_training (
        question VARCHAR, category VARCHAR, gold_sql VARCHAR, chart_type VARCHAR)""")
    seeds = [
        ("What are the top 10 features driving dispute flags?", "global_explainability",
         "SELECT fi.rank, fi.feature, fi.value AS recon_error_gap, f.description "
         "FROM core.feature_importance_global fi LEFT JOIN core.features f ON f.variable_name = fi.feature "
         "WHERE fi.model_id = 'm4_autoencoder' ORDER BY fi.rank LIMIT 10", "bar"),
        ("Why was client CUST_00042 flagged?", "local_case_audit",
         "SELECT feature, contribution FROM core.local_explanations "
         "WHERE client_id = 'CUST_00042' AND dataset = 'dev' ORDER BY abs(contribution) DESC LIMIT 10", "bar"),
        ("Is the model fair across segments on validation?", "fairness",
         "SELECT segment, metric, value FROM core.fairness_metrics "
         "WHERE model_id = 'm4_autoencoder' AND dataset = 'Validation' AND source = 'segment' "
         "AND metric <> 'sample_size' ORDER BY segment, metric LIMIT 100", "bar"),
        ("Any disparate impact alerts?", "fairness",
         "SELECT count(*) AS alert_count FROM core.disparate_impact WHERE is_alert", "table_only"),
        ("Has performance drifted over the OOT months?", "performance_drift",
         "SELECT period, period_start, metric, value FROM core.performance_metrics "
         "WHERE model_id = 'm4_autoencoder' AND dataset_norm = 'oot_monthly' "
         "AND metric IN ('AUC_ROC','KS') ORDER BY period_start, metric", "line"),
        ("Does client CUST_00002 have active communication consent?", "consent",
         "SELECT client_id, purpose, granted_date, expiry_date, withdrawn_date, consent_state "
         "FROM privacy.consent_status WHERE client_id = 'CUST_00002' ORDER BY purpose LIMIT 50", "table_only"),
        ("Which model is best?", "model_comparison",
         "SELECT model_id, dataset_label, metric, value FROM core.performance_metrics "
         "WHERE dataset_norm IN ('train','validation') AND metric IN ('AUC_ROC','KS') "
         "ORDER BY model_id, dataset_label, metric LIMIT 100", "bar"),
        ("How is maxSscript_l_150d calculated?", "lineage",
         "SELECT variable_name, description, calculation_logic, source_variables "
         "FROM core.features WHERE variable_name = 'maxSscript_l_150d' LIMIT 5", "table_only"),
        ("What date ranges were used for train and validation?", "lineage",
         "SELECT file, new_rows, new_disputes, min_date, max_date FROM lineage.data_splits "
         "ORDER BY min_date LIMIT 10", "table_only"),
        ("How fair is XGBoost on validation overall?", "fairness",
         "SELECT metric, value FROM core.fairness_metrics WHERE model_id = 'm2_xgboost' "
         "AND dataset = 'Validation' AND source = 'global' LIMIT 10", "bar"),
        ("How many consent records are withdrawn or expired?", "consent",
         "SELECT consent_state, count(*) AS n FROM privacy.consent_status "
         "WHERE consent_state IN ('withdrawn','expired') GROUP BY consent_state", "bar"),
        ("Which segments have fairness metrics that were not computed?", "fairness",
         "SELECT segment, metric, count(*) AS not_computed FROM core.fairness_metrics "
         "WHERE value IS NULL AND source = 'segment' GROUP BY segment, metric ORDER BY segment LIMIT 100",
         "table_only"),
        ("Compare AUC_ROC across all three models on validation.", "performance_drift",
         "SELECT model_id, value AS auc_roc FROM core.performance_metrics "
         "WHERE dataset_norm = 'validation' AND metric = 'AUC_ROC' ORDER BY model_id", "bar"),
        ("Show the autoencoder's precision and recall by OOT month.", "performance_drift",
         "SELECT period, metric, value FROM core.performance_metrics "
         "WHERE model_id = 'm4_autoencoder' AND dataset_norm = 'oot_monthly' "
         "AND metric IN ('Precision','Recall') ORDER BY period_start, metric", "line"),
        ("How many clients are in each customer segment?", "lineage",
         "SELECT segment, count(*) AS clients FROM privacy.customer_master "
         "GROUP BY segment ORDER BY clients DESC", "bar"),
        ("How fair is the champion model on validation overall?", "fairness",
         "SELECT metric, value FROM core.fairness_metrics WHERE model_id = 'm4_autoencoder' "
         "AND dataset = 'Validation' AND source = 'global' LIMIT 10", "bar"),
        ("How many disputes are in the validation period?", "lineage",
         "SELECT file, new_disputes, min_date, max_date FROM lineage.data_splits "
         "WHERE file = 'val_oos.csv'", "table_only"),
    ]
    con.executemany("INSERT INTO vec.nlq_training VALUES (?,?,?,?)", seeds)
    log(f"  seed questions: {len(seeds)}")

    # ---------------------------------------------------- quality + asserts
    log("== lineage.data_quality / row-count assertions ==")
    con.execute("DROP TABLE IF EXISTS lineage.data_quality")
    con.execute("""CREATE TABLE lineage.data_quality (
        table_name VARCHAR, check_name VARCHAR, value VARCHAR, status VARCHAR)""")

    def qc(table, check, value, ok=True):
        con.execute("INSERT INTO lineage.data_quality VALUES (?,?,?,?)",
                    [table, check, str(value), "ok" if ok else "FAIL"])
        log(f"  [{'ok' if ok else 'FAIL'}] {table}: {check} = {value}")
        return ok

    failures = 0
    counts = {
        "core.client_features": "Part_A/Data/Data.csv",
        "core.cases": "XAI/Client_Input/Common/dev_scored.csv",
        "privacy.consent_log": "Part_A/Data/consent_log.csv",
        "privacy.customer_master": "Part_A/Data/customer_master.csv",
    }
    for table, src in counts.items():
        with open(os.path.join(res, src), "rb") as f:
            n_lines = sum(1 for _ in f)
        if not qc(src, f"physical line count == {EXPECTED_LINES[src]} (incl. header)",
                  n_lines, n_lines == EXPECTED_LINES[src]):
            failures += 1
        actual = con.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
        if not qc(table, f"loaded data rows == {EXPECTED_ROWS[src]}", actual,
                  actual == EXPECTED_ROWS[src]):
            failures += 1

    dupes = con.execute(
        "SELECT count(*) FROM (SELECT client_id FROM privacy.customer_master "
        "GROUP BY 1 HAVING count(*) > 1)").fetchone()[0]
    if not qc("privacy.customer_master", "duplicate client_id", dupes, dupes == 0):
        failures += 1
    # dev_scored is the oversampled dev set: each of the 205 original dispute clients
    # appears 10x (205 -> 2050, matching split_summary). Non-dispute rows must be unique.
    dup_normals = con.execute(
        "SELECT count(*) FROM (SELECT client_id FROM core.cases WHERE target = 0 "
        "GROUP BY 1 HAVING count(*) > 1)").fetchone()[0]
    if not qc("core.cases", "duplicate client_id among non-disputes", dup_normals, dup_normals == 0):
        failures += 1
    distinct_clients = con.execute("SELECT count(DISTINCT client_id) FROM core.cases").fetchone()[0]
    if not qc("core.cases", "distinct clients == 91,635 (pre-oversampling train rows)",
              distinct_clients, distinct_clients == 91_635):
        failures += 1

    nf = con.execute("SELECT count(*) FROM core.features").fetchone()[0]
    qc("core.features", "glossary entries", nf, nf == 277)
    orphan = con.execute("""
        SELECT count(*) FROM core.cases c
        WHERE NOT EXISTS (SELECT 1 FROM privacy.customer_master m WHERE m.client_id = c.client_id)
    """).fetchone()[0]
    if not qc("core.cases", "clients missing from customer_master", orphan, orphan == 0):
        failures += 1
    null_consent = con.execute(
        "SELECT count(*) FROM privacy.consent_log WHERE client_id IS NULL").fetchone()[0]
    qc("privacy.consent_log", "NULL client_id rows", null_consent, null_consent == 0)
    for ds, (total, recovered) in recovery_stats.items():
        qc("core.local_explanations", f"client_id recovery rate ({ds})",
           f"{recovered}/{total} ({100.0 * recovered / total:.1f}%)")

    # ------------------------------------------------------------ load report
    log("== Load report (rows per table) ==")
    tables = con.execute("""
        SELECT table_schema || '.' || table_name
        FROM information_schema.tables
        WHERE table_schema IN ('core','privacy','lineage','ops','vec') AND table_type='BASE TABLE'
        ORDER BY 1
    """).fetchall()
    for (t,) in tables:
        c = con.execute(f"SELECT count(*) FROM {t}").fetchone()[0]
        log(f"  {t:45s} {c:>12,}")

    con.close()
    out_dir = os.path.dirname(args.db)
    with open(os.path.join(out_dir, "load_report.txt"), "w", encoding="utf-8") as f:
        f.write("\n".join(report))
    if failures:
        sys.exit(f"{failures} quality assertion(s) FAILED — see load report.")
    log(f"\nDone. DB: {args.db}")


if __name__ == "__main__":
    main()
