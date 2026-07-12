#!/usr/bin/env python3
"""Build and optionally apply the reviewed P0 remediation bundle.

The source audit snapshot is never modified unless ``--write`` is supplied.
Redirect changes are accepted only from a completed live verification and only
when the same-brand destination returned HTTP 200.
"""

from __future__ import annotations

import argparse
import difflib
import json
import re
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable


HEADER = Path("local/templates/aspro_next/header.php")
FOOTER = Path("local/templates/aspro_next/footer.php")
INIT = Path("local/php_interface/init.php")
HTACCESS = Path(".htaccess")
PAGINATION_DEFAULT = Path(
    "local/templates/aspro_next/components/bitrix/system.pagenavigation/.default/template.php"
)


CANONICAL_REPLACEMENT = """<?
        // Единственный серверный canonical. Для валидной пагинации сохраняем
        // только параметры PAGEN_N; сортировки и служебные параметры исключаем.
        $canonicalPath = parse_url($_SERVER['REQUEST_URI'] ?? '/', PHP_URL_PATH);
        if (!is_string($canonicalPath) || $canonicalPath === '') {
            $canonicalPath = '/';
        }
        if ($canonicalPath[0] !== '/') {
            $canonicalPath = '/'.$canonicalPath;
        }

        $canonicalPagination = [];
        foreach ($_GET as $key => $value) {
            if (
                preg_match('/^PAGEN_\\d+$/', (string)$key)
                && is_scalar($value)
                && ctype_digit((string)$value)
                && (int)$value > 1
            ) {
                $canonicalPagination[(string)$key] = (int)$value;
            }
        }
        ksort($canonicalPagination, SORT_NATURAL);

        $canonicalUrl = 'https://restomoda.ru'.$canonicalPath;
        if ($canonicalPagination) {
            $canonicalUrl .= '?'.http_build_query(
                $canonicalPagination,
                '',
                '&',
                PHP_QUERY_RFC3986
            );
        }
        ?>
        <link rel="canonical" href="<?=htmlspecialchars(
            $canonicalUrl,
            ENT_QUOTES | ENT_SUBSTITUTE,
            LANG_CHARSET
        )?>" />"""


@dataclass
class Change:
    relative_path: Path
    original: str
    updated: str
    newline: str


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--site-root", type=Path, default=Path("data_for_audit/site_code")
    )
    parser.add_argument(
        "--redirect-analysis",
        type=Path,
        default=Path("reports/generated/htaccess_redirect_analysis.json"),
    )
    parser.add_argument(
        "--patch-output",
        type=Path,
        default=Path("reports/generated/p0-remediation.patch"),
    )
    parser.add_argument(
        "--manifest-output",
        type=Path,
        default=Path("reports/generated/p0-remediation-manifest.json"),
    )
    parser.add_argument(
        "--scope",
        choices=("all", "safety", "redirects", "canonical"),
        default="all",
    )
    parser.add_argument("--write", action="store_true")
    parser.add_argument("--backup-dir", type=Path)
    return parser.parse_args()


def source_text(path: Path) -> tuple[str, str]:
    raw = path.read_bytes().decode("utf-8", errors="strict")
    newline = "\r\n" if "\r\n" in raw else "\n"
    return raw.replace("\r\n", "\n"), newline


def replace_once(text: str, old: str, new: str, label: str) -> str:
    count = text.count(old)
    if count != 1:
        raise RuntimeError(f"{label}: expected one exact match, found {count}")
    return text.replace(old, new, 1)


def remove_dynamic_last_modified(text: str) -> str:
    text, count = re.subn(
        r"^<\?header\('Last-Modified: '\.gmdate\('D, d M Y H:i:s T', time\(\) - 60\)\);\?>\n",
        "",
        text,
        count=1,
        flags=re.MULTILINE,
    )
    if count != 1:
        raise RuntimeError(f"header.php: expected one dynamic Last-Modified, found {count}")
    return text


def update_header_canonical(text: str) -> str:
    old_canonical = """<?$APPLICATION->ShowMeta('canonical')?>

        <?
        if ($_REQUEST['PAGEN_1']) {
            global $APPLICATION;
            $APPLICATION->AddHeadString('<link href="https://'.$_SERVER['HTTP_HOST'].$APPLICATION->sDirPath.'" rel="canonical" />',true);
        }
        ?>

        <?
        if ( !isset($_REQUEST['PAGEN_1'])) { // CSite::InDir('/info/brands/') && ?>
            <link href="<?=$APPLICATION->GetCurDir();?>" rel="canonical" />
        <?
        }
        ?>"""
    return replace_once(
        text,
        old_canonical,
        CANONICAL_REPLACEMENT,
        "header.php canonical block",
    )


