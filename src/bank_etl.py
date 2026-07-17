"""ETL pipeline for the world's ten largest publicly traded banks."""

from __future__ import annotations

import argparse
import csv
import logging
import re
import sqlite3
from collections.abc import Iterable
from datetime import datetime, timezone
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from pathlib import Path

import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

SOURCE_URL = "https://companiesmarketcap.com/banks/largest-banks-by-market-cap/"
REQUIRED_CURRENCIES = ("GBP", "EUR", "INR")
OUTPUT_FIELDS = (
    "Rank",
    "Name",
    "Ticker",
    "Country",
    "MC_USD_Billion",
    "MC_GBP_Billion",
    "MC_EUR_Billion",
    "MC_INR_Billion",
    "Data_As_Of_UTC",
)
LOGGER = logging.getLogger("bank_etl")


def build_session() -> requests.Session:
    """Return an HTTP session with retries for transient source failures."""
    retry = Retry(
        total=3,
        backoff_factor=0.5,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=("GET",),
    )
    session = requests.Session()
    session.headers.update(
        {
            "User-Agent": (
                "Mozilla/5.0 (compatible; LargestBanksETL/1.0; "
                "+https://github.com/felmonon/largest-banks-etl)"
            )
        }
    )
    session.mount("https://", HTTPAdapter(max_retries=retry))
    return session


def extract_html(source_url: str, timeout: int = 30) -> str:
    """Download the market-cap ranking page."""
    LOGGER.info("Extracting bank rankings from %s", source_url)
    response = build_session().get(source_url, timeout=timeout)
    response.raise_for_status()
    return response.text


def _displayed_market_cap_to_billions(text: str) -> Decimal:
    match = re.search(r"([\d,.]+)\s*([TBMK])", text.upper())
    if not match:
        raise ValueError(f"Unsupported market-cap value: {text!r}")

    amount = Decimal(match.group(1).replace(",", ""))
    factors = {
        "T": Decimal("1000"),
        "B": Decimal("1"),
        "M": Decimal("0.001"),
        "K": Decimal("0.000001"),
    }
    return amount * factors[match.group(2)]


def parse_banks(html: str, limit: int = 10, extracted_at: str | None = None) -> list[dict]:
    """Parse ranked bank rows from CompaniesMarketCap HTML."""
    soup = BeautifulSoup(html, "html.parser")
    rows = soup.select("table.marketcap-table tbody tr")
    if not rows:
        raise ValueError("Could not find the market-cap ranking table")

    as_of = extracted_at or datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    banks: list[dict] = []

    for row in rows:
        rank_cell = row.select_one(".rank-td")
        name_cell = row.select_one(".company-name")
        ticker_cell = row.select_one(".company-code")
        cells = row.find_all("td")
        if not rank_cell or not name_cell or not ticker_cell or len(cells) < 8:
            continue

        try:
            rank = int(rank_cell.get_text(strip=True))
        except ValueError:
            continue
        if rank > limit:
            continue

        market_cap_cell = cells[3]
        raw_market_cap = market_cap_cell.get("data-sort")
        try:
            if raw_market_cap:
                market_cap_billion = Decimal(raw_market_cap) / Decimal("1000000000")
            else:
                market_cap_billion = _displayed_market_cap_to_billions(
                    market_cap_cell.get_text(" ", strip=True)
                )
        except (InvalidOperation, ValueError) as exc:
            raise ValueError(f"Invalid market cap for rank {rank}") from exc

        country = cells[-1].get_text(" ", strip=True).encode("ascii", "ignore").decode().strip()
        banks.append(
            {
                "Rank": rank,
                "Name": name_cell.get_text(" ", strip=True),
                "Ticker": ticker_cell.get_text(" ", strip=True),
                "Country": country,
                "MC_USD_Billion": market_cap_billion,
                "Data_As_Of_UTC": as_of,
            }
        )

    banks.sort(key=lambda item: item["Rank"])
    if len(banks) != limit or [bank["Rank"] for bank in banks] != list(range(1, limit + 1)):
        raise ValueError(f"Expected ranks 1-{limit}; parsed {len(banks)} valid rows")
    LOGGER.info("Extracted %d ranked banks", len(banks))
    return banks


