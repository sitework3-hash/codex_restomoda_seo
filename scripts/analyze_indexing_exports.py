#!/usr/bin/env python3
"""Cross-check Google/Yandex indexing exports against sitemaps and crawl data."""

from __future__ import annotations

import argparse
import json
import re
import warnings
from collections import Counter
from pathlib import Path
from typing import Any, Iterator
from urllib.parse import urlsplit, urlunsplit
from xml.etree.ElementTree import iterparse
from zipfile import ZipFile

import pandas as pd


CELL_REF = re.compile(r"([A-Z]+)")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-dir", type=Path, default=Path("data_for_audit"))
    parser.add_argument(
        "--sitemap-urls",
        type=Path,
        default=Path("reports/generated/sitemap_urls.csv.gz"),
    )
    parser.add_argument(
        "--filter-crawl",
        type=Path,
        default=Path("reports/generated/seo_filters_full.csv.gz"),
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("reports/generated/indexing_analysis.json"),
    )
    parser.add_argument(
        "--issues",
        type=Path,
        default=Path("reports/generated/indexing_cross_engine_issues.csv.gz"),
    )
    return parser.parse_args()


def find_one(directory: Path, pattern: str) -> Path:
    matches = sorted(directory.glob(pattern))
    if len(matches) != 1:
        raise SystemExit(
            f"Expected one file for {pattern!r} in {directory}, found {len(matches)}"
        )
    return matches[0]


def local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def first_text(element: Any, name: str) -> str | None:
    for child in element.iter():
        if local_name(child.tag) == name:
            return child.text
    return None


def xlsx_rows(path: Path) -> Iterator[dict[str, str | None]]:
    """Stream the first XLSX sheet, ignoring incorrect dimension metadata.

    Yandex WebMaster exports currently declare dimension A1 even when tens of
    thousands of rows are present. openpyxl read-only mode therefore sees one
    row; parsing sheet XML is both reliable and memory efficient.
    """

    with ZipFile(path) as archive:
        shared: list[str] = []
        if "xl/sharedStrings.xml" in archive.namelist():
            for _, element in iterparse(
                archive.open("xl/sharedStrings.xml"), events=("end",)
            ):
                if local_name(element.tag) == "si":
                    shared.append(
                        "".join(
                            item.text or ""
                            for item in element.iter()
                            if local_name(item.tag) == "t"
                        )
                    )
                    element.clear()

        with archive.open("xl/worksheets/sheet1.xml") as sheet:
            for _, row in iterparse(sheet, events=("end",)):
                if local_name(row.tag) != "row":
                    continue
                values: dict[str, str | None] = {}
                for cell in row:
                    if local_name(cell.tag) != "c":
                        continue
                    match = CELL_REF.match(cell.attrib.get("r", ""))
                    if not match:
                        continue
                    column = match.group(1)
                    cell_type = cell.attrib.get("t")
                    if cell_type == "inlineStr":
                        value = "".join(
                            item.text or ""
                            for item in cell.iter()
                            if local_name(item.tag) == "t"
                        )
                    else:
                        value = first_text(cell, "v")
                        if cell_type == "s" and value is not None:
                            value = shared[int(value)]
                    values[column] = value
                yield values
                row.clear()


def read_yandex(path: Path) -> pd.DataFrame:
    iterator = xlsx_rows(path)
    try:
        header_row = next(iterator)
    except StopIteration:
        return pd.DataFrame()
    columns = {letter: value for letter, value in header_row.items() if value}
    records = [
        {name: row.get(letter) for letter, name in columns.items()}
        for row in iterator
    ]
    return pd.DataFrame.from_records(records, columns=list(columns.values()))


def normalize_url(value: Any) -> str:
    if value is None or pd.isna(value):
        return ""
    value = re.sub(r"\s+", " ", str(value)).strip().replace(" ", "%20")
    parts = urlsplit(value)
    scheme = parts.scheme.lower()
    host = parts.netloc.lower()
    path = re.sub(r"/{2,}", "/", parts.path)
    return urlunsplit((scheme, host, path, parts.query, ""))


