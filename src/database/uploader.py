from __future__ import annotations

import io
import logging
import re
from pathlib import Path
from typing import Any, Optional

import duckdb
import pandas as pd

logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent.parent.parent
DATA_DIR = BASE_DIR / "data"
USER_DBS_DIR = DATA_DIR / "user_dbs"


def get_user_db_path(user_id: int) -> Path:
    USER_DBS_DIR.mkdir(parents=True, exist_ok=True)
    return USER_DBS_DIR / f"user_{user_id}.duckdb"


def sanitize_table_name(name: str) -> str:
    cleaned = re.sub(r"[^a-zA-Z0-9_]", "_", name.strip().lower())
    cleaned = re.sub(r"_+", "_", cleaned).strip("_")
    if not cleaned or cleaned[0].isdigit():
        cleaned = f"tbl_{cleaned}"
    return cleaned[:64]


def sanitize_column_name(name: str) -> str:
    cleaned = re.sub(r"[^a-zA-Z0-9_]", "_", str(name).strip().lower())
    cleaned = re.sub(r"_+", "_", cleaned).strip("_")
    return cleaned or "col"


def import_dataset(
    user_id: int,
    filename: str,
    file_bytes: bytes,
    custom_table_name: Optional[str] = None,
) -> dict[str, Any]:
    ext = Path(filename).suffix.lower()
    if ext not in [".csv", ".tsv", ".parquet", ".xlsx", ".xls"]:
        raise ValueError(
            f"Unsupported file format '{ext}'. Allowed formats: .csv, .tsv, .xlsx, .parquet"
        )

    base_name = custom_table_name or Path(filename).stem
    table_name = sanitize_table_name(base_name)

    logger.info("Importing %s as table '%s' for user %s", filename, table_name, user_id)

    bio = io.BytesIO(file_bytes)
    if ext in [".csv", ".tsv"]:
        sep = "\t" if ext == ".tsv" else ","
        try:
            df = pd.read_csv(bio, sep=sep, encoding="utf-8", low_memory=False)
        except UnicodeDecodeError:
            bio.seek(0)
            df = pd.read_csv(bio, sep=sep, encoding="latin1", low_memory=False)
    elif ext in [".xlsx", ".xls"]:
        df = pd.read_excel(bio)
    elif ext == ".parquet":
        df = pd.read_parquet(bio)
    else:
        raise ValueError(f"Unsupported format: {ext}")

    if df.empty:
        raise ValueError("Uploaded dataset is empty (0 rows found).")

    df.columns = [sanitize_column_name(c) for c in df.columns]

    user_db = get_user_db_path(user_id)
    conn = duckdb.connect(str(user_db), read_only=False)
    try:
        conn.register("upload_df_temp", df)
        conn.execute(f"CREATE OR REPLACE TABLE {table_name} AS SELECT * FROM upload_df_temp;")
        row_count = conn.execute(f"SELECT COUNT(*) FROM {table_name};").fetchone()[0]
        cols_info = conn.execute(f"DESCRIBE {table_name};").fetchall()
        columns = [{"name": c[0], "type": str(c[1])} for c in cols_info]
        sample_rows = conn.execute(f"SELECT * FROM {table_name} LIMIT 5;").df().to_dict(orient="records")

        logger.info(
            "Imported table '%s' (%d rows, %d columns) into %s",
            table_name,
            row_count,
            len(columns),
            user_db,
        )

        return {
            "table_name": table_name,
            "filename": filename,
            "row_count": row_count,
            "column_count": len(columns),
            "columns": columns,
            "sample_rows": sample_rows,
        }
    finally:
        conn.close()


def list_user_tables(user_id: int) -> list[dict[str, Any]]:
    user_db = get_user_db_path(user_id)
    if not user_db.exists():
        return []

    try:
        conn = duckdb.connect(str(user_db), read_only=True)
        tables = conn.execute("SHOW TABLES;").fetchall()
        previews = []
        for t in tables:
            t_name = t[0]
            try:
                cnt = conn.execute(f"SELECT COUNT(*) FROM {t_name};").fetchone()[0]
                cols_info = conn.execute(f"DESCRIBE {t_name};").fetchall()
                columns = [c[0] for c in cols_info]
                sample_rows = conn.execute(f"SELECT * FROM {t_name} LIMIT 5;").df().to_dict(orient="records")
                previews.append({
                    "name": t_name,
                    "row_count": cnt,
                    "column_count": len(columns),
                    "columns": columns,
                    "sample_rows": sample_rows,
                })
            except Exception as e:
                logger.warning("Error inspecting user table %s: %s", t_name, e)
        conn.close()
        return previews
    except Exception as e:
        logger.warning("Error reading user database %s: %s", user_db, e)
        return []


def delete_user_table(user_id: int, table_name: str) -> bool:
    clean_name = sanitize_table_name(table_name)
    user_db = get_user_db_path(user_id)
    if not user_db.exists():
        return False

    conn = duckdb.connect(str(user_db), read_only=False)
    try:
        conn.execute(f"DROP TABLE IF EXISTS {clean_name};")
        return True
    finally:
        conn.close()