def load_exchange_rates(csv_path: Path) -> dict[str, Decimal]:
    """Read target-currency units per USD from a CSV file."""
    LOGGER.info("Reading exchange rates from %s", csv_path)
    with csv_path.open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        if not reader.fieldnames or not {"Currency", "Rate"}.issubset(reader.fieldnames):
            raise ValueError("Exchange-rate CSV must contain Currency and Rate columns")
        rates = {
            row["Currency"].strip().upper(): Decimal(row["Rate"].strip())
            for row in reader
            if row.get("Currency") and row.get("Rate")
        }

    missing = set(REQUIRED_CURRENCIES) - rates.keys()
    if missing:
        raise ValueError(f"Missing required exchange rates: {', '.join(sorted(missing))}")
    if any(rates[code] <= 0 for code in REQUIRED_CURRENCIES):
        raise ValueError("Exchange rates must be positive")
    return rates


def transform(banks: Iterable[dict], rates: dict[str, Decimal]) -> list[dict]:
    """Convert USD market caps to GBP, EUR and INR billions."""
    cents = Decimal("0.01")
    transformed: list[dict] = []
    for bank in banks:
        usd = Decimal(bank["MC_USD_Billion"]).quantize(cents, rounding=ROUND_HALF_UP)
        record = dict(bank)
        record["MC_USD_Billion"] = usd
        for currency in REQUIRED_CURRENCIES:
            record[f"MC_{currency}_Billion"] = (usd * rates[currency]).quantize(
                cents, rounding=ROUND_HALF_UP
            )
        transformed.append(record)
    LOGGER.info("Transformed market caps into GBP, EUR and INR")
    return transformed


def load_csv(records: Iterable[dict], output_path: Path) -> None:
    """Write transformed records to CSV."""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=OUTPUT_FIELDS)
        writer.writeheader()
        writer.writerows({field: record[field] for field in OUTPUT_FIELDS} for record in records)
    LOGGER.info("Loaded CSV output to %s", output_path)


def load_database(records: Iterable[dict], database_path: Path, table_name: str) -> None:
    """Replace a SQLite table with transformed records."""
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", table_name):
        raise ValueError("Table name must be a valid SQL identifier")

    database_path.parent.mkdir(parents=True, exist_ok=True)
    quoted_table = f'"{table_name}"'
    rows = [tuple(str(record[field]) for field in OUTPUT_FIELDS) for record in records]
    with sqlite3.connect(database_path) as connection:
        connection.execute(f"DROP TABLE IF EXISTS {quoted_table}")
        connection.execute(
            f"""
            CREATE TABLE {quoted_table} (
                Rank INTEGER PRIMARY KEY,
                Name TEXT NOT NULL,
                Ticker TEXT NOT NULL,
                Country TEXT NOT NULL,
                MC_USD_Billion REAL NOT NULL,
                MC_GBP_Billion REAL NOT NULL,
                MC_EUR_Billion REAL NOT NULL,
                MC_INR_Billion REAL NOT NULL,
                Data_As_Of_UTC TEXT NOT NULL
            )
            """
        )
        placeholders = ", ".join("?" for _ in OUTPUT_FIELDS)
        columns = ", ".join(f'"{field}"' for field in OUTPUT_FIELDS)
        connection.executemany(
            f"INSERT INTO {quoted_table} ({columns}) VALUES ({placeholders})",
            rows,
        )
    LOGGER.info("Loaded %d rows into %s table %s", len(rows), database_path, table_name)


def configure_logging(log_path: Path) -> None:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(message)s",
        handlers=[logging.FileHandler(log_path, encoding="utf-8"), logging.StreamHandler()],
        force=True,
    )


def project_root() -> Path:
    return Path(__file__).resolve().parents[1]


def parse_args() -> argparse.Namespace:
    root = project_root()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-url", default=SOURCE_URL)
    parser.add_argument("--exchange-rates", type=Path, default=root / "data/exchange_rates.csv")
    parser.add_argument("--output-csv", type=Path, default=root / "output/largest_banks.csv")
    parser.add_argument("--database", type=Path, default=root / "output/banks.db")
    parser.add_argument("--table", default="Largest_banks")
    parser.add_argument("--log-file", type=Path, default=root / "logs/etl_pipeline.log")
    return parser.parse_args()


def run_pipeline(args: argparse.Namespace) -> list[dict]:
    configure_logging(args.log_file)
    LOGGER.info("Starting largest-banks ETL pipeline")
    html = extract_html(args.source_url)
    banks = parse_banks(html)
    rates = load_exchange_rates(args.exchange_rates)
    records = transform(banks, rates)
    load_csv(records, args.output_csv)
    load_database(records, args.database, args.table)
    LOGGER.info("ETL pipeline completed successfully")
    return records


def main() -> int:
    try:
        run_pipeline(parse_args())
    except Exception:
        LOGGER.exception("ETL pipeline failed")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