def url_features(url: str) -> dict[str, bool]:
    lowered = url.lower()
    path = urlsplit(lowered).path
    query = urlsplit(lowered).query
    return {
        "query": bool(query),
        "pagination": "pagen_" in query,
        "raw_filter": "/filter/" in path,
        "legacy_brand": bool(re.search(r"/brand_[^/]+/", path)),
        "attr_landing": bool(re.search(r"/attr_[^/]+/", path)),
    }


def type_for_url(
    url: str,
    sitemap_urls: set[str],
    product_urls: set[str],
    filter_urls: set[str],
) -> str:
    features = url_features(url)
    path = urlsplit(url).path
    if features["pagination"]:
        return "pagination"
    if features["raw_filter"]:
        return "raw_filter"
    if url in filter_urls:
        return "seo_filter"
    if url in product_urls:
        return "product_or_category"
    if path.startswith("/blog/"):
        return "blog"
    if url in sitemap_urls:
        return "other_sitemap"
    return "outside_sitemap"


def counts(series: pd.Series) -> dict[str, int]:
    return {str(key): int(value) for key, value in series.value_counts().items()}


def feature_counts(urls: pd.Series) -> dict[str, int]:
    features = [url_features(url) for url in urls]
    return {
        key: sum(int(item[key]) for item in features)
        for key in ("query", "pagination", "raw_filter", "legacy_brand", "attr_landing")
    }


def duplicate_titles(frame: pd.DataFrame, limit: int = 20) -> dict[str, Any]:
    if "title" not in frame:
        return {"groups": 0, "urls": 0, "largest_group": 0, "examples": []}
    title = frame["title"].fillna("").str.strip()
    grouped = frame.assign(_title=title).loc[title.ne("")].groupby("_title")
    sizes = grouped.size().loc[lambda value: value > 1].sort_values(ascending=False)
    examples = []
    for name, size in sizes.head(limit).items():
        examples.append(
            {
                "title": name,
                "count": int(size),
                "urls": grouped.get_group(name)["url"].head(5).tolist(),
            }
        )
    return {
        "groups": int(len(sizes)),
        "urls": int(sizes.sum()),
        "largest_group": int(sizes.max()) if len(sizes) else 0,
        "examples": examples,
    }


def gsc_issue(path: Path) -> tuple[pd.DataFrame, dict[str, Any]]:
    chart = pd.read_excel(path, sheet_name="Диаграмма")
    table = pd.read_excel(path, sheet_name="Таблица")
    chart.columns = ["date", "affected"]
    chart["date"] = pd.to_datetime(chart["date"])
    chart["affected"] = chart["affected"].astype(int)
    table.columns = ["url", "last_crawl"]
    table["url"] = table["url"].map(normalize_url)
    return table, {
        "date_range": [
            str(chart["date"].min().date()),
            str(chart["date"].max().date()),
        ],
        "first": int(chart.iloc[0]["affected"]),
        "latest": int(chart.iloc[-1]["affected"]),
        "minimum": int(chart["affected"].min()),
        "maximum": int(chart["affected"].max()),
        "maximum_date": str(chart.loc[chart["affected"].idxmax(), "date"].date()),
        "sample_rows": int(len(table)),
    }


def crawl_stats(path: Path) -> dict[str, Any]:
    daily = pd.read_excel(path, sheet_name=0)
    daily.columns = ["date", "requests", "bytes", "response_ms"]
    daily["date"] = pd.to_datetime(daily["date"])
    for column in ("requests", "bytes", "response_ms"):
        daily[column] = pd.to_numeric(daily[column])

    def period(frame: pd.DataFrame) -> dict[str, float | int]:
        requests = int(frame["requests"].sum())
        return {
            "requests": requests,
            "bytes": int(frame["bytes"].sum()),
            "bytes_per_request": float(frame["bytes"].sum() / requests),
            "weighted_response_ms": float(
                (frame["response_ms"] * frame["requests"]).sum() / requests
            ),
        }

    response = pd.read_excel(path, sheet_name="Таблица по ответу")
    response_shares = dict(zip(response.iloc[:, 0], response.iloc[:, 1]))
    total_requests = int(daily["requests"].sum())
    first_28 = period(daily.head(28))
    last_28 = period(daily.tail(28))
    return {
        "date_range": [str(daily["date"].min().date()), str(daily["date"].max().date())],
        "days": int(len(daily)),
        "total": period(daily),
        "response_ms": {
            "latest": int(daily.iloc[-1]["response_ms"]),
            "minimum": int(daily["response_ms"].min()),
            "maximum": int(daily["response_ms"].max()),
        },
        "first_28_days": first_28,
        "last_28_days": last_28,
        "first_to_last_28_change": {
            key: last_28[key] / first_28[key] - 1
            for key in ("requests", "bytes_per_request", "weighted_response_ms")
        },
        "response_shares": {
            str(key): {"share": float(value), "estimated_requests": round(total_requests * value)}
            for key, value in response_shares.items()
        },
    }


