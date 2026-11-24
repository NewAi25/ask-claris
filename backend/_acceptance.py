"""End-to-end acceptance tests against a running backend (http://localhost:8000).

Covers the 10 launch acceptance questions (test 2 and 3 have two variants each).
Prints PASS/FAIL per check plus the grounded summary for human review.
"""
import json
import sys
import urllib.request

BASE = "http://localhost:8000"
results = []


def ask(question, **ctx):
    body = json.dumps({"question": question, **ctx}).encode()
    req = urllib.request.Request(f"{BASE}/ask", data=body,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=600) as r:
        return json.loads(r.read())


def check(test_id, question, resp, predicates, ctx_note=""):
    failures = []
    for desc, fn in predicates:
        try:
            if not fn(resp):
                failures.append(desc)
        except Exception as e:
            failures.append(f"{desc} (raised {e})")
    ok = not failures
    results.append((test_id, ok, failures))
    print(f"\n{'='*80}\n[{ 'PASS' if ok else 'FAIL' }] T{test_id}: {question} {ctx_note}")
    if failures:
        print("  failed checks:", failures)
    print("  summary:", (resp.get("summary") or resp.get("error") or resp.get("clarification") or "")[:600])
    for n in resp.get("notices", []):
        print("  notice:", n)
    if resp.get("sql_used"):
        print("  sql:", resp["sql_used"][:300].replace("\n", " "))
    if resp.get("rows") is not None:
        print("  rows:", resp.get("row_count"), "| first:", resp["rows"][:2])


def has_rows(r):
    return bool(r.get("rows"))


def text(r):
    return " ".join([r.get("summary") or ""] + (r.get("notices") or [])).lower()


# 1. Global explainability
r = ask("What are the top 10 features driving dispute flags?")
check(1, "top 10 features", r, [
    ("has rows", has_rows),
    ("10 rows", lambda r: r.get("row_count") == 10),
    ("maxSscript_l_150d is #1", lambda r: any("maxSscript_l_150d" in str(row) for row in r["rows"][:1])),
    ("real SQL", lambda r: "feature_importance" in (r.get("sql_used") or "")),
])

# 2a. Case audit — client WITH a recovered explanation row
r = ask("Why was client CUST_09222 flagged?")
check("2a", "why was CUST_09222 flagged (per-client)", r, [
    ("has rows", has_rows),
    ("identity line present", lambda r: "CUST_09222" in (r.get("identity") or "")),
    ("PII masked (no full name)", lambda r: "***" in (r.get("identity") or "")),
    ("NOT cohort-level", lambda r: "cohort" not in text(r)),
])

# 2b. Case audit — scored client WITHOUT an explanation row -> cohort fallback
r = ask("Why was client CUST_06310 flagged?")
check("2b", "why was CUST_06310 flagged (cohort fallback)", r, [
    ("has rows", has_rows),
    ("cohort clearly labeled", lambda r: "cohort" in text(r)),
])

# 3a. Fairness across segments on validation (champion default)
r = ask("Is the model fair across segments on validation?")
check("3a", "segment fairness on validation", r, [
    ("has rows", has_rows),
    ("segment fairness SQL", lambda r: "fairness" in (r.get("sql_used") or "").lower()),
])

# 3b. XGBoost global fairness on validation (~20.58% statistical parity)
r = ask("How fair is XGBoost on validation overall?")
check("3b", "XGBoost validation fairness", r, [
    ("has rows", has_rows),
    ("20.5x% statistical parity in data", lambda r: any("20.57" in str(row) or "20.578" in str(row) for row in r["rows"])),
])

# 4. Disparate impact alerts (empty alerts file -> zero, backed by a query)
r = ask("Any disparate impact alerts?")
check(4, "disparate impact alerts", r, [
    ("query was run", lambda r: bool(r.get("sql_used"))),
    ("zero alerts in result", lambda r: any("0" in str(v) for row in r.get("rows", []) for v in row) or r.get("row_count") == 0),
    ("summary says none", lambda r: any(w in text(r) for w in ("no ", "none", "zero", "0 "))),
])

# 5. Performance drift over OOT months
r = ask("Has the autoencoder's performance drifted over the OOT months?")
check(5, "performance drift OOT", r, [
    ("has rows", has_rows),
    ("line chart", lambda r: (r.get("chart") or {}).get("type") == "line"),
    ("monthly periods present", lambda r: any("Oct 2025" in str(row) for row in r["rows"])),
])

# 6. PSI / distribution drift -> honest deferral + offered trend
r = ask("Show distribution drift / PSI for the champion model")
check(6, "PSI honest deferral", r, [
    ("says not computed", lambda r: "not" in text(r) and ("comput" in text(r) or "available" in text(r))),
    ("offers performance trend rows", has_rows),
])

# 7. Consent check (CUST_00002 Communication consent is withdrawn)
r = ask("Does client CUST_00002 have active communication consent?")
check(7, "consent CUST_00002", r, [
    ("has rows", has_rows),
    ("says withdrawn / not active", lambda r: "withdraw" in text(r) or "not" in text(r)),
])

# 8. Which model is best -> trade-off, never a single winner
r = ask("Which model is best?")
check(8, "model trade-off", r, [
    ("has rows", has_rows),
    ("mentions XGBoost and Autoencoder", lambda r: "xgboost" in text(r) and "autoencoder" in text(r)),
    ("mentions fairness or overfit trade-off", lambda r: any(w in text(r) for w in ("fair", "overfit", "trade-off", "tradeoff"))),
])

# 9. Segment B scope refusal
r = ask("Show the top Segment B clients by fraud score")
check(9, "Segment B refusal", r, [
    ("refused with scope message", lambda r: "out of scope" in text(r) or "core portfolio" in text(r)),
    ("no fabricated rows", lambda r: not r.get("rows")),
])

# 10. Near-miss column names -> auto-repair (or model self-corrects; either is a pass,
#     but we report which path fired)
r = ask("Run this for me: select featur, valu from feature_importance where model_id = 'm4_autoencoder' order by rank limit 5")
check(10, "near-miss identifiers auto-repair", r, [
    ("answered correctly", has_rows),
    ("top feature correct", lambda r: any("maxSscript_l_150d" in str(row) for row in r["rows"][:1])),
])
print("\n  repair path:", r.get("repairs") or "(model corrected identifiers itself)")

print("\n" + "=" * 80)
passed = sum(1 for _, ok, _ in results if ok)
print(f"RESULT: {passed}/{len(results)} tests passed")
for tid, ok, fails in results:
    if not ok:
        print(f"  FAILED T{tid}: {fails}")
sys.exit(0 if passed == len(results) else 1)
