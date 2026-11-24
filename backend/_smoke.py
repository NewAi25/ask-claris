"""Offline smoke test: DB load, guard, auto-repair, masking. No LLM calls."""
from db import DB
from masking import mask_results
from schema_doc import build_schema_doc, worked_examples

db = DB()
print("tables:", len(db.tables), "| columns indexed:", len(db.all_columns))

# guard
for sql, want in [
    ("SELECT * FROM core.models", True),
    ("DROP TABLE core.models", False),
    ("SELECT * FROM core.models; SELECT 1", False),
    ("SELECT * FROM ops.query_audit", False),          # not allowlisted
    ("WITH x AS (SELECT 1 AS a) SELECT * FROM x", True),
]:
    ok, err = db.guard(sql)
    print(f"guard {'ok' if ok == want else 'UNEXPECTED'}: {sql[:45]!r} -> {ok} {err or ''}")

# auto-repair: near-miss column and table names
bad = "SELECT featur, valu FROM feature_importance WHERE model_id='m4_autoencoder' LIMIT 5"
ok, sql, repairs, err = __import__("nlq").NLQEngine.__dict__["_validate_or_repair"](
    type("E", (), {"db": db})(), bad)
print("repair ok:", ok, "| repairs:", repairs)
if ok:
    cols, rows = db.run(db.enforce_limit(sql))
    print("repaired query rows:", len(rows), rows[0] if rows else None)

# masking
cols = ["client_id", "name", "email", "national_id", "date_of_birth"]
rows = [["CUST_00001", "Emma Stone", "emma.stone@mail.com", "123-45-3286", "1987-04-12"]]
masked, mcols = mask_results(cols, rows)
print("masked:", masked[0], "| masked cols:", mcols)

# prompt assets build without error
doc = build_schema_doc(db)
ex = worked_examples(db)
print("schema doc chars:", len(doc), "| examples chars:", len(ex))

# every gold seed SQL must pass guard + validate
for q, sql in db.con.execute("SELECT question, gold_sql FROM vec.nlq_training").fetchall():
    g, ge = db.guard(sql)
    v, ve = db.validate(sql)
    print(("seed ok " if g and v else f"seed FAIL ({ge or ve}) "), q[:60])