def main() -> None:
    warnings.filterwarnings("ignore", message="Workbook contains no default style")
    args = parse_args()
    data_dir = args.data_dir
    crawled_path = find_one(data_dir, "Страница просканирована*.xlsx")
    blocked_path = find_one(data_dir, "заблокировано*.xlsx")
    crawl_stats_path = find_one(data_dir, "*Crawl-stats*.xlsx")
    yandex_current_path = find_one(data_dir, "Страницы в поиске яндекс*.xlsx")
    yandex_excluded_path = find_one(data_dir, "*исключенные страницы.xlsx")
    yandex_events_path = find_one(data_dir, "*страници в поиске.xlsx")

    sitemap = pd.read_csv(args.sitemap_urls, usecols=["url", "source_sitemap"])
    sitemap["url"] = sitemap["url"].map(normalize_url)
    sitemap_urls = set(sitemap["url"])
    product_urls = set(
        sitemap.loc[sitemap["source_sitemap"].str.contains("iblock_17", na=False), "url"]
    )
    crawl = pd.read_csv(
        args.filter_crawl,
        usecols=["url", "status", "final_url", "redirect_count", "noindex"],
    )
    for column in ("url", "final_url"):
        crawl[column] = crawl[column].map(normalize_url)
    filter_urls = set(crawl["url"])

    current = read_yandex(yandex_current_path)
    excluded = read_yandex(yandex_excluded_path)
    events = read_yandex(yandex_events_path)
    for frame in (current, excluded, events):
        frame["url"] = frame["url"].map(normalize_url)
        frame["url_type"] = frame["url"].map(
            lambda url: type_for_url(
                url, sitemap_urls=sitemap_urls, product_urls=product_urls, filter_urls=filter_urls
            )
        )

    crawled, crawled_summary = gsc_issue(crawled_path)
    blocked, blocked_summary = gsc_issue(blocked_path)
    for frame in (crawled, blocked):
        frame["url_type"] = frame["url"].map(
            lambda url: type_for_url(
                url, sitemap_urls=sitemap_urls, product_urls=product_urls, filter_urls=filter_urls
            )
        )

    current_urls = set(current["url"])
    excluded_urls = set(excluded["url"])
    for frame, summary in ((crawled, crawled_summary), (blocked, blocked_summary)):
        summary["sample_types"] = counts(frame["url_type"])
        summary["sample_features"] = feature_counts(frame["url"])
        summary["sample_in_sitemap"] = int(frame["url"].isin(sitemap_urls).sum())
        summary["sample_in_yandex_search"] = int(frame["url"].isin(current_urls).sum())
        summary["sample_in_yandex_excluded"] = int(frame["url"].isin(excluded_urls).sum())

    current_unique = current.drop_duplicates("url")
    excluded_unique = excluded.drop_duplicates("url")
    event_types = counts(events["event"])
    event_by_url = events.groupby("url")["event"].agg(set)
    both_events = int(event_by_url.map(lambda values: {"ADD", "DELETE"} <= values).sum())
    add_only = int(event_by_url.map(lambda values: values == {"ADD"}).sum())
    delete_only = int(event_by_url.map(lambda values: values == {"DELETE"}).sum())
    events["parsed_date"] = pd.to_datetime(events["updateDate"], dayfirst=True)
    latest_events = events.sort_values("parsed_date").drop_duplicates("url", keep="last")

    broken = crawl.loc[crawl["status"].ne(200)]
    live_noindex = crawl.loc[crawl["status"].eq(200) & crawl["noindex"].fillna(False)]
    valid_redirect = crawl.loc[
        crawl["status"].eq(200) & crawl["redirect_count"].fillna(0).gt(0)
    ]

    summary: dict[str, Any] = {
        "source_files": {
            "gsc_crawled_not_indexed": crawled_path.name,
            "gsc_blocked_by_robots": blocked_path.name,
            "gsc_crawl_stats": crawl_stats_path.name,
            "yandex_current": yandex_current_path.name,
            "yandex_excluded": yandex_excluded_path.name,
            "yandex_events": yandex_events_path.name,
        },
        "inventory": {
            "sitemap_unique_urls": len(sitemap_urls),
            "product_or_category_urls": len(product_urls),
            "seo_filter_crawl_urls": len(filter_urls),
        },
        "gsc": {
            "crawled_not_indexed": crawled_summary,
            "blocked_by_robots": blocked_summary,
            "crawl_stats": crawl_stats(crawl_stats_path),
        },
        "yandex": {
            "current_search": {
                "rows": int(len(current)),
                "unique_urls": int(current["url"].nunique()),
                "in_sitemap": int(current_unique["url"].isin(sitemap_urls).sum()),
                "url_types": counts(current_unique["url_type"]),
                "features": feature_counts(current_unique["url"]),
                "duplicate_titles": duplicate_titles(current_unique),
            },
            "excluded": {
                "rows": int(len(excluded)),
                "unique_urls": int(excluded["url"].nunique()),
                "in_sitemap": int(excluded_unique["url"].isin(sitemap_urls).sum()),
                "statuses": counts(excluded["status"]),
                "url_types": counts(excluded_unique["url_type"]),
                "status_by_url_type": {
                    url_type: counts(group["status"])
                    for url_type, group in excluded_unique.groupby("url_type")
                },
                "features": feature_counts(excluded_unique["url"]),
                "duplicate_titles": duplicate_titles(excluded_unique),
            },
            "events": {
                "rows": int(len(events)),
                "unique_urls": int(events["url"].nunique()),
                "date_range": [
                    str(events["parsed_date"].min().date()),
                    str(events["parsed_date"].max().date()),
                ],
                "event_counts": event_types,
                "urls_with_add_and_delete": both_events,
                "urls_add_only": add_only,
                "urls_delete_only": delete_only,
                "latest_event": counts(latest_events["event"]),
                "latest_delete_statuses": counts(
                    latest_events.loc[latest_events["event"].eq("DELETE"), "status"]
                ),
            },
            "filter_crawl_crosscheck": {
                "broken": {
                    "total": int(len(broken)),
                    "in_search": int(broken["url"].isin(current_urls).sum()),
                    "in_excluded": int(broken["url"].isin(excluded_urls).sum()),
                },
                "live_noindex": {
                    "total": int(len(live_noindex)),
                    "in_search": int(live_noindex["url"].isin(current_urls).sum()),
                    "in_excluded": int(live_noindex["url"].isin(excluded_urls).sum()),
                },
                "valid_redirect": {
                    "total": int(len(valid_redirect)),
                    "in_search": int(valid_redirect["url"].isin(current_urls).sum()),
                    "in_excluded": int(valid_redirect["url"].isin(excluded_urls).sum()),
                },
            },
        },
    }

    issue_rows = []
    for source, frame in (
        ("gsc_crawled_not_indexed", crawled),
        ("gsc_blocked_by_robots", blocked),
        ("yandex_excluded", excluded_unique),
    ):
        for row in frame.to_dict(orient="records"):
            issue_rows.append(
                {
                    "source": source,
                    "url": row.get("url"),
                    "url_type": row.get("url_type"),
                    "status": row.get("status"),
                    "in_sitemap": row.get("url") in sitemap_urls,
                    "in_yandex_search": row.get("url") in current_urls,
                }
            )
    issues = pd.DataFrame(issue_rows)
    args.issues.parent.mkdir(parents=True, exist_ok=True)
    issues.to_csv(args.issues, index=False, compression="gzip")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"Saved: {args.output}")
    print(f"Saved: {args.issues}")


if __name__ == "__main__":
    main()
