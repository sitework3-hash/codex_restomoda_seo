# Restomoda SEO audit

Рабочий проект технического и поискового аудита `restomoda.ru`.

## Локальная настройка

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
```

Исходные выгрузки Excel/CSV и автоматически созданные наборы данных исключены
из Git. Они должны оставаться только в защищённой рабочей среде.

## Команды

```bash
.venv/bin/python scripts/analyze_search_exports.py
.venv/bin/python scripts/audit_sitemaps.py
.venv/bin/python scripts/crawl_sitemap_pages.py --limit 2000 \
  --concurrency 5 --requests-per-second 5
.venv/bin/python scripts/audit_rendering.py
```

Результаты команд сохраняются в `reports/generated/` и не коммитятся.
Выводы, прошедшие проверку, оформляются отдельными Markdown-отчётами в
`reports/`.
