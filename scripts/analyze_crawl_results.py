#!/usr/bin/env python3
"""Summarize technical crawl results and export actionable issue rows."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

import pandas as pd


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("input", type=Path)
    parser.add_argument(
        "--summary", type=Path, default=Path("reports/generated/crawl_analysis.json")
    )
    parser.add_argument(
        "--issues", type=Path, default=Path("reports/generated/crawl_issues.csv.gz")
    )
    return parser.parse_args()


def count_and_share(mask: pd.Series, total: int) -> dict[str, int | float]:
    count = int(mask.fillna(False).sum())
    return {"count": count, "share": count / total if total else 0.0}


def duplicate_stats(frame: pd.DataFrame, column: str) -> dict[str, int]:
    values = frame[column].fillna("").astype(str).str.strip()
    counts = values[values.ne("")].value_counts()
    duplicates = counts[counts > 1]
    return {
        "groups": int(len(duplicates)),
        "urls": int(duplicates.sum()),
        "largest_group": int(duplicates.max()) if len(duplicates) else 0,
    }


def native_records(frame: pd.DataFrame, limit: int = 50) -> list[dict[str, Any]]:
    return json.loads(frame.head(limit).to_json(orient="records", force_ascii=False))


def main() -> None:
    args = parse_args()
    frame = pd.read_csv(args.input)
    total = len(frame)
    html = frame[frame["content_type"].fillna("").str.contains("html", case=False)]
    ok_html = html[html["status"].eq(200)].copy()
    ok_html_mask = frame["status"].eq(200) & frame["content_type"].fillna("").str.contains(
        "html", case=False
    )

    issues: dict[str, pd.Series] = {
        "non_200": frame["status"].ne(200),
        "redirected": frame["redirect_count"].fillna(0).gt(0),
        "noindex": frame["noindex"].fillna(False).eq(True),
        "missing_title": ok_html_mask & frame["title"].fillna("").eq(""),
        "missing_description": ok_html_mask
        & frame["description"].fillna("").eq(""),
        "missing_h1": ok_html_mask & frame["h1_count"].fillna(0).eq(0),
        "multiple_h1": ok_html_mask & frame["h1_count"].fillna(0).gt(1),
        "missing_canonical": ok_html_mask
        & frame["canonical"].fillna("").eq(""),
        "non_self_canonical": ok_html_mask
        & frame["canonical_is_self"].astype("boolean").fillna(True).eq(False),
        "soft_404_hint": frame["soft_404_hint"].fillna(False).eq(True),
        "slow_over_3s": frame["response_ms"].fillna(0).gt(3000),
        "html_over_750kb": frame["response_bytes"].fillna(0).gt(750_000),
    }
    for column, issue_name in (
        ("title", "duplicate_title"),
        ("description", "duplicate_description"),
        ("h1", "duplicate_h1"),
    ):
        normalized = frame[column].fillna("").astype(str).str.strip()
        ok_values = normalized.where(ok_html_mask, "")
        issues[issue_name] = ok_values.ne("") & ok_values.duplicated(keep=False)

    issue_names = pd.Series("", index=frame.index, dtype="object")
    for issue_name, mask in issues.items():
        issue_names.loc[mask] = issue_names.loc[mask].map(
            lambda current: f"{current}|{issue_name}" if current else issue_name
        )
    issue_rows = frame[issue_names.ne("")].copy()
    issue_rows.insert(0, "issues", issue_names[issue_names.ne("")])

    response_quantiles = ok_html["response_ms"].quantile([0.5, 0.75, 0.9, 0.95, 1.0])
    size_quantiles = ok_html["response_bytes"].quantile([0.5, 0.9, 0.95, 1.0])
    summary = {
        "input": str(args.input),
        "rows": total,
        "html_rows": len(html),
        "ok_html_rows": len(ok_html),
        "statuses": {
            str(key): int(value)
            for key, value in frame["status"].fillna("error").value_counts().items()
        },
        "issues": {
            issue_name: count_and_share(mask, total)
            for issue_name, mask in issues.items()
        },
        "duplicate_metadata": {
            column: duplicate_stats(ok_html, column)
            for column in ("title", "description", "h1")
        },
        "title_length": {
            "over_65": count_and_share(ok_html["title_length"].gt(65), len(ok_html)),
            "under_30": count_and_share(ok_html["title_length"].lt(30), len(ok_html)),
        },
        "description_length": {
            "over_160": count_and_share(
                ok_html["description_length"].gt(160), len(ok_html)
            ),
            "under_70": count_and_share(
                ok_html["description_length"].lt(70), len(ok_html)
            ),
        },
        "response_ms_quantiles": {
            str(key): float(value) for key, value in response_quantiles.items()
        },
        "response_bytes_quantiles": {
            str(key): float(value) for key, value in size_quantiles.items()
        },
        "issue_rows": len(issue_rows),
        "non_200_examples": native_records(
            frame.loc[issues["non_200"], ["url", "status", "final_url", "redirect_chain"]]
        ),
        "noindex_examples": native_records(
            frame.loc[issues["noindex"], ["url", "status", "meta_robots", "canonical"]]
        ),
    }

    args.summary.parent.mkdir(parents=True, exist_ok=True)
    args.issues.parent.mkdir(parents=True, exist_ok=True)
    args.summary.write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    issue_rows.to_csv(args.issues, index=False, compression="gzip")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    print(f"Saved: {args.summary}")
    print(f"Saved: {args.issues}")


if __name__ == "__main__":
    main()
