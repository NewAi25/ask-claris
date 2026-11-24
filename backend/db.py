"""DuckDB access for Ask Claris: read-only NLQ guard, table allowlist,
column auto-repair, row limits, and ops audit writes.

Guard + auto-repair carried over from the prototype with one extension:
the repair may also snap near-miss TABLE names to allowlisted ones. Repair
only ever substitutes identifiers — it can never weaken the SELECT-only guard.
"""
import difflib
import os
import re
import time

import duckdb

_FORBIDDEN = re.compile(
    r"\b(insert|update|delete|drop|alter|attach|detach|copy|create|replace|"
    r"truncate|pragma|install|load|export|import|set|call|merge|grant|revoke|vacuum)\b",
    re.IGNORECASE,
)
ROW_LIMIT = 1000

# NLQ may only touch these schemas. ops.* is writable by the app itself but is
# not queryable through generated SQL.
ALLOWED_SCHEMAS = ("core", "privacy", "lineage", "vec")

DEFAULT_DB_PATH = os.path.abspath(os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "data", "claris.duckdb"))


class DB:
    def __init__(self, db_path: str | None = None):
        self.path = db_path or os.getenv("CLARIS_DB", DEFAULT_DB_PATH)
        if not os.path.exists(self.path):
            raise FileNotFoundError(
                f"Database not found: {self.path}. Run `python etl/build_db.py` first.")
        self.con = duckdb.connect(self.path)
        self.con.execute("CREATE SEQUENCE IF NOT EXISTS ops.audit_seq")
        self.tables = self._index_tables()      # {qualified_table: [columns]}
        self.all_columns = sorted({c for cols in self.tables.values() for c in cols})

    def _index_tables(self):
        rows = self.con.execute("""
            SELECT table_schema, table_name, column_name
            FROM information_schema.columns
            WHERE table_schema IN ({})
            ORDER BY table_schema, table_name, ordinal_position
        """.format(",".join(f"'{s}'" for s in ALLOWED_SCHEMAS))).fetchall()
        tables: dict[str, list[str]] = {}
        for schema, table, col in rows:
            tables.setdefault(f"{schema}.{table}", []).append(col)
        return tables

    # ------------------------------------------------------------------ guard
    def guard(self, sql: str):
        """(ok, error). Single read-only SELECT/CTE over allowlisted tables only."""
        if not sql or not sql.strip():
            return False, "Empty query."
        s = sql.strip().rstrip(";")
        if ";" in s:
            return False, "Only a single statement is allowed."
        low = s.lstrip("(").lstrip().lower()
        if not (low.startswith("select") or low.startswith("with")):
            return False, "Only read-only SELECT queries are allowed."
        m = _FORBIDDEN.search(s)
        if m:
            return False, f"Forbidden keyword in query: {m.group(0).upper()}."
        bad = self._disallowed_tables(s)
        if bad:
            return False, f"Table(s) not in the allowlist: {', '.join(sorted(bad))}."
        return True, None

    _TABLE_REF = re.compile(r'\b(?:from|join)\s+("?[\w]+"?(?:\."?[\w]+"?)?)', re.IGNORECASE)

    def _referenced_tables(self, sql: str) -> list[str]:
        return [t.replace('"', "") for t in self._TABLE_REF.findall(sql)]

    def _disallowed_tables(self, sql: str) -> set[str]:
        # CTE names defined in the query itself are legitimate FROM targets.
        ctes = {m.group(1).lower()
                for m in re.finditer(r'(?:with|,)\s*("?[\w]+"?)\s+as\s*\(', sql, re.IGNORECASE)}
        bad = set()
        for t in self._referenced_tables(sql):
            tl = t.lower()
            if tl in ctes or tl.replace('"', "") in ctes:
                continue
            if tl in (k.lower() for k in self.tables):
                continue
            # bare table name with a unique allowlisted match is fine (binder resolves it)
            matches = [k for k in self.tables if k.lower().split(".")[1] == tl]
            if matches:
                continue
            bad.add(t)
        return bad

    @staticmethod
    def enforce_limit(sql: str) -> str:
        s = sql.strip().rstrip(";")
        if re.search(r"\blimit\b\s+\d+", s, re.IGNORECASE):
            return s
        return f"SELECT * FROM (\n{s}\n) AS _capped LIMIT {ROW_LIMIT}"

    def validate(self, sql: str):
        """Bind without scanning; catches unknown columns/tables."""
        try:
            self.con.execute("EXPLAIN " + sql).fetchall()
            return True, None
        except Exception as e:
            return False, str(e).splitlines()[0]

    # ----------------------------------------------------------- auto-repair
    @staticmethod
    def _norm(s: str) -> str:
        return re.sub(r"[^a-z0-9]", "", s.lower())

    def _closest_column(self, bad: str):
        bn = self._norm(bad)
        if not bn:
            return None
        best, best_score = None, 0.0
        for c in self.all_columns:
            cn = self._norm(c)
            if not cn:
                continue
            if cn == bn or cn.endswith(bn) or bn in cn or cn in bn:
                score = 0.97
            else:
                score = difflib.SequenceMatcher(None, bn, cn).ratio()
            if score > best_score:
                best_score, best = score, c
        return best if best_score >= 0.62 else None

    def _closest_table(self, bad: str):
        bn = self._norm(bad.split(".")[-1])
        if not bn:
            return None
        best, best_score = None, 0.0
        for t in self.tables:
            tn = self._norm(t.split(".")[1])
            score = 0.97 if (tn == bn or bn in tn or tn in bn) else \
                difflib.SequenceMatcher(None, bn, tn).ratio()
            if score > best_score:
                best_score, best = score, t
        return best if best_score >= 0.6 else None

    @staticmethod
    def _sub_identifier(sql: str, bad: str, good: str, quote=True) -> str:
        goodq = '"' + good + '"' if quote else good
        out = sql.replace('"' + bad + '"', goodq)
        pat = re.compile(r'(?<![\w".])' + re.escape(bad) + r'(?![\w"])')
        return pat.sub(goodq, out)

    @staticmethod
    def _sub_table(sql: str, bad: str, good: str) -> str:
        """Replace a table reference, including any schema prefix the model attached
        (e.g. core.feature_importance -> core.feature_importance_global)."""
        bad_name = bad.replace('"', "").split(".")[-1]
        pat = re.compile(r'(?<![\w".])((?:[\w]+\.)?' + re.escape(bad_name) + r')(?![\w"])')
        return pat.sub(good, sql)

    def _resolve_table(self, name: str):
        nl = name.replace('"', "").lower()
        for t in self.tables:
            if t.lower() == nl or t.lower().split(".")[1] == nl.split(".")[-1]:
                return t
        return None

    def _explain_error(self, sql: str):
        try:
            self.con.execute("EXPLAIN " + sql).fetchall()
            return None
        except Exception as e:
            return str(e)

    def repair(self, sql: str, max_iter: int = 6):
        """(sql, ok, repairs). Iteratively snap unknown column/table identifiers to
        the nearest real ones, re-validating each step."""
        cur, repairs = sql, []
        for _ in range(max_iter):
            full = self._explain_error(cur)
            if full is None:
                return cur, True, repairs
            mcol = re.search(r'column "([^"]+)" not found', full, re.I) or \
                re.search(r'\bcolumn ([\w\-]+) not found', full, re.I) or \
                re.search(r'column named "([^"]+)"', full, re.I)
            mtab = re.search(r'Table with name ([\w."]+) does not exist', full, re.I) or \
                re.search(r'Table "([^"]+)" not found', full, re.I)
            new = cur
            if mtab and not mcol:
                bad = mtab.group(1).replace('"', "")
                # Prefer DuckDB's own "Did you mean" candidate when it resolves to an
                # allowlisted table; otherwise fall back to fuzzy matching.
                good = None
                sugg = re.search(r'Did you mean "([^"]+)"', full)
                if sugg:
                    good = self._resolve_table(sugg.group(1))
                if not good:
                    good = self._closest_table(bad)
                if good:
                    new = self._sub_table(cur, bad, good)
                    if new != cur:
                        repairs.append(f"table {bad} -> {good}")
            elif mcol:
                bad = mcol.group(1)
                good = None
                cand = re.search(r'andidate bindings?:\s*"?([\w.\-]+)"?', full)
                if cand:
                    good = cand.group(1).split(".")[-1].strip('"')
                if not good:
                    good = self._closest_column(bad)
                if good and good != bad:
                    new = self._sub_identifier(cur, bad, good)
                    repairs.append(f"column {bad} -> {good}")
            if new == cur:
                return cur, False, repairs
            cur = new
        return cur, False, repairs

    # -------------------------------------------------------------- execution
    def run(self, sql: str):
        cur = self.con.execute(sql)
        cols = [d[0] for d in cur.description]
        rows = cur.fetchall()
        return cols, [list(r) for r in rows]

    # ------------------------------------------------------------------ audit
    def audit(self, question: str, generated_sql: str, repaired_sql: str | None,
              final_sql: str | None, rows_returned: int, pii_reveal: bool,
              status: str, error: str | None, latency_ms: int):
        self.con.execute(
            "INSERT INTO ops.query_audit VALUES (nextval('ops.audit_seq'), now(), ?,?,?,?,?,?,?,?,?)",
            [question, generated_sql, repaired_sql, final_sql, rows_returned,
             pii_reveal, status, error, latency_ms])

    def history(self, question: str, answer_summary: str):
        self.con.execute(
            "INSERT INTO ops.query_history VALUES (nextval('ops.audit_seq'), now(), ?, ?)",
            [question, answer_summary])

    def counts(self):
        return {t: self.con.execute(f"SELECT count(*) FROM {t}").fetchone()[0]
                for t in self.tables}
