"""Builds the documented-schema section of the NLQ system prompt.

Table descriptions are curated; column lists and low-cardinality value hints are
read live from the database at startup so the prompt can never drift from the
real schema.
"""
TABLE_DOCS = {
    "core.models": "The 3 dispute-risk models. is_champion=true marks the default model (m4_autoencoder).",
    "core.model_versions": "Hyperparameters/architecture per model (config_json) and training notes.",
    "core.features": "Semantic glossary of all 277 variables: description, calculation_logic, source_variables. JOIN on variable_name for plain-English meaning.",
    "core.feature_importance_global": "Global feature importance per model. ONE method per model: m2_xgboost='gain', m3_rf='mda', m4_autoencoder='recon_error_gap' (value = dispute minus normal reconstruction error; normal/dispute_recon_error columns filled for m4 only). rank 1 = most important.",
    "core.cases": "Scored dev population (93,480 rows, one per scored row). Scores+binary preds for all 3 models. NOTE: dispute rows (target=1) are oversampled 10x — use COUNT(DISTINCT client_id) for client counts. decile_m4: 1..10 by autoencoder score, 10 = highest/riskiest.",
    "core.client_features": "Canonical raw feature dataset (150,339 clients, core portfolio only): 181 raw features + dispute ('D'/'N'), snapshot_date, segment, activeness_segment.",
    "core.local_explanations": "Per-row local explanations, AUTOENCODER ONLY, long format (feature, contribution). 20k-row samples per dataset ('dev','validation_oos'); client_id recovered for ~97% of dev rows, NULL otherwise and for all validation rows. decile 10 = riskiest.",
    "core.fairness_metrics": "Fairness metrics, long format. source='global' (whole dataset, segment NULL), 'segment' (per group-status segment: Privileged/Protected/Vulnerable/...), 'disparate_impact'. Values are percentages (100 = perfect parity). NULL value = metric NOT COMPUTED for that slice — report as 'not computed', NEVER as 0 or 100.",
    "core.disparate_impact": "Disparate impact detail: approval_rate, baseline_rate, impact_ratio, status per dataset x model x segment x group_status. is_alert=true rows are open alerts (currently zero).",
    "core.performance_metrics": "Performance scorecards, long format. dataset_norm in ('train','validation','internal_oot','oot_monthly'); monthly OOT rows have period ('Oct 2025','Nov 2025','Dec 2025') and period_start for ordering. Metrics include AUC_ROC, AUC_PR, Gini, KS, Top_Decile_Recall, Precision, Recall, F1, TP/FP/TN/FN, Rows, Disputes.",
    "core.rai_narratives": "Pre-written persona narratives (dataset x model x persona) for role-tailored prose.",
    "core.rai_documents": "Long-form RAI documents (business_user_summary).",
    "privacy.customer_master": "Customer PII + segmentation (150,339 rows). PII columns (name, email, national_id, phone_no, address, date_of_birth) are masked downstream — select them only when the question is about a specific client's identity.",
    "privacy.consent_log": "Consent rows (429,212; multiple purposes per client) with granted/expiry/withdrawn dates.",
    "privacy.consent_status": "VIEW over consent_log adding consent_state ('active'/'expired'/'withdrawn'/'unknown') as of the current date — prefer this for consent questions.",
    "privacy.segment_master": "The 10 customer segments: nickname (segment), group_status, profiles and RAI testing context.",
    "privacy.activeness_segment_definitions": "The 6 activeness tiers (Dormant..Power User) with thresholds.",
    "lineage.data_splits": "Train/validation/OOT split files with row/dispute counts and date ranges.",
    "lineage.feature_lineage": "feature -> source_variable edges derived from the variable dictionary.",
    "lineage.data_quality": "ETL quality checks from the latest build.",
    "vec.nlq_training": "Seed questions with gold SQL (used as worked examples).",
}

# Columns whose distinct values are enumerated in the prompt (low cardinality).
VALUE_HINTS = [
    ("core.models", "model_id"),
    ("core.fairness_metrics", "dataset"),
    ("core.fairness_metrics", "metric"),
    ("core.fairness_metrics", "segment"),
    ("core.performance_metrics", "dataset_label"),
    ("core.performance_metrics", "period"),
    ("core.performance_metrics", "metric"),
    ("core.rai_narratives", "persona"),
    ("core.rai_narratives", "model_name"),
    ("privacy.consent_log", "purpose"),
    ("privacy.customer_master", "segment"),
    ("privacy.customer_master", "activeness_segment"),
    ("privacy.customer_master", "group_status"),
    ("core.local_explanations", "dataset"),
    ("core.client_features", "dispute"),
]


def build_schema_doc(db, compact: bool = False) -> str:
    """compact=True trims the prompt for small local models (~4k-token context):
    the 186 raw feature columns of core.client_features are summarized instead of listed."""
    lines = ["DATABASE SCHEMA (DuckDB). Query ONLY these tables, with schema-qualified names:"]
    for table, cols in db.tables.items():
        desc = TABLE_DOCS.get(table, "")
        lines.append(f"\n{table} — {desc}")
        if table == "core.client_features":
            meta = ["client", "client_id", "dispute", "snapshot_date", "segment", "activeness_segment"]
            feats = [c for c in cols if c not in meta]
            lines.append("  columns: " + ", ".join(meta))
            if compact:
                lines.append(
                    f"  plus {len(feats)} raw feature columns named exactly like "
                    "core.features.variable_name (e.g. acct_dormancyPeriod, txn_totalAmt_l_030d, "
                    "maxSscript_l_150d — rolling windows end in _l_NNNd).")
            else:
                lines.append("  feature columns (" + str(len(feats)) + "): " + ", ".join(feats))
        else:
            lines.append("  columns: " + ", ".join(cols))
    hints = ["\nKNOWN VALUES (copy spellings exactly):"]
    for table, col in VALUE_HINTS:
        try:
            vals = [r[0] for r in db.con.execute(
                f"SELECT DISTINCT {col} FROM {table} WHERE {col} IS NOT NULL ORDER BY 1 LIMIT 40"
            ).fetchall()]
        except Exception:
            continue
        hints.append(f"  {table}.{col}: " + ", ".join(repr(str(v)) for v in vals))
    return "\n".join(lines) + "\n" + "\n".join(hints)


def worked_examples(db) -> str:
    rows = db.con.execute(
        "SELECT question, gold_sql, chart_type FROM vec.nlq_training").fetchall()
    out = ["WORKED EXAMPLES (real, validated queries — copy the exact table/column spellings):"]
    for q, sql, chart in rows:
        out.append(f"  Q: {q}\n    sql: {sql}\n    chart: {chart}")
    return "\n".join(out)
