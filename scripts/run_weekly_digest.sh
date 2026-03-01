#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

# Auto-load environment variables from .env files (if present).
# .env.local overrides .env.
if [[ -f ".env" ]]; then
  set -a
  # shellcheck disable=SC1091
  source ".env"
  set +a
fi
if [[ -f ".env.local" ]]; then
  set -a
  # shellcheck disable=SC1091
  source ".env.local"
  set +a
fi

DAYS="${DAYS:-7}"
MAX_RESULTS="${MAX_RESULTS:-200}"
LLM_TOP_N="${LLM_TOP_N:-24}"
LLM_CORE_TOP_N="${LLM_CORE_TOP_N:-15}"
LLM_LITE_TOP_N="${LLM_LITE_TOP_N:-25}"
LLM_BATCH_SIZE="${LLM_BATCH_SIZE:-2}"
LLM_LITE_BATCH_SIZE="${LLM_LITE_BATCH_SIZE:-4}"
LLM_BATCH_DELAY_SECONDS="${LLM_BATCH_DELAY_SECONDS:-15}"
LLM_MIN_SUCCESS_RATE="${LLM_MIN_SUCCESS_RATE:-0}"
LLM_MAX_REQUESTS="${LLM_MAX_REQUESTS:-18}"
SAFE_MODE="${SAFE_MODE:-0}"
PODCAST_SOURCE="${PODCAST_SOURCE:-1}"
PODCAST_MAX_ITEMS="${PODCAST_MAX_ITEMS:-15}"
PODCAST_PDF="${PODCAST_PDF:-1}"
ESTIMATE_FIRST="${ESTIMATE_FIRST:-1}"
AUTO_PROCEED="${AUTO_PROCEED:-0}"
SEND_EMAIL="${SEND_EMAIL:-0}"
NO_EMAIL="${NO_EMAIL:-0}"
EMAIL_TO="${EMAIL_TO:-}"
EMAIL_TO_FILE="${EMAIL_TO_FILE:-}"
EMAIL_SUBJECT_PREFIX="${EMAIL_SUBJECT_PREFIX:-Weekly ID + General Medicine Digest}"
EMAIL_PROVIDER="${EMAIL_PROVIDER:-smtp}"

if [[ "$NO_EMAIL" == "1" ]]; then
  SEND_EMAIL="0"
fi

echo "Email mode: SEND_EMAIL=$SEND_EMAIL (provider=${EMAIL_PROVIDER})"

CMD=(
  python3 run_digest.py
  --days "$DAYS" \
  --max-results "$MAX_RESULTS" \
  --llm-enrich \
  --llm-top-n "$LLM_TOP_N" \
  --llm-core-top-n "$LLM_CORE_TOP_N" \
  --llm-lite-top-n "$LLM_LITE_TOP_N" \
  --llm-batch-size "$LLM_BATCH_SIZE" \
  --llm-lite-batch-size "$LLM_LITE_BATCH_SIZE" \
  --llm-batch-delay-seconds "$LLM_BATCH_DELAY_SECONDS" \
  --llm-min-success-rate "$LLM_MIN_SUCCESS_RATE" \
  --llm-max-requests "$LLM_MAX_REQUESTS" \
  --podcast-max-items "$PODCAST_MAX_ITEMS"
)

if [[ "$SAFE_MODE" == "1" ]]; then
  CMD+=(--safe-mode)
fi
if [[ "$PODCAST_SOURCE" == "1" ]]; then
  CMD+=(--podcast-source)
fi

if [[ "$ESTIMATE_FIRST" == "1" ]]; then
  ESTIMATE_CMD=(
    python3 run_digest.py
    --days "$DAYS" \
    --max-results "$MAX_RESULTS" \
    --llm-enrich \
    --llm-top-n "$LLM_TOP_N" \
    --llm-core-top-n "$LLM_CORE_TOP_N" \
    --llm-lite-top-n "$LLM_LITE_TOP_N" \
    --llm-batch-size "$LLM_BATCH_SIZE" \
    --llm-lite-batch-size "$LLM_LITE_BATCH_SIZE" \
    --estimate-llm-requests
  )
  echo "Running LLM request/token preflight estimate..."
  "${ESTIMATE_CMD[@]}"

  if [[ -t 0 && "$AUTO_PROCEED" != "1" ]]; then
    echo
    read -r -p "Proceed with full run using current settings? [y/N] " RESPONSE
    case "$RESPONSE" in
      y|Y|yes|YES)
        ;;
      *)
        echo "Aborted before full run. You can rerun with modified env vars."
        exit 0
        ;;
    esac
  fi
fi

"${CMD[@]}"

LATEST_MD="$(ls -1t outputs/*_digest.md | head -n 1)"
./scripts/render_pdf.sh "$LATEST_MD"
LATEST_PDF="${LATEST_MD%.md}.pdf"
LATEST_MD_ABS="$ROOT_DIR/${LATEST_MD#./}"
LATEST_PDF_ABS="$ROOT_DIR/${LATEST_PDF#./}"

echo "Completed:"
echo "- Markdown: $LATEST_MD_ABS"
echo "- PDF: $LATEST_PDF_ABS"
echo "- PDF link: file://$LATEST_PDF_ABS"

if [[ "$PODCAST_SOURCE" == "1" ]]; then
  LATEST_PODCAST_MD="$(ls -1t outputs/*_core_podcast_source.md 2>/dev/null | head -n 1 || true)"
  if [[ -n "${LATEST_PODCAST_MD:-}" ]]; then
    if [[ "$PODCAST_PDF" == "1" ]]; then
      ./scripts/render_pdf.sh "$LATEST_PODCAST_MD"
      LATEST_PODCAST_PDF="${LATEST_PODCAST_MD%.md}.pdf"
      LATEST_PODCAST_MD_ABS="$ROOT_DIR/${LATEST_PODCAST_MD#./}"
      LATEST_PODCAST_PDF_ABS="$ROOT_DIR/${LATEST_PODCAST_PDF#./}"
      echo "- Podcast markdown: $LATEST_PODCAST_MD_ABS"
      echo "- Podcast PDF: $LATEST_PODCAST_PDF_ABS"
      echo "- Podcast PDF link: file://$LATEST_PODCAST_PDF_ABS"
    else
      LATEST_PODCAST_MD_ABS="$ROOT_DIR/${LATEST_PODCAST_MD#./}"
      echo "- Podcast markdown: $LATEST_PODCAST_MD_ABS"
    fi
  fi
fi

if [[ "$SEND_EMAIL" == "1" ]]; then
  if [[ -z "$EMAIL_TO" && -z "$EMAIL_TO_FILE" ]]; then
    echo "SEND_EMAIL=1 but EMAIL_TO and EMAIL_TO_FILE are both empty. Skipping email."
  else
    EMAIL_SUBJECT="$EMAIL_SUBJECT_PREFIX ($(basename "$LATEST_MD" .md))"
    EMAIL_CMD=(
      python3 scripts/email_digest.py
      --provider "$EMAIL_PROVIDER"
      --subject "$EMAIL_SUBJECT"
      --markdown "$LATEST_MD"
      --pdf "$LATEST_PDF"
    )
    if [[ -n "$EMAIL_TO_FILE" ]]; then
      EMAIL_CMD+=(--to-file "$EMAIL_TO_FILE")
    else
      EMAIL_CMD+=(--to "$EMAIL_TO")
    fi
    "${EMAIL_CMD[@]}"
  fi
else
  echo "Email sending disabled for this run."
fi
