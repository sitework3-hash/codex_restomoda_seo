#!/usr/bin/env python3
"""Create a compact machine summary from local Lighthouse JSON reports."""

from __future__ import annotations

import argparse
import json
from collections import defaultdict
from pathlib import Path
from typing import Any


METRIC_IDS = (
    "first-contentful-paint",
    "largest-contentful-paint",
    "speed-index",
    "total-blocking-time",
    "cumulative-layout-shift",
    "interactive",
    "server-response-time",
    "total-byte-weight",
    "mainthread-work-breakdown",
    "bootup-time",
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "input_dir", type=Path, nargs="?", default=Path("reports/generated/lighthouse")
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("reports/generated/lighthouse_summary.json"),
    )
    return parser.parse_args()


def failed_audits(report: dict[str, Any], category: str) -> list[dict[str, Any]]:
    audits = report["audits"]
    rows = []
    for reference in report["categories"][category]["auditRefs"]:
        audit_id = reference["id"]
        audit = audits[audit_id]
        if audit.get("score") is None or audit.get("score") >= 1:
            continue
        if audit.get("scoreDisplayMode") in {"manual", "notApplicable", "informative"}:
            continue
        rows.append(
            {
                "id": audit_id,
                "score": audit.get("score"),
                "title": audit.get("title"),
                "display": audit.get("displayValue"),
            }
        )
    return rows


def summarize(path: Path) -> dict[str, Any] | None:
    report = json.loads(path.read_text(encoding="utf-8"))
    categories = report.get("categories", {})
    performance = categories.get("performance", {})
    if performance.get("score") is None:
        return None
    audits = report["audits"]
    requests = audits.get("network-requests", {}).get("details", {}).get("items", [])
    resources: dict[str, dict[str, int]] = defaultdict(lambda: {"requests": 0, "bytes": 0})
    for request in requests:
        resource_type = request.get("resourceType", "Other")
        resources[resource_type]["requests"] += 1
        resources[resource_type]["bytes"] += int(request.get("transferSize") or 0)
    savings = []
    for audit_id, audit in audits.items():
        details = audit.get("details") or {}
        savings_ms = float(details.get("overallSavingsMs") or 0)
        savings_bytes = int(details.get("overallSavingsBytes") or 0)
        if savings_ms or savings_bytes:
            savings.append(
                {
                    "id": audit_id,
                    "title": audit.get("title"),
                    "milliseconds": savings_ms,
                    "bytes": savings_bytes,
                }
            )
    savings.sort(key=lambda item: (item["milliseconds"], item["bytes"]), reverse=True)
    return {
        "file": path.name,
        "url": report.get("finalDisplayedUrl") or report.get("finalUrl"),
        "fetch_time": report.get("fetchTime"),
        "lighthouse_version": report.get("lighthouseVersion"),
        "scores": {
            name: round(category["score"] * 100)
            for name, category in categories.items()
            if category.get("score") is not None
        },
        "metrics": {
            metric_id: {
                "numeric": audits.get(metric_id, {}).get("numericValue"),
                "display": audits.get(metric_id, {}).get("displayValue"),
            }
            for metric_id in METRIC_IDS
        },
        "network_requests": len(requests),
        "resources": dict(resources),
        "failed_seo": failed_audits(report, "seo"),
        "failed_best_practices": failed_audits(report, "best-practices"),
        "failed_accessibility": failed_audits(report, "accessibility"),
        "top_savings": savings[:20],
    }


def main() -> None:
    args = parse_args()
    rows = []
    skipped = []
    for path in sorted(args.input_dir.glob("*.json")):
        result = summarize(path)
        if result is None:
            skipped.append(path.name)
        else:
            rows.append(result)
    output = {"reports": rows, "skipped_incomplete": skipped}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(output, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"Complete reports: {len(rows)}")
    print(f"Skipped incomplete reports: {len(skipped)}")
    print(f"Saved: {args.output}")


if __name__ == "__main__":
    main()
