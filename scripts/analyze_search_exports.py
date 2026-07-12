#!/usr/bin/env python3
"""Build a compact JSON summary from GSC and Yandex Metrica exports."""

from __future__ import annotations

import argparse
import json
import warnings
from pathlib import Path
from typing import Any

import pandas as pd


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-dir", type=Path, default=Path("data_for_audit"))
    parser.add_argument("--gsc", type=Path)
    parser.add_argument("--metrika", type=Path)
    parser.add_argument(
        "--output", type=Path, default=Path("reports/generated/analytics_summary.json")
    )
    return parser.parse_args()


def find_one(directory: Path, pattern: str) -> Path:
    matches = sorted(directory.glob(pattern))
    if len(matches) != 1:
        raise SystemExit(
            f"Expected one file for {pattern!r} in {directory}, found {len(matches)}"
        )
    return matches[0]


def native(value: Any) -> Any:
    if pd.isna(value):
        return None
    if hasattr(value, "item"):
        return value.item()
    return value


def gsc_sheet(path: Path, sheet_name: str) -> pd.DataFrame:
    frame = pd.read_excel(path, sheet_name=sheet_name)
    frame.columns = [str(column).replace("Kлики", "Клики").strip() for column in frame]
    return frame


def aggregate_period(frame: pd.DataFrame) -> dict[str, float | int]:
    impressions = int(frame["Показы"].sum())
    clicks = int(frame["Клики"].sum())
    return {
        "clicks": clicks,
        "impressions": impressions,
        "ctr": clicks / impressions if impressions else 0.0,
        "weighted_position": float(
            (frame["Позиция"] * frame["Показы"]).sum() / impressions
        ) if impressions else 0.0,
    }


def compare_periods(
    daily: pd.DataFrame, days: int, data_lag_days: int = 3
) -> dict[str, Any]:
    end = daily["Дата"].max() - pd.Timedelta(days=data_lag_days)
    current = daily[
        (daily["Дата"] > end - pd.Timedelta(days=days)) & (daily["Дата"] <= end)
    ]
    previous = daily[
        (daily["Дата"] > end - pd.Timedelta(days=days * 2))
        & (daily["Дата"] <= end - pd.Timedelta(days=days))
    ]
    current_values = aggregate_period(current)
    previous_values = aggregate_period(previous)
    changes = {
        key: current_values[key] / previous_values[key] - 1
        for key in ("clicks", "impressions", "ctr")
        if previous_values[key]
    }
    changes["weighted_position_points"] = (
        current_values["weighted_position"] - previous_values["weighted_position"]
    )
    return {
        "days": days,
        "current_range": [str(current["Дата"].min().date()), str(current["Дата"].max().date())],
        "previous_range": [str(previous["Дата"].min().date()), str(previous["Дата"].max().date())],
        "current": current_values,
        "previous": previous_values,
        "changes": changes,
    }


def opportunity_rows(frame: pd.DataFrame, limit: int = 30) -> list[dict[str, Any]]:
    key = frame.columns[0]
    candidates = frame[
        (frame["Показы"] >= frame["Показы"].quantile(0.75))
        & frame["Позиция"].between(4, 20)
    ].copy()
    candidates["estimated_click_gap_at_8pct_ctr"] = (
        candidates["Показы"] * (0.08 - candidates["CTR"]).clip(lower=0)
    ).round()
    columns = [
        key,
        "Клики",
        "Показы",
        "CTR",
        "Позиция",
        "estimated_click_gap_at_8pct_ctr",
    ]
    rows = candidates.sort_values(
        ["estimated_click_gap_at_8pct_ctr", "Показы"], ascending=False
    )[columns].head(limit)
    return [
        {str(column): native(value) for column, value in row.items()}
        for _, row in rows.iterrows()
    ]


def main() -> None:
    warnings.filterwarnings("ignore", message="Workbook contains no default style")
    args = parse_args()
    gsc_path = args.gsc or find_one(args.data_dir, "*Google-Search*.xlsx")
    metrika_path = args.metrika or find_one(args.data_dir, "*metrika*.xlsx")

    daily = gsc_sheet(gsc_path, "Диаграмма")
    daily["Дата"] = pd.to_datetime(daily["Дата"])
    queries = gsc_sheet(gsc_path, "Запросы")
    pages = gsc_sheet(gsc_path, "Страницы")
    devices = gsc_sheet(gsc_path, "Устройства")
    countries = gsc_sheet(gsc_path, "Страны")

    end = daily["Дата"].max() - pd.Timedelta(days=3)
    current_yoy = daily[
        (daily["Дата"] > end - pd.Timedelta(days=28)) & (daily["Дата"] <= end)
    ]
    previous_yoy = daily[
        (daily["Дата"] > end - pd.Timedelta(days=393))
        & (daily["Дата"] <= end - pd.Timedelta(days=365))
    ]
    current_yoy_values = aggregate_period(current_yoy)
    previous_yoy_values = aggregate_period(previous_yoy)

    metrika = pd.read_excel(metrika_path, header=4)
    metrika_total = metrika.iloc[0]
    metrika_rows = metrika.iloc[1:].copy()
    undefined = metrika_rows["Поисковая фраза"].fillna("").eq("Не определено")
    engines = (
        metrika_rows.groupby("Поисковая система", dropna=False)[
            [
                "Визиты",
                "Посетители",
                "Достижения избранных целей",
                "Доход по избранным целям, RUB",
            ]
        ]
        .sum()
        .sort_values("Визиты", ascending=False)
        .head(10)
        .reset_index()
    )

    summary = {
        "source_files": {"gsc": gsc_path.name, "metrika": metrika_path.name},
        "gsc": {
            "date_range": [str(daily["Дата"].min().date()), str(daily["Дата"].max().date())],
            "days": len(daily),
            "total": aggregate_period(daily),
            "comparisons": [compare_periods(daily, 28), compare_periods(daily, 90)],
            "year_over_year_28_days": {
                "current_range": [str(current_yoy["Дата"].min().date()), str(current_yoy["Дата"].max().date())],
                "previous_range": [str(previous_yoy["Дата"].min().date()), str(previous_yoy["Дата"].max().date())],
                "current": current_yoy_values,
                "previous": previous_yoy_values,
                "changes": {
                    key: current_yoy_values[key] / previous_yoy_values[key] - 1
                    for key in ("clicks", "impressions", "ctr")
                    if previous_yoy_values[key]
                },
            },
            "ui_export_limits": {"query_rows": len(queries), "page_rows": len(pages)},
            "query_opportunities": opportunity_rows(queries),
            "page_opportunities": opportunity_rows(pages),
            "devices": devices.to_dict(orient="records"),
            "top_countries": countries.head(15).to_dict(orient="records"),
        },
        "metrika": {
            "export_rows": len(metrika),
            "total": {str(key): native(value) for key, value in metrika_total.items()},
            "engines": engines.to_dict(orient="records"),
            "undefined_query_visits": int(metrika_rows.loc[undefined, "Визиты"].sum()),
            "undefined_query_share": float(
                metrika_rows.loc[undefined, "Визиты"].sum()
                / metrika_rows["Визиты"].sum()
            ),
        },
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"Saved: {args.output}")


if __name__ == "__main__":
    main()
