# ID & GIM Weekly Literature Digest

[![DOI](https://img.shields.io/badge/DOI-10.5281%2Fzenodo.20512341-blue)](https://doi.org/10.5281/zenodo.20512341)

Automated weekly literature triage for infectious diseases + general medicine.

The PubMed scan uses record creation date (`CRDT`) across the previous seven
complete UTC calendar days. This captures citations when they first enter PubMed
without resurfacing them when an online-first record later receives a print date.

## Licence

This project is released under the BSD 3-Clause Licence. See the LICENSE file for details.

## Citation

If you use or adapt this software for academic, clinical, teaching, or research purposes, please cite the repository or archived release:

Underwood J. ID & GIM Weekly Literature Digest: an automated infectious diseases and general internal medicine literature surveillance workflow. Version 0.1.1. 2026. DOI: https://doi.org/10.5281/zenodo.20512341

## Disclaimer

This software is intended to support literature surveillance, education, and research workflow development. It does not provide medical advice, does not replace expert clinical judgement, and should not be used as a substitute for formal systematic review, guideline development, or validated clinical decision support.

## Outputs

Each run generates in `outputs/`:

- `YYYY-MM-DD_digest.md`
- `YYYY-MM-DD_digest.pdf`
- `YYYY-MM-DD_digest.json`
- `YYYY-MM-DD_run_summary.json`
- `YYYY-MM-DD_core_podcast_source.md` (if enabled)
- `YYYY-MM-DD_core_podcast_source.pdf` (if enabled)

## Example output

A shortened representative example is available in the `examples/` directory:

- [Example digest excerpt, Markdown](examples/example-digest-excerpt.md)
- [Example digest excerpt, PDF](examples/example-digest-excerpt.pdf)

The example is provided to demonstrate the output format only. It is not a clinical recommendation, systematic review, guideline, or complete literature update. Generated summaries and prioritisation scores require human verification.

## Quick Start (Local)

```bash
cd /Users/jonathanunderwood/id-literature-digest
python3 run_digest.py --days 7 --max-results 750
```

With LLM enrichment:

```bash
python3 run_digest.py \
  --days 7 \
  --max-results 750 \
  --llm-enrich \
  --llm-core-top-n 15 \
  --llm-lite-top-n 25 \
  --gemini-model gemini-3.8-flash \
  --gemini-fallback-models gemini-3.7-flash,gemini-3.6-flash,gemini-3.5-flash,gemini-3.5-flash-lite \
  --gemini-lite-model gemini-3.5-flash-lite \
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

The core appraisal defaults to Gemini 3.8 Flash, then falls back after hard model
quota exhaustion through Gemini 3.7, 3.6 and 3.5 Flash, followed by Gemini 3.5
Flash-Lite. Every core fallback retains the complete appraisal prompt and structured
output schema; only the model changes. Extended papers use the shorter Flash-Lite
prompt directly. Override the chain with `GEMINI_MODEL`, `GEMINI_FALLBACK_MODELS`,
and `GEMINI_LITE_MODEL` when using `scripts/run_weekly_digest.sh`.
Core appraisal uses the complete PubMed abstract. Its clinical-impact, method-quality,
and novelty fields remain internal ranking inputs and are not printed in the PDF.

Brevo email (recommended):

- `BREVO_API_KEY`
- `BREVO_SENDER_EMAIL`
- `BREVO_SENDER_NAME` (optional)
- `BREVO_LIST_ID` (if using Brevo contact list delivery)

## GitHub Actions

Workflow file: `.github/workflows/weekly-digest.yml`

Scheduled runs email automatically. Manual workflow runs default to preview-only;
select the `send_email` input explicitly to send one. Before any requested email,
the wrapper requires an LLM enrichment success rate of at least 50% and at least
10 enriched core papers by default. Set `LLM_MIN_EMAIL_SUCCESS_RATE` and
`LLM_MIN_EMAIL_CORE_ENRICHED` to change those thresholds. A failed gate retains and
uploads the generated files and cache, skips email, and marks the run failed.

- Scheduled: **Saturday 09:13 UTC**
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
The PDF starts the Core Digest and Extended Digest on new pages by default.
Each digest ends with a one-page, compact two-column Methods and QA Appendix containing the
run-specific search strategy, selection flow, scoring description, QA status, and models.
Set `PDF_PAGE_BREAK_BEFORE_CORE=0` or `PDF_PAGE_BREAK_BEFORE_EXTENDED=0` to disable either break.
Set `PDF_OVERVIEW_SECTION_SPACING=0` to disable the extra spacing around the opening overview sections.
Set `PDF_COMPACT_METHODS_APPENDIX=0` to render the appendix without compact two-column styling.
Article scores are omitted from the PDF by default but retained in Markdown and JSON;
set `PDF_INCLUDE_ARTICLE_SCORES=1` to show them in the PDF.
The PDF adds a small optical gap between each article date and the following
separator; set `PDF_SPACE_AFTER_ARTICLE_DATE=0` to disable it.
Core papers with full LLM appraisal use two concise `Why it matters` bullets, followed
by a one-line study-design summary and a lightly shaded box containing the primary
outcome, effect estimate, and confidence interval. Missing statistics are explicitly
labelled as not reported in the abstract rather than inferred. When the effect and its
confidence interval share units, the unit appears once at the end of the expression.
