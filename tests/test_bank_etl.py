import sqlite3
from decimal import Decimal

import pytest

from src.bank_etl import load_database, load_exchange_rates, parse_banks, transform


def sample_html() -> str:
    rows = []
    for rank, name, ticker, market_cap in [
        (1, "Alpha Bank", "ALP", "500000000000"),
        (2, "Beta Bank", "BET", "250000000000"),
    ]:
        rows.append(
            f"""
            <tr>
              <td class="fav"></td>
              <td class="rank-td" data-sort="{rank}">{rank}</td>
              <td class="name-td"><div class="company-name">{name}</div>
                <div class="company-code">{ticker}</div></td>
              <td class="td-right" data-sort="{market_cap}">$ {int(market_cap) / 1e9:.2f} B</td>
              <td></td><td></td><td></td><td>🇺🇸 USA</td>
            </tr>
            """
        )
    return '<table class="marketcap-table"><tbody>' + "".join(rows) + "</tbody></table>"


def test_parse_banks_extracts_ranked_rows():
    banks = parse_banks(sample_html(), limit=2, extracted_at="2026-07-16T12:00:00+00:00")

    assert [bank["Name"] for bank in banks] == ["Alpha Bank", "Beta Bank"]
    assert banks[0]["MC_USD_Billion"] == Decimal("500")
    assert banks[0]["Country"] == "USA"


def test_exchange_rates_and_transform(tmp_path):
    rates_csv = tmp_path / "rates.csv"
    rates_csv.write_text("Currency,Rate\nGBP,0.8\nEUR,0.9\nINR,80\n", encoding="utf-8")
    rates = load_exchange_rates(rates_csv)
    banks = parse_banks(sample_html(), limit=2)

    records = transform(banks, rates)

    assert records[0]["MC_GBP_Billion"] == Decimal("400.00")
    assert records[0]["MC_EUR_Billion"] == Decimal("450.00")
    assert records[0]["MC_INR_Billion"] == Decimal("40000.00")


def test_missing_rate_is_rejected(tmp_path):
    rates_csv = tmp_path / "rates.csv"
    rates_csv.write_text("Currency,Rate\nGBP,0.8\nEUR,0.9\n", encoding="utf-8")

    with pytest.raises(ValueError, match="INR"):
        load_exchange_rates(rates_csv)


def test_database_load_replaces_table(tmp_path):
    rates = {"GBP": Decimal("0.8"), "EUR": Decimal("0.9"), "INR": Decimal("80")}
    records = transform(parse_banks(sample_html(), limit=2), rates)
    database = tmp_path / "banks.db"

    load_database(records, database, "Largest_banks")

    with sqlite3.connect(database) as connection:
        count = connection.execute("SELECT COUNT(*) FROM Largest_banks").fetchone()[0]
        leader = connection.execute(
            "SELECT Name, MC_USD_Billion FROM Largest_banks ORDER BY Rank LIMIT 1"
        ).fetchone()
    assert count == 2
    assert leader == ("Alpha Bank", 500.0)
