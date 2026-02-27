# ID Weekly Literature Digest

Standalone project to generate a weekly infectious diseases + acute medicine digest from PubMed.

## What it does

- Uses your curated journal list (general medicine, ID/microbiology, and basic/translational science).
- Pulls papers from the last 7 days (default).
- Scores papers for near-term clinical translation and trial relevance.
- Produces:
  - `outputs/YYYY-MM-DD_digest.md`
  - `outputs/YYYY-MM-DD_digest.json`
  - `outputs/YYYY-MM-DD_core_podcast_source.md` (when podcast source enabled)

## Quick start

```bash
cd /Users/jonathanunderwood/id-literature-digest
python3 run_digest.py --days 7 --max-results 150
```

With Gemini enrichment (request-efficient default mode):

```bash
export GEMINI_API_KEY="your_key"
python3 run_digest.py --days 7 --max-results 150 --llm-enrich --llm-top-n 40 --llm-batch-size 10 --llm-batch-delay-seconds 12 --llm-max-requests 18
```

With Gemini enrichment for all retrieved papers (weekly full-summary mode):

```bash
export GEMINI_API_KEY="your_key"
python3 run_digest.py --days 7 --max-results 150 --llm-enrich --llm-top-n 0 --llm-batch-size 10 --llm-batch-delay-seconds 12 --llm-max-requests 18
```

One-command weekly run (LLM + PDF):

```bash
./scripts/run_weekly_digest.sh
```

By default this now runs an LLM estimate preflight first and (in interactive terminal sessions) asks whether to proceed.

Optional overrides:

```bash
DAYS=7 MAX_RESULTS=150 LLM_TOP_N=40 LLM_BATCH_SIZE=10 LLM_BATCH_DELAY_SECONDS=12 LLM_MAX_REQUESTS=18 ./scripts/run_weekly_digest.sh
```

Disable preflight prompt (for cron/automation):

```bash
ESTIMATE_FIRST=0 AUTO_PROCEED=1 ./scripts/run_weekly_digest.sh
```

Podcast source controls:

```bash
PODCAST_SOURCE=1 PODCAST_MAX_ITEMS=15 ./scripts/run_weekly_digest.sh
```

Podcast PDF controls:

```bash
PODCAST_PDF=1 ./scripts/run_weekly_digest.sh
```

Set `PODCAST_PDF=0` to skip podcast PDF conversion.

Direct CLI usage:

```bash
python3 run_digest.py --days 7 --max-results 150 --llm-enrich --llm-top-n 40 --llm-batch-size 10 --podcast-source --podcast-max-items 15
```

Estimator-first workflow (recommended for request-limited plans):

```bash
python3 run_digest.py --days 7 --max-results 150 --llm-enrich --llm-top-n 40 --llm-batch-size 10 --estimate-llm-requests
```

Then run with a hard request cap (example: 18):

```bash
LLM_TOP_N=0 LLM_BATCH_SIZE=10 LLM_MAX_REQUESTS=18 ./scripts/run_weekly_digest.sh
```

Safe/stable mode (recommended if quota/rate issues recur):

```bash
SAFE_MODE=1 LLM_MIN_SUCCESS_RATE=0.5 ./scripts/run_weekly_digest.sh
```

Low-risk validation run (recommended after config changes):

```bash
python3 run_digest.py --days 7 --max-results 150 --llm-enrich --llm-top-n 5 --llm-batch-size 2 --llm-batch-delay-seconds 15
```

## Weekly automation (cron)

Run every Friday at 06:30:

```bash
30 6 * * 5 cd /Users/jonathanunderwood/id-literature-digest && /usr/bin/python3 run_digest.py --days 7 --max-results 150 --llm-enrich --llm-top-n 40 --llm-batch-size 10 --llm-batch-delay-seconds 12 --llm-max-requests 18
```

## Optional: PDF and email

- Markdown to PDF:
  - `./scripts/render_pdf.sh outputs/YYYY-MM-DD_digest.md`
  - Script auto-selects available engine (`tectonic` first), and falls back to HTML if no PDF engine is installed.
  - To show all TeX warnings, run with `QUIET_TEX_WARNINGS=0`.
- Email:
  - Set `SMTP_HOST`, `SMTP_PORT`, `SMTP_USER`, `SMTP_PASSWORD`, `SMTP_FROM`
  - `python3 scripts/email_digest.py --to your@email --subject "Weekly ID digest" --markdown outputs/YYYY-MM-DD_digest.md --pdf outputs/YYYY-MM-DD_digest.pdf`

## Notes

- This starter uses PubMed E-utilities only (reliable and structured).
- Add your NCBI API key via `NCBI_API_KEY` env var for higher request limits.
- Add `GEMINI_API_KEY` to enable optional LLM enrichment.
- LLM results are cached in `outputs/llm_cache.json` to minimize repeat calls and stay in free limits.
- LLM enrichment is batched to reduce requests (better fit for free-tier request caps).
- If a batch returns malformed/truncated JSON, the script automatically retries with smaller sub-batches.
- Clinical judgment is still required; scores are triage aids.
