#!/usr/bin/env bash
# Rebuild the PDF copy of every guide in docs/ from its HTML (the Docs tab of the UI shows the HTML).
#
#   tools/make_docs_pdf.sh [docs/M2_Migration_Running_and_UI.html ...]
#
# Needs Chrome or Chromium. Page size and margins come from the @page rule in each guide.
set -eu
HERE="$(cd "$(dirname "$0")/.." && pwd)"
CHROME="$(command -v google-chrome || command -v chromium || command -v chromium-browser || command -v brave-browser || true)"
[ -n "$CHROME" ] || { echo "Install Chrome or Chromium to print the guides."; exit 1; }
[ "$#" -gt 0 ] || set -- "$HERE"/docs/*.html
for html in "$@"; do
  pdf="${html%.html}.pdf"
  "$CHROME" --headless --no-sandbox --disable-gpu --no-pdf-header-footer --print-to-pdf="$pdf" \
    "file://$(realpath "$html")" >/dev/null 2>&1
  echo "Written: $pdf"
done
