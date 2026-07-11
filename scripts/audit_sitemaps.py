#!/usr/bin/env python3
"""Inventory all sitemap files referenced by robots.txt.

The script fetches sitemap XML documents only. It does not crawl page URLs, so
it is safe to use as the first, low-load stage of a technical audit.
"""

from __future__ import annotations

import argparse
import csv
import gzip
import json
from collections import Counter, deque
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

import httpx
from lxml import etree


SITEMAP_NS = "http://www.sitemaps.org/schemas/sitemap/0.9"
USER_AGENT = "RestomodaTechnicalAudit/1.0 (site-owner SEO audit)"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="https://restomoda.ru")
    parser.add_argument(
        "--output-dir", type=Path, default=Path("reports/generated")
    )
    return parser.parse_args()


def sitemap_urls_from_robots(text: str) -> list[str]:
    urls = []
    for line in text.splitlines():
        key, separator, value = line.partition(":")
        if separator and key.strip().lower() == "sitemap":
            urls.append(value.strip())
    return urls


def local_name(element: etree._Element) -> str:
    return etree.QName(element).localname


def text_at(element: etree._Element, child_name: str) -> str | None:
    node = element.find(f"{{{SITEMAP_NS}}}{child_name}")
    if node is None or node.text is None:
        return None
    return node.text.strip()


def url_bucket(url: str) -> str:
    path = urlparse(url).path
    if path == "/":
        return "home"
    if path.startswith("/catalog/"):
        return "catalog"
    if path.startswith("/product/"):
        return "product"
    if path.startswith("/info/"):
        return "info"
    return path.strip("/").split("/", 1)[0] or "other"


def main() -> None:
    args = parse_args()
    args.output_dir.mkdir(parents=True, exist_ok=True)
    robots_url = f"{args.base_url.rstrip('/')}/robots.txt"
    timeout = httpx.Timeout(30.0, connect=10.0)
    records: list[dict[str, object]] = []
    page_rows: list[dict[str, str]] = []
    seen_sitemaps: set[str] = set()
    queue: deque[str]

    with httpx.Client(
        follow_redirects=True,
        timeout=timeout,
        headers={"User-Agent": USER_AGENT, "Accept": "application/xml,text/xml,*/*"},
    ) as client:
        robots_response = client.get(robots_url)
        robots_response.raise_for_status()
        seeds = sitemap_urls_from_robots(robots_response.text)
        if not seeds:
            seeds = [f"{args.base_url.rstrip('/')}/sitemap.xml"]
        queue = deque(dict.fromkeys(seeds))

        while queue:
            sitemap_url = queue.popleft()
            if sitemap_url in seen_sitemaps:
                continue
            seen_sitemaps.add(sitemap_url)
            record: dict[str, object] = {"url": sitemap_url}
            try:
                response = client.get(sitemap_url)
                record.update(
                    status=response.status_code,
                    final_url=str(response.url),
                    bytes=len(response.content),
                    content_type=response.headers.get("content-type", ""),
                )
                response.raise_for_status()
                root = etree.fromstring(response.content)
                kind = local_name(root)
                record["kind"] = kind
                if kind == "sitemapindex":
                    children = []
                    lastmods = []
                    for node in root.findall(f"{{{SITEMAP_NS}}}sitemap"):
                        loc = text_at(node, "loc")
                        lastmod = text_at(node, "lastmod")
                        if loc:
                            children.append(loc)
                            queue.append(loc)
                        if lastmod:
                            lastmods.append(lastmod)
                    record.update(
                        child_sitemaps=len(children),
                        lastmod_min=min(lastmods) if lastmods else None,
                        lastmod_max=max(lastmods) if lastmods else None,
                    )
                elif kind == "urlset":
                    lastmods = []
                    buckets: Counter[str] = Counter()
                    count = 0
                    for node in root.findall(f"{{{SITEMAP_NS}}}url"):
                        loc = text_at(node, "loc")
                        lastmod = text_at(node, "lastmod")
                        if not loc:
                            continue
                        count += 1
                        buckets[url_bucket(loc)] += 1
                        if lastmod:
                            lastmods.append(lastmod)
                        page_rows.append(
                            {
                                "url": loc,
                                "lastmod": lastmod or "",
                                "source_sitemap": sitemap_url,
                                "bucket": url_bucket(loc),
                            }
                        )
                    record.update(
                        page_urls=count,
                        buckets=dict(buckets),
                        lastmod_min=min(lastmods) if lastmods else None,
                        lastmod_max=max(lastmods) if lastmods else None,
                    )
                else:
                    record["error"] = f"Unexpected XML root: {kind}"
            except (httpx.HTTPError, etree.XMLSyntaxError) as exc:
                record["error"] = str(exc)
            records.append(record)

    url_counts = Counter(row["url"] for row in page_rows)
    duplicate_urls = {url: count for url, count in url_counts.items() if count > 1}
    xml_urls_listed_as_pages = sorted(
        row["url"] for row in page_rows if urlparse(row["url"]).path.endswith(".xml")
    )
    lastmod_dates = Counter(
        row["lastmod"][:10] for row in page_rows if row["lastmod"]
    )
    summary = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "base_url": args.base_url,
        "robots_url": robots_url,
        "robots_status": robots_response.status_code,
        "seed_sitemaps": seeds,
        "sitemap_documents": len(records),
        "sitemap_errors": sum(bool(record.get("error")) for record in records),
        "page_url_rows": len(page_rows),
        "unique_page_urls": len(url_counts),
        "duplicate_page_urls": len(duplicate_urls),
        "xml_urls_listed_as_pages": xml_urls_listed_as_pages,
        "url_buckets": dict(Counter(row["bucket"] for row in page_rows)),
        "top_lastmod_dates": dict(lastmod_dates.most_common(20)),
        "sitemaps": records,
        "duplicate_examples": dict(list(sorted(duplicate_urls.items()))[:100]),
    }

    summary_path = args.output_dir / "sitemap_inventory.json"
    urls_path = args.output_dir / "sitemap_urls.csv.gz"
    summary_path.write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    with gzip.open(urls_path, "wt", encoding="utf-8", newline="") as file_handle:
        writer = csv.DictWriter(
            file_handle,
            fieldnames=["url", "lastmod", "source_sitemap", "bucket"],
        )
        writer.writeheader()
        writer.writerows(page_rows)

    print(json.dumps({key: summary[key] for key in (
        "robots_status",
        "sitemap_documents",
        "sitemap_errors",
        "page_url_rows",
        "unique_page_urls",
        "duplicate_page_urls",
        "url_buckets",
    )}, ensure_ascii=False, indent=2))
    print(f"Saved: {summary_path}")
    print(f"Saved: {urls_path}")


if __name__ == "__main__":
    main()
