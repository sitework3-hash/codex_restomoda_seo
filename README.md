# Restomoda SEO audit

Рабочий проект технического и поискового аудита `restomoda.ru`.

## Локальная настройка

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
```

Исходные выгрузки Excel/CSV и автоматически созданные наборы данных исключены
из Git. Они должны оставаться только в защищённой рабочей среде.

Все сырые данные Restomoda хранятся в `data_for_audit/`: выгрузки поисковых
систем, снимки экранов, архивы кода и другие исходные материалы. Папка целиком
исключена из Git. В корне проекта остаются только код, документация и отчёты.

## Команды

```bash
.venv/bin/python scripts/analyze_search_exports.py --data-dir data_for_audit
.venv/bin/python scripts/audit_sitemaps.py
.venv/bin/python scripts/crawl_sitemap_pages.py --limit 2000 \
  --concurrency 5 --requests-per-second 5
.venv/bin/python scripts/audit_rendering.py
```

Локальный Lighthouse через установленный Chrome:

```bash
npm install
npx --no-install lighthouse https://restomoda.ru/ \
  --chrome-path=/usr/bin/google-chrome \
  --output=json \
  --output-path=reports/generated/lighthouse/home-mobile.json \
  --quiet \
  --chrome-flags='--headless --no-sandbox --disable-dev-shm-usage' \
  --only-categories=performance,seo,best-practices,accessibility \
  --form-factor=mobile

.venv/bin/python scripts/analyze_lighthouse_results.py
```

Для длительного возобновляемого обхода SEO-фильтров:

```bash
.venv/bin/python scripts/crawl_sitemap_pages.py \
  --limit 0 \
  --source-contains sitemap_sotbit \
  --source-contains sitemap_seometa \
  --concurrency 5 \
  --requests-per-second 5 \
  --batch-size 250 \
  --resume \
  --output reports/generated/seo_filters_full.csv.gz \
  --summary reports/generated/seo_filters_full_summary.json
```

Флаг `--resume` пропускает уже сохранённые URL. Прогресс записывается после
каждой партии, поэтому длительный аудит можно безопасно продолжить после
остановки.

Анализ текущего сохранённого результата:

```bash
.venv/bin/python scripts/analyze_crawl_results.py \
  reports/generated/seo_filters_full.csv.gz \
  --summary reports/generated/seo_filters_analysis.json \
  --issues reports/generated/seo_filters_issues.csv.gz
```

Результаты команд сохраняются в `reports/generated/` и не коммитятся.
Выводы, прошедшие проверку, оформляются отдельными Markdown-отчётами в
`reports/`.

Текущие отчёты:

- `reports/2026-07-11-initial-technical-audit.md`;
- `reports/2026-07-11-seo-filter-full-audit.md`;
- `reports/2026-07-11-lighthouse-audit.md`.