def update_footer(text: str) -> str:
    old = """            // Canonical
            let metaCanonical = $('meta[name="canonical"]').attr("content");
            if (metaCanonical) {
                $('link[rel="canonical"]').attr("href", metaCanonical);
            }
            console.log(metaCanonical);

"""
    return replace_once(text, old, "", "footer.php dead canonical JavaScript")


def update_init(text: str) -> str:
    old = '                "  Пароль: {$newPassword}\\n\\n";'
    new = (
        '                "  Пароль не сохраняется. Для доступа используйте " .\n'
        '                "штатное восстановление пароля.\\n\\n";'
    )
    return replace_once(text, old, new, "init.php plaintext password")


def update_default_pagination(text: str) -> str:
    old = '\t\t\t\t\t<link rel="canonical" href="<?=$arResult["sUrlPath"]?>" />\n'
    return replace_once(
        text,
        old,
        "",
        "default pagination body canonical",
    )


def eligible_redirects(analysis: dict[str, Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    if analysis.get("verified") is not True:
        raise RuntimeError("Redirect analysis must contain a completed live verification")
    eligible = []
    blocked = []
    for item in analysis.get("mismatch_candidates", []):
        verification = item.get("verification", {})
        results = list(verification.values())
        if len(results) != 3 or any(result.get("error") for result in results):
            blocked.append({**item, "block_reason": "missing_or_failed_live_verification"})
            continue
        if verification["source"].get("status") != 301:
            blocked.append({**item, "block_reason": "source_is_not_301"})
            continue
        if verification["identity_target"].get("status") != 200:
            blocked.append({**item, "block_reason": "same_brand_target_is_not_200"})
            continue
        eligible.append(item)
    return eligible, blocked


def update_htaccess(text: str, redirects: list[dict[str, Any]]) -> str:
    for item in redirects:
        source = re.escape(item["source"])
        current = re.escape(item["target"])
        pattern = re.compile(
            r"(RewriteCond\s+%\{REQUEST_URI\}\s+\^"
            + source
            + r"\$\n\s*RewriteRule\s+\(\.\*\)\s+https://%\{SERVER_NAME\})"
            + current
            + r"(\s+\[R=301,L\])"
        )
        text, count = pattern.subn(
            lambda match: match.group(1)
            + item["expected_identity_target"]
            + match.group(2),
            text,
            count=1,
        )
        if count != 1:
            raise RuntimeError(
                f".htaccess line {item['line']}: expected one verified redirect match, "
                f"found {count}"
            )
    return text


def create_changes(
    site_root: Path, analysis: dict[str, Any], scope: str
) -> tuple[list[Change], list[dict[str, Any]], list[dict[str, Any]]]:
    eligible: list[dict[str, Any]] = []
    blocked: list[dict[str, Any]] = []
    if scope in ("all", "redirects"):
        eligible, blocked = eligible_redirects(analysis)
    transformations: dict[Path, Callable[[str], str]] = {}

    def add_transform(path: Path, transform: Callable[[str], str]) -> None:
        previous = transformations.get(path)
        if previous is None:
            transformations[path] = transform
        else:
            transformations[path] = (
                lambda value, previous=previous, transform=transform: transform(
                    previous(value)
                )
            )

    if scope in ("all", "safety"):
        add_transform(HEADER, remove_dynamic_last_modified)
        add_transform(INIT, update_init)
    if scope in ("all", "canonical"):
        add_transform(HEADER, update_header_canonical)
        add_transform(FOOTER, update_footer)
        add_transform(PAGINATION_DEFAULT, update_default_pagination)
    if scope in ("all", "redirects"):
        add_transform(HTACCESS, lambda value: update_htaccess(value, eligible))
    changes = []
    for relative_path, transform in transformations.items():
        original, newline = source_text(site_root / relative_path)
        updated = transform(original)
        if original == updated:
            raise RuntimeError(f"{relative_path}: transformation produced no change")
        changes.append(Change(relative_path, original, updated, newline))
    return changes, eligible, blocked


def unified_patch(changes: list[Change]) -> str:
    chunks = []
    for change in changes:
        original = change.original.replace("\n", change.newline)
        updated = change.updated.replace("\n", change.newline)
        chunks.extend(
            difflib.unified_diff(
                original.splitlines(keepends=True),
                updated.splitlines(keepends=True),
                fromfile=f"a/{change.relative_path.as_posix()}",
                tofile=f"b/{change.relative_path.as_posix()}",
            )
        )
    return "".join(chunks)


def write_changes(site_root: Path, backup_dir: Path, changes: list[Change]) -> None:
    if backup_dir.exists() and any(backup_dir.iterdir()):
        raise RuntimeError(f"Backup directory is not empty: {backup_dir}")
    backup_dir.mkdir(parents=True, exist_ok=True)
    for change in changes:
        source = site_root / change.relative_path
        backup = backup_dir / change.relative_path
        backup.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, backup)
    for change in changes:
        source = site_root / change.relative_path
        source.write_bytes(change.updated.replace("\n", change.newline).encode("utf-8"))


def main() -> None:
    args = parse_args()
    analysis = (
        json.loads(args.redirect_analysis.read_text(encoding="utf-8"))
        if args.scope in ("all", "redirects")
        else {}
    )
    changes, eligible, blocked = create_changes(args.site_root, analysis, args.scope)
    patch = unified_patch(changes)
    manifest = {
        "site_root": str(args.site_root),
        "scope": args.scope,
        "dry_run": not args.write,
        "changed_files": [str(change.relative_path) for change in changes],
        "changes": {
            **(
                {
                    "remove_dynamic_last_modified": 1,
                    "remove_plaintext_password_from_order_comment": 1,
                }
                if args.scope in ("all", "safety")
                else {}
            ),
            **(
                {
                    "single_server_canonical_with_pagination": 1,
                    "remove_dead_canonical_javascript": 1,
                    "remove_body_canonical_from_default_pagination": 1,
                }
                if args.scope in ("all", "canonical")
                else {}
            ),
            **(
                {"verified_brand_redirect_fixes": len(eligible)}
                if args.scope in ("all", "redirects")
                else {}
            ),
        },
        "verified_redirect_fixes": [
            {
                "line": item["line"],
                "source": item["source"],
                "current_target": item["target"],
                "new_target": item["expected_identity_target"],
            }
            for item in eligible
        ],
        "blocked_brand_redirect_candidates": [
            {
                "line": item["line"],
                "source": item["source"],
                "current_target": item["target"],
                "same_brand_target": item["expected_identity_target"],
                "reason": item["block_reason"],
            }
            for item in blocked
        ],
        "manual_items": [
            {
                "source": "/info/brands/amitek/?section_id=1077",
                "current_target": "/catalog/professionalnyy-slayser-dlya-narezki-produktov/amitek/",
                "reason": "active redirects.php target is 404; no equivalent landing is present in sitemap",
            },
            {
                "reason": "server 5xx and robots.txt outages require access/error logs before code changes"
            },
            {
                "reason": "sitemap generation is controlled by Bitrix/module configuration absent from the snapshot"
            },
        ],
    }

    args.patch_output.parent.mkdir(parents=True, exist_ok=True)
    args.patch_output.write_text(patch, encoding="utf-8")
    args.manifest_output.parent.mkdir(parents=True, exist_ok=True)
    args.manifest_output.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    if args.write:
        if args.backup_dir is None:
            raise SystemExit("--backup-dir is required together with --write")
        if args.site_root.resolve() == Path("data_for_audit/site_code").resolve():
            raise SystemExit(
                "Refusing to modify the immutable audit snapshot; pass an explicit "
                "staging or production --site-root"
            )
        if args.backup_dir.resolve().is_relative_to(args.site_root.resolve()):
            raise SystemExit("--backup-dir must be outside --site-root")
        write_changes(args.site_root, args.backup_dir, changes)
    print(f"Saved: {args.patch_output}")
    print(f"Saved: {args.manifest_output}")
    print(
        f"Files: {len(changes)}, verified redirect fixes: {len(eligible)}, "
        f"blocked redirect candidates: {len(blocked)}, mode: "
        f"{'write' if args.write else 'dry-run'}"
    )


if __name__ == "__main__":
    main()
