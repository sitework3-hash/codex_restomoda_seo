#!/usr/bin/env python3
"""Check rendered DOM, browser errors, and mobile overflow in real Chrome."""

from __future__ import annotations

import argparse
import asyncio
import json
import re
from pathlib import Path
from urllib.parse import urlparse

from playwright.async_api import async_playwright


DEFAULT_URLS = [
    "https://restomoda.ru/",
    "https://restomoda.ru/catalog/testoraskatochnye-mashiny/",
    "https://restomoda.ru/catalog/teplovye-stoly/teplovoy-stol-hicold-ts-16-gn/",
    "https://restomoda.ru/catalog/parokonvektomaty-injektornogo-tipa/attr_napryazhenie_220/",
    "https://restomoda.ru/blog/vytyazhnye-zonty-polnoe-rukovodstvo-po-vyboru-i-ustanovke/",
]
VIEWPORTS = {
    "mobile": {"width": 390, "height": 844},
    "desktop": {"width": 1440, "height": 900},
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("urls", nargs="*", default=DEFAULT_URLS)
    parser.add_argument("--chrome", default="/usr/bin/google-chrome")
    parser.add_argument(
        "--output-dir", type=Path, default=Path("reports/generated/rendering")
    )
    return parser.parse_args()


def slug(url: str) -> str:
    path = urlparse(url).path.strip("/") or "home"
    return re.sub(r"[^a-zA-Z0-9_-]+", "-", path)[:100].strip("-")


async def inspect_page(browser, url: str, profile: str, viewport: dict[str, int], output: Path):
    context = await browser.new_context(
        viewport=viewport,
        device_scale_factor=1,
        is_mobile=profile == "mobile",
        has_touch=profile == "mobile",
        locale="ru-RU",
    )
    page = await context.new_page()
    console_errors: list[str] = []
    page_errors: list[str] = []
    failed_requests: list[str] = []
    bad_responses: list[str] = []
    page.on(
        "console",
        lambda message: console_errors.append(message.text[:500])
        if message.type == "error"
        else None,
    )
    page.on("pageerror", lambda error: page_errors.append(str(error)[:500]))
    page.on(
        "requestfailed",
        lambda request: failed_requests.append(
            f"{request.failure}: {request.url}"[:700]
        ),
    )
    page.on(
        "response",
        lambda browser_response: bad_responses.append(
            f"{browser_response.status}: {browser_response.url}"[:700]
        )
        if browser_response.status >= 400
        else None,
    )
    response = None
    navigation_error = ""
    try:
        response = await page.goto(url, wait_until="domcontentloaded", timeout=60_000)
        await page.wait_for_timeout(4_000)
    except Exception as exc:  # Playwright exceptions include useful browser context.
        navigation_error = f"{type(exc).__name__}: {exc}"
    metrics = await page.evaluate(
        """() => {
          const canonical = document.querySelector('link[rel="canonical"]');
          const robots = document.querySelector('meta[name="robots" i]');
          const navigation = performance.getEntriesByType('navigation')[0];
          return {
            title: document.title || '',
            canonical: canonical ? canonical.href : '',
            robots: robots ? robots.content : '',
            h1_count: document.querySelectorAll('h1').length,
            h1: document.querySelector('h1')?.innerText?.trim() || '',
            text_length: document.body?.innerText?.length || 0,
            dom_nodes: document.querySelectorAll('*').length,
            html_length: document.documentElement?.outerHTML?.length || 0,
            viewport_width: window.innerWidth,
            document_width: document.documentElement.scrollWidth,
            horizontal_overflow: document.documentElement.scrollWidth > window.innerWidth + 2,
            jsonld_blocks: document.querySelectorAll('script[type="application/ld+json" i]').length,
            product_microdata: document.querySelectorAll('[itemtype$="/Product"], [itemtype$="/Product/"]').length,
            breadcrumb_microdata: document.querySelectorAll('[itemtype$="/BreadcrumbList"], [itemtype$="/BreadcrumbList/"]').length,
            navigation_ms: navigation ? Math.round(navigation.duration) : null,
            dom_content_loaded_ms: navigation ? Math.round(navigation.domContentLoadedEventEnd) : null,
          };
        }"""
    )
    screenshot = output / f"{slug(url)}-{profile}.png"
    await page.screenshot(path=str(screenshot), full_page=False)
    result = {
        "url": url,
        "profile": profile,
        "status": response.status if response else None,
        "final_url": page.url,
        "navigation_error": navigation_error,
        **metrics,
        "console_error_count": len(console_errors),
        "console_error_examples": console_errors[:10],
        "page_error_count": len(page_errors),
        "page_error_examples": page_errors[:10],
        "failed_request_count": len(failed_requests),
        "failed_request_examples": failed_requests[:10],
        "bad_response_count": len(bad_responses),
        "bad_response_examples": bad_responses[:20],
        "screenshot": str(screenshot),
    }
    await context.close()
    return result


async def run(args: argparse.Namespace) -> list[dict]:
    args.output_dir.mkdir(parents=True, exist_ok=True)
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(
            executable_path=args.chrome,
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage"],
        )
        results = []
        for url in args.urls:
            for profile, viewport in VIEWPORTS.items():
                print(f"Rendering {profile}: {url}", flush=True)
                results.append(
                    await inspect_page(browser, url, profile, viewport, args.output_dir)
                )
        await browser.close()
        return results


def main() -> None:
    args = parse_args()
    results = asyncio.run(run(args))
    output = args.output_dir / "rendering_audit.json"
    output.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Saved: {output}")


if __name__ == "__main__":
    main()
