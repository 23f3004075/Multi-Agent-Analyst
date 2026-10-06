from __future__ import annotations

import io
import sys
import zipfile
from pathlib import Path

import duckdb
import httpx
from rich.console import Console
from rich.progress import Progress, SpinnerColumn, TextColumn

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

# ─────────────────────────────────────────────────────────────────────
# Constants
# ─────────────────────────────────────────────────────────────────────

# Mirror of the Olist dataset (public, no auth needed)
DATASET_URL = (
    "https://github.com/olist/work-at-olist-data/raw/master/"
    "datasets/olist_public_dataset.zip"
)

# Fallback: individual CSV URLs (if zip fails)
FALLBACK_BASE = (
    "https://raw.githubusercontent.com/olist/work-at-olist-data/"
    "master/datasets/"
)

# CSV filename → DuckDB table name mapping
CSV_TABLE_MAP: dict[str, str] = {
    "olist_customers_dataset.csv": "customers",
    "olist_orders_dataset.csv": "orders",
    "olist_order_items_dataset.csv": "order_items",
    "olist_order_payments_dataset.csv": "order_payments",
    "olist_order_reviews_dataset.csv": "order_reviews",
    "olist_products_dataset.csv": "products",
    "olist_sellers_dataset.csv": "sellers",
    "product_category_name_translation.csv": "product_category",
    "olist_geolocation_dataset.csv": "geolocation",
}

# Project root is two levels up from this file
DATA_DIR = Path(__file__).resolve().parent
DB_PATH = DATA_DIR / "analytics.duckdb"

console = Console(legacy_windows=False)


# ─────────────────────────────────────────────────────────────────────
# DDL: Schema definitions with proper types
# ─────────────────────────────────────────────────────────────────────

SCHEMA_DDL: dict[str, str] = {
    "customers": """
        CREATE TABLE IF NOT EXISTS customers (
            customer_id              VARCHAR PRIMARY KEY,
            customer_unique_id       VARCHAR NOT NULL,
            customer_zip_code_prefix VARCHAR,
            customer_city            VARCHAR,
            customer_state           VARCHAR(2)
        )
    """,
    "orders": """
        CREATE TABLE IF NOT EXISTS orders (
            order_id                      VARCHAR PRIMARY KEY,
            customer_id                   VARCHAR NOT NULL,
            order_status                  VARCHAR NOT NULL,
            order_purchase_timestamp      TIMESTAMP,
            order_approved_at             TIMESTAMP,
            order_delivered_carrier_date   TIMESTAMP,
            order_delivered_customer_date  TIMESTAMP,
            order_estimated_delivery_date  TIMESTAMP
        )
    """,
    "order_items": """
        CREATE TABLE IF NOT EXISTS order_items (
            order_id            VARCHAR NOT NULL,
            order_item_id       INTEGER NOT NULL,
            product_id          VARCHAR NOT NULL,
            seller_id           VARCHAR NOT NULL,
            shipping_limit_date TIMESTAMP,
            price               DOUBLE NOT NULL,
            freight_value       DOUBLE NOT NULL,
            PRIMARY KEY (order_id, order_item_id)
        )
    """,
    "order_payments": """
        CREATE TABLE IF NOT EXISTS order_payments (
            order_id             VARCHAR NOT NULL,
            payment_sequential   INTEGER NOT NULL,
            payment_type         VARCHAR NOT NULL,
            payment_installments INTEGER,
            payment_value        DOUBLE NOT NULL
        )
    """,
    "order_reviews": """
        CREATE TABLE IF NOT EXISTS order_reviews (
            review_id              VARCHAR,
            order_id               VARCHAR NOT NULL,
            review_score           INTEGER,
            review_comment_title   VARCHAR,
            review_comment_message VARCHAR,
            review_creation_date   TIMESTAMP,
            review_answer_timestamp TIMESTAMP
        )
    """,
    "products": """
        CREATE TABLE IF NOT EXISTS products (
            product_id                  VARCHAR PRIMARY KEY,
            product_category_name       VARCHAR,
            product_name_lenght         INTEGER,
            product_description_lenght  INTEGER,
            product_photos_qty          INTEGER,
            product_weight_g            INTEGER,
            product_length_cm           INTEGER,
            product_height_cm           INTEGER,
            product_width_cm            INTEGER
        )
    """,
    "sellers": """
        CREATE TABLE IF NOT EXISTS sellers (
            seller_id              VARCHAR PRIMARY KEY,
            seller_zip_code_prefix VARCHAR,
            seller_city            VARCHAR,
            seller_state           VARCHAR(2)
        )
    """,
    "product_category": """
        CREATE TABLE IF NOT EXISTS product_category (
            product_category_name         VARCHAR PRIMARY KEY,
            product_category_name_english VARCHAR
        )
    """,
    "geolocation": """
        CREATE TABLE IF NOT EXISTS geolocation (
            geolocation_zip_code_prefix VARCHAR,
            geolocation_lat             DOUBLE,
            geolocation_lng             DOUBLE,
            geolocation_city            VARCHAR,
            geolocation_state           VARCHAR(2)
        )
    """,
}

