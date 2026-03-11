#!/usr/bin/env bash
set -euo pipefail

if [[ $# -lt 1 ]]; then
  echo "Usage: $0 <digest-markdown-file>"
  exit 1
fi

MD_FILE="$1"
PDF_FILE="${MD_FILE%.md}.pdf"
HTML_FILE="${MD_FILE%.md}.html"
TMP_MD=""
TMP_TEX_HEADER=""
MD_BASENAME="$(basename "$MD_FILE")"
RUN_DATE="$(echo "$MD_BASENAME" | sed -nE 's/^([0-9]{4}-[0-9]{2}-[0-9]{2})_.*$/\1/p')"
if [[ -z "$RUN_DATE" ]]; then
  RUN_DATE="$(date +%Y-%m-%d)"
fi
PDF_FOOTER_TEXT="Automated ID literature review ${RUN_DATE} Jonathan Underwood v1.0 March 2026"

if [[ ! -f "$MD_FILE" ]]; then
  echo "Markdown file not found: $MD_FILE" >&2
  exit 1
fi

# For PDF readability, remove verbose scoring rationale lines by default.
# Source markdown is left untouched.
if [[ "${PDF_INCLUDE_WHY_PRIORITIZED:-0}" != "1" ]]; then
  TMP_MD="$(mktemp "${TMPDIR:-/tmp}/digest.XXXXXX")"
  sed -E '/^[[:space:]]*Why (prioritized|prioritised|priotitized):/Id' "$MD_FILE" > "$TMP_MD"
fi

PDF_SOURCE="${TMP_MD:-$MD_FILE}"

# Optionally hide metadata lines in PDF while keeping source markdown unchanged.
if [[ "${PDF_INCLUDE_TYPE_AND_GROUP:-0}" != "1" ]]; then
  if [[ -z "$TMP_MD" ]]; then
    TMP_MD="$(mktemp "${TMPDIR:-/tmp}/digest.XXXXXX")"
    cp "$MD_FILE" "$TMP_MD"
  fi
  TMP_MD_FILTERED="$(mktemp "${TMPDIR:-/tmp}/digest.XXXXXX")"
  sed '/^[[:space:]]*Type:/d;/^[[:space:]]*Journal group:/d' "$TMP_MD" > "$TMP_MD_FILTERED"
  rm -f "$TMP_MD"
  TMP_MD="$TMP_MD_FILTERED"
  PDF_SOURCE="$TMP_MD"
fi

normalize_unicode_for_pdf() {
  # Normalize selected Unicode symbols for TeX setups without full glyph coverage.
  # Source markdown is unchanged; only temporary PDF source is modified.
  if [[ -z "$TMP_MD" ]]; then
    TMP_MD="$(mktemp "${TMPDIR:-/tmp}/digest.XXXXXX")"
    cp "$MD_FILE" "$TMP_MD"
  fi
  local tmp_norm
  tmp_norm="$(mktemp "${TMPDIR:-/tmp}/digest.XXXXXX")"
  perl -CS -pe 's/β/beta/g; s/α/alpha/g; s/γ/gamma/g; s/μ/mu/g; s/≥/>=/g; s/≤/<=/g; s/⁺/+/g; s/[\x00-\x08\x0B\x0C\x0E-\x1F\x7F-\x9F]//g;' "$TMP_MD" > "$tmp_norm"
  rm -f "$TMP_MD"
  TMP_MD="$tmp_norm"
  PDF_SOURCE="$TMP_MD"
}

if [[ "${PDF_NORMALIZE_UNICODE:-1}" == "1" ]]; then
  normalize_unicode_for_pdf
fi

cleanup() {
  [[ -n "$TMP_MD" ]] && rm -f "$TMP_MD"
  [[ -n "$TMP_TEX_HEADER" ]] && rm -f "$TMP_TEX_HEADER"
}
trap cleanup EXIT

pick_engine() {
  local candidate
  for candidate in xelatex lualatex tectonic pdflatex wkhtmltopdf; do
    if command -v "$candidate" >/dev/null 2>&1; then
      echo "$candidate"
      return 0
    fi
  done
  return 1
}

run_pandoc_pdf() {
  local engine="$1"
  local log_file
  local log_file_fallback
  local -a base_args=()
  local -a style_args=()
  local page_size
  local page_margin
  local font_size
  local line_stretch
  log_file="$(mktemp)"
  log_file_fallback="$(mktemp)"
  page_size="${PDF_PAGE_SIZE:-a4}"
  page_margin="${PDF_MARGIN:-1.6cm}"
  font_size="${PDF_FONT_SIZE:-11pt}"
  line_stretch="${PDF_LINE_STRETCH:-1.06}"

  # Improve readability and space usage in PDF output.
  base_args+=(
    -V "papersize:${page_size}"
    -V "geometry:margin=${page_margin}"
    -V "fontsize:${font_size}"
    -V "linestretch:${line_stretch}"
  )

  # Force clearer hyperlink styling in PDF output for TeX engines.
  if [[ "$engine" == "tectonic" || "$engine" == "pdflatex" || "$engine" == "xelatex" || "$engine" == "lualatex" ]]; then
    TMP_TEX_HEADER="$(mktemp "${TMPDIR:-/tmp}/linkstyle.XXXXXX.tex")"
    if [[ "$engine" == "xelatex" || "$engine" == "lualatex" ]]; then
      cat > "$TMP_TEX_HEADER" <<EOF
\usepackage{fontspec}
\usepackage{xcolor}
\usepackage[normalem]{ulem}
\usepackage{fancyhdr}
\usepackage{lastpage}
\AtBeginDocument{%
  \small
  \setlength{\emergencystretch}{3em}
  \sloppy
  \hypersetup{colorlinks=true,urlcolor=blue,linkcolor=blue,citecolor=blue}
  \let\HrefOrig\href
  \renewcommand{\href}[2]{\HrefOrig{#1}{\textcolor{blue}{\uline{#2}}}}
  \let\UrlOrig\url
  \renewcommand{\url}[1]{\textcolor{blue}{\uline{\nolinkurl{#1}}}}
  \pagestyle{fancy}
  \fancyhf{}
  \fancyfoot[L]{\scriptsize ${PDF_FOOTER_TEXT}}
  \fancyfoot[R]{\scriptsize \thepage\ of \pageref{LastPage}}
  \renewcommand{\headrulewidth}{0pt}
  \renewcommand{\footrulewidth}{0pt}
}
EOF
    else
      cat > "$TMP_TEX_HEADER" <<EOF
\usepackage{xcolor}
\usepackage[normalem]{ulem}
\usepackage{fancyhdr}
\usepackage{lastpage}
\AtBeginDocument{%
  \small
  \setlength{\emergencystretch}{3em}
  \sloppy
  \hypersetup{colorlinks=true,urlcolor=blue,linkcolor=blue,citecolor=blue}
  \let\HrefOrig\href
  \renewcommand{\href}[2]{\HrefOrig{#1}{\textcolor{blue}{\uline{#2}}}}
  \let\UrlOrig\url
  \renewcommand{\url}[1]{\textcolor{blue}{\uline{\nolinkurl{#1}}}}
  \pagestyle{fancy}
  \fancyhf{}
  \fancyfoot[L]{\scriptsize ${PDF_FOOTER_TEXT}}
  \fancyfoot[R]{\scriptsize \thepage\ of \pageref{LastPage}}
  \renewcommand{\headrulewidth}{0pt}
  \renewcommand{\footrulewidth}{0pt}
}
EOF
    fi
    style_args+=(--include-in-header="$TMP_TEX_HEADER")
  fi

  if pandoc -f markdown "$PDF_SOURCE" -o "$PDF_FILE" --pdf-engine="$engine" "${base_args[@]}" "${style_args[@]}" >"$log_file" 2>&1; then
    if [[ "${QUIET_TEX_WARNINGS:-1}" == "1" ]]; then
      grep -Ev "Underfull \\\\hbox|Overfull \\\\hbox|warning: warnings were issued by the TeX engine" "$log_file" || true
    else
      cat "$log_file"
    fi
    rm -f "$log_file"
    rm -f "$log_file_fallback"
    return 0
  fi

  echo "Styled PDF render failed with engine '$engine'. Retrying without custom link styling..." >&2
  if pandoc -f markdown "$PDF_SOURCE" -o "$PDF_FILE" --pdf-engine="$engine" "${base_args[@]}" >"$log_file_fallback" 2>&1; then
    cat "$log_file_fallback" | grep -Ev "Underfull \\\\hbox|Overfull \\\\hbox|warning: warnings were issued by the TeX engine" || true
    rm -f "$log_file"
    rm -f "$log_file_fallback"
    return 0
  fi

  echo "PDF generation failed with engine '$engine'. Full log:" >&2
  cat "$log_file" >&2
  echo "Fallback log (without custom styling):" >&2
  cat "$log_file_fallback" >&2
  rm -f "$log_file"
  rm -f "$log_file_fallback"
  return 1
}

if ENGINE="$(pick_engine)"; then
  run_pandoc_pdf "$ENGINE"
  echo "Created $PDF_FILE (engine: $ENGINE)"
  exit 0
fi

pandoc -f markdown "$PDF_SOURCE" -o "$HTML_FILE"
echo "No PDF engine found. Created HTML instead: $HTML_FILE"
echo "Open it and Print -> Save as PDF in your browser."
