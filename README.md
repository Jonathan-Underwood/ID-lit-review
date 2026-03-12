# ID Weekly Literature Digest

Automated weekly literature triage for infectious diseases + general medicine.

## Outputs

Each run generates in `outputs/`:

- `YYYY-MM-DD_digest.md`
- `YYYY-MM-DD_digest.pdf`
- `YYYY-MM-DD_digest.json`
- `YYYY-MM-DD_run_summary.json`
- `YYYY-MM-DD_core_podcast_source.md` (if enabled)
- `YYYY-MM-DD_core_podcast_source.pdf` (if enabled)

## Quick Start (Local)

```bash
cd /Users/jonathanunderwood/id-literature-digest
python3 run_digest.py --days 7 --max-results 500
```

With LLM enrichment:

```bash
python3 run_digest.py \
  --days 7 \
  --max-results 500 \
  --llm-enrich \
  --llm-core-top-n 15 \
  --llm-lite-top-n 25 \
  --llm-batch-size 1 \
  --llm-lite-batch-size 13 \
  --llm-max-requests 20
```

## One-command Weekly Run

```bash
./scripts/run_weekly_digest.sh
```

This script:

- loads `.env` / `.env.local`
- runs a preflight LLM estimate
- generates markdown/json (+ podcast source if enabled)
- renders PDFs
- emails via SMTP or Brevo

## Environment

Required for data + LLM:

- `NCBI_API_KEY`
- `GEMINI_API_KEY`

Brevo email (recommended):

- `BREVO_API_KEY`
- `BREVO_SENDER_EMAIL`
- `BREVO_SENDER_NAME` (optional)
- `BREVO_LIST_ID` (if using Brevo contact list delivery)

## GitHub Actions

Workflow file: `.github/workflows/weekly-digest.yml`

- Scheduled: **Friday 08:15 UTC**
- Also supports manual `workflow_dispatch`
- Uses concurrency control to prevent overlapping runs

## Scoring Notes

- Rule score combines journal tier/group, article type, topic keywords, and downweights.
- LLM score is added on top (`score = rule_score + llm_score`).
- Core digest is the top 15 by final score.
- Excludes no-abstract records and excludes publication types:
  - Review, Comment, Published Erratum, Editorial, Letter

## PDF Notes

`render_pdf.sh` applies styling and footer metadata automatically.