# Indices for common analytical query patterns
INDICES_DDL: list[str] = [
    "CREATE INDEX IF NOT EXISTS idx_orders_customer ON orders(customer_id)",
    "CREATE INDEX IF NOT EXISTS idx_orders_status ON orders(order_status)",
    "CREATE INDEX IF NOT EXISTS idx_orders_purchase ON orders(order_purchase_timestamp)",
    "CREATE INDEX IF NOT EXISTS idx_order_items_order ON order_items(order_id)",
    "CREATE INDEX IF NOT EXISTS idx_order_items_product ON order_items(product_id)",
    "CREATE INDEX IF NOT EXISTS idx_order_items_seller ON order_items(seller_id)",
    "CREATE INDEX IF NOT EXISTS idx_payments_order ON order_payments(order_id)",
    "CREATE INDEX IF NOT EXISTS idx_reviews_order ON order_reviews(order_id)",
    "CREATE INDEX IF NOT EXISTS idx_products_category ON products(product_category_name)",
    "CREATE INDEX IF NOT EXISTS idx_geo_zip ON geolocation(geolocation_zip_code_prefix)",
]


# ─────────────────────────────────────────────────────────────────────
# Download & Load
# ─────────────────────────────────────────────────────────────────────


def download_dataset_zip(url: str, timeout: float = 120.0) -> dict[str, bytes]:
    console.print(f"[bold blue]Downloading dataset from:[/] {url}")

    with httpx.Client(timeout=timeout, follow_redirects=True) as client:
        response = client.get(url)
        response.raise_for_status()

    csv_files: dict[str, bytes] = {}
    with zipfile.ZipFile(io.BytesIO(response.content)) as zf:
        for name in zf.namelist():
            if name.endswith(".csv"):
                # Extract just the filename (ignore subdirectory paths)
                basename = Path(name).name
                csv_files[basename] = zf.read(name)

    console.print(f"[green]✓[/] Downloaded {len(csv_files)} CSV files from archive")
    return csv_files


def download_individual_csvs(
    base_url: str, filenames: list[str], timeout: float = 60.0
) -> dict[str, bytes]:
    csv_files: dict[str, bytes] = {}
    with httpx.Client(timeout=timeout, follow_redirects=True) as client:
        for fname in filenames:
            url = f"{base_url}{fname}"
            console.print(f"  Downloading {fname}...")
            try:
                response = client.get(url)
                response.raise_for_status()
                csv_files[fname] = response.content
            except httpx.HTTPError as e:
                console.print(f"  [yellow]⚠ Failed to download {fname}: {e}[/]")
    return csv_files


