# Largest Banks ETL

A Python ETL pipeline that extracts the world's ten largest publicly traded banks by market capitalization, converts USD market caps to GBP, EUR, and INR, and loads the results into both CSV and SQLite.

## Data sources

- Bank ranking and USD market capitalization: [CompaniesMarketCap](https://companiesmarketcap.com/banks/largest-banks-by-market-cap/)
- Foreign exchange reference rates: [European Central Bank](https://www.ecb.europa.eu/stats/policy_and_exchange_rates/euro_reference_exchange_rates/html/index.en.html), dated 16 July 2026

The ECB quotes currencies per euro. The checked-in [`data/exchange_rates.csv`](data/exchange_rates.csv) normalizes those values to target-currency units per USD using:

```text
target currency per USD = target currency per EUR / USD per EUR
```

ECB inputs for 16 July 2026 were USD `1.1467`, GBP `0.84873`, EUR `1.0`, and INR `110.4895` per EUR. Reference rates are informational and are not intended for transaction settlement.

Market capitalization changes with security prices. Each pipeline run records its UTC extraction timestamp in `Data_As_Of_UTC`.

## Pipeline

```text
CompaniesMarketCap HTML ── extract ──> top 10 USD values
                                             │
exchange_rates.csv ─────── transform ────────┤
                                             ├──> output/largest_banks.csv
                                             └──> output/banks.db :: Largest_banks
```

## Run locally

Python 3.10 or newer is recommended.

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python src/bank_etl.py
```

Generated artifacts:

- `output/largest_banks.csv`
- `output/banks.db`, table `Largest_banks`
- `logs/etl_pipeline.log`

Inspect the database:

```bash
sqlite3 -header -column output/banks.db \
  'SELECT Rank, Name, MC_USD_Billion, MC_GBP_Billion, MC_EUR_Billion, MC_INR_Billion FROM Largest_banks ORDER BY Rank;'
```

Optional paths and source URL can be changed with command-line flags:

```bash
python src/bank_etl.py --help
```

## Test and lint

```bash
pip install -r requirements-dev.txt
ruff check .
pytest -q
```

## Output schema

| Column | Meaning |
|---|---|
| `Rank` | Rank by market capitalization |
| `Name` | Bank or bank holding company |
| `Ticker` | Source listing ticker |
| `Country` | Source country |
| `MC_USD_Billion` | Market cap in USD billions |
| `MC_GBP_Billion` | Market cap in GBP billions |
| `MC_EUR_Billion` | Market cap in EUR billions |
| `MC_INR_Billion` | Market cap in INR billions |
| `Data_As_Of_UTC` | UTC extraction timestamp |

## Notes

- The database load replaces the table transactionally, making reruns idempotent.
- HTTP retries handle transient source failures.
- Structural and row-count validation fails fast if the upstream HTML changes.
- Values are rounded to two decimal places with decimal half-up rounding.
