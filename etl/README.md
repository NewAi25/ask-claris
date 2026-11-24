# ETL

`build_db.py` is the idempotent ETL that builds the governance database (`data/claris.duckdb`) from the modelling pipeline outputs: about 20 source files, 5 schemas, a 10.84M-row long-format explanation table and 16 automated data-quality checks. See the main README for details.