def load_csvs_into_duckdb(
    db_path: Path,
    csv_data: dict[str, bytes],
    table_map: dict[str, str],
) -> None:
    # Remove existing database to ensure clean state
    if db_path.exists():
        console.print(f"[yellow]Removing existing database: {db_path}[/]")
        db_path.unlink()

    console.print(f"[bold blue]Creating database:[/] {db_path}")
    conn = duckdb.connect(str(db_path))
    tmp_files: list[Path] = []

    try:
        # Create all tables
        for table_name, ddl in SCHEMA_DDL.items():
            conn.execute(ddl)
            console.print(f"  [dim]Created table:[/] {table_name}")

        # Load data
        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            console=console,
        ) as progress:
            for csv_filename, table_name in table_map.items():
                if csv_filename not in csv_data:
                    console.print(
                        f"  [yellow]⚠ Skipping {table_name}: "
                        f"{csv_filename} not found in download[/]"
                    )
                    continue

                task = progress.add_task(f"Loading {table_name}...", total=None)

                # Write CSV bytes to a temp file for DuckDB's native reader
                tmp_csv = db_path.parent / f"_tmp_{csv_filename}"
                tmp_files.append(tmp_csv)
                try:
                    tmp_csv.write_bytes(csv_data[csv_filename])

                    # Use COPY for bulk loading — much faster than INSERT
                    conn.execute(
                        f"""
                        INSERT INTO {table_name}
                        SELECT * FROM read_csv_auto(
                            '{tmp_csv.as_posix()}',
                            header=true,
                            ignore_errors=true,
                            sample_size=10000
                        )
                        """
                    )

                    # Get row count
                    count = conn.execute(
                        f"SELECT COUNT(*) FROM {table_name}"
                    ).fetchone()[0]
                    progress.update(task, description=f"[OK] {table_name}: {count:,} rows")

                finally:
                    if tmp_csv.exists():
                        try:
                            tmp_csv.unlink()
                        except OSError:
                            pass

        # Create indices
        console.print("\n[bold blue]Creating analytical indices...[/]")
        for idx_ddl in INDICES_DDL:
            conn.execute(idx_ddl)
        console.print("[green][OK][/] All indices created")

        # Print summary
        console.print("\n[bold green]=== Database Seeded Successfully ===[/]\n")
        summary = conn.execute("""
            SELECT table_name, estimated_size, column_count
            FROM duckdb_tables()
            ORDER BY table_name
        """).fetchdf()
        console.print(summary.to_string(index=False))

    finally:
        conn.close()
        for tmp in tmp_files:
            try:
                if tmp.exists():
                    tmp.unlink()
            except OSError:
                pass


# ─────────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────────


def main() -> None:
    console.print(
        "\n[bold]=== Olist E-Commerce Dataset Seeder ===[/]\n"
    )

    # Ensure data directory exists
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    # Attempt zip download first, fall back to individual CSVs
    try:
        csv_data = download_dataset_zip(DATASET_URL)
    except (httpx.HTTPError, zipfile.BadZipFile) as e:
        console.print(f"[yellow][!] Zip download failed ({e}), trying individual CSVs...[/]")
        csv_data = download_individual_csvs(
            FALLBACK_BASE, list(CSV_TABLE_MAP.keys())
        )

    if not csv_data:
        console.print("[red][X] No CSV data downloaded. Aborting.[/]")
        sys.exit(1)

    # Load into DuckDB
    load_csvs_into_duckdb(DB_PATH, csv_data, CSV_TABLE_MAP)

    # Verify database opens in read-only mode
    console.print("\n[bold blue]Verifying read-only access...[/]")
    verify_conn = duckdb.connect(str(DB_PATH), read_only=True)
    try:
        tables = verify_conn.execute(
            "SELECT table_name FROM information_schema.tables ORDER BY table_name"
        ).fetchall()
        console.print(f"[green][OK][/] Read-only verification passed: {len(tables)} tables accessible")
    finally:
        verify_conn.close()

    console.print(f"\n[bold green]Done![/] Database at: {DB_PATH}\n")


if __name__ == "__main__":
    main()
