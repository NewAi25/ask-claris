"""PII masking applied to every NLQ result by default.

Full values are returned only when the request sets reveal_pii=true, and that
flag is written to ops.query_audit for every query.
"""
import re

# Result columns considered PII (matched case-insensitively on the output column name).
PII_COLUMNS = {"name", "email", "national_id", "phone_no", "phoneno", "phone",
               "address", "date_of_birth", "dob"}


def _mask_name(v: str) -> str:
    return " ".join(w[0] + "***" for w in v.split() if w)


def _mask_email(v: str) -> str:
    local, _, domain = v.partition("@")
    return (local[:2] + "***@" + domain) if domain else v[:2] + "***"


def _mask_id(v: str) -> str:
    digits = re.sub(r"\D", "", v)
    tail = digits[-4:] if len(digits) >= 4 else digits
    return "***-**-" + tail


def _mask_address(v: str) -> str:
    return "*** (address hidden)"


def _mask_dob(v: str) -> str:
    m = re.search(r"\b(19|20)\d{2}\b", v)
    return (m.group(0) + "-**-**") if m else "****-**-**"


_MASKERS = {
    "name": _mask_name,
    "email": _mask_email,
    "national_id": _mask_id,
    "phone_no": _mask_id,
    "phoneno": _mask_id,
    "phone": _mask_id,
    "address": _mask_address,
    "date_of_birth": _mask_dob,
    "dob": _mask_dob,
}


def mask_results(columns: list[str], rows: list[list]) -> tuple[list[list], list[str]]:
    """Return (masked_rows, masked_column_names)."""
    targets = {i: _MASKERS[c.lower()] for i, c in enumerate(columns) if c.lower() in PII_COLUMNS}
    if not targets:
        return rows, []
    out = []
    for row in rows:
        r = list(row)
        for i, fn in targets.items():
            if r[i] is not None:
                r[i] = fn(str(r[i]))
        out.append(r)
    return out, [columns[i] for i in targets]


def mask_value(kind: str, value) -> str:
    fn = _MASKERS.get(kind, _mask_name)
    return fn(str(value)) if value is not None else ""
