#!/bin/bash
# Print the CHANGELOG.md section body for one version (Keep a Changelog format).
#
# Usage:   changelog-section.sh <version>     # "1.0.0" or "v1.0.0"
#          changelog-section.sh --selftest
# Output:  the lines under "## [<version>] ..." up to the next "## " heading, with
#          leading/trailing blank lines trimmed. Exits 1 (no output) if not found.
# Used by .github/workflows/release.yml to build the GitHub release notes.
set -euo pipefail

extract() {  # <version> <changelog-file>
    awk -v ver="$1" '
        index($0, "## [" ver "]") == 1 { grab = 1; next }
        grab && /^## /              { grab = 0 }
        grab                        { buf = buf $0 "\n" }
        END {
            sub(/^\n+/, "", buf); sub(/\n+$/, "", buf)
            if (buf == "") exit 1
            print buf
        }
    ' "$2"
}

if [ "${1:-}" = "--selftest" ]; then
    tmp=$(mktemp)
    printf '## [Unreleased]\n\n## [1.2.0] - 2026-01-01\n\n### Added\n- thing A\n- thing B\n\n## [1.1.0] - 2025-12-01\n\n### Fixed\n- old bug\n' > "$tmp"
    out=$(extract 1.2.0 "$tmp")
    echo "$out" | grep -q 'thing A' || { echo "FAIL: missing 'thing A'"; exit 1; }
    echo "$out" | grep -q 'thing B' || { echo "FAIL: missing 'thing B'"; exit 1; }
    if echo "$out" | grep -q 'old bug';    then echo "FAIL: bled into 1.1.0"; exit 1; fi
    if echo "$out" | grep -q 'Unreleased'; then echo "FAIL: grabbed Unreleased"; exit 1; fi
    [ "$(extract 1.1.0 "$tmp")" = "### Fixed"$'\n'"- old bug" ] || { echo "FAIL: 1.1.0 body wrong"; exit 1; }
    if extract 9.9.9 "$tmp" >/dev/null;    then echo "FAIL: missing version did not exit 1"; exit 1; fi
    rm -f "$tmp"
    echo "changelog-section selftest: PASS"
    exit 0
fi

ver="${1:?usage: changelog-section.sh <version>|--selftest}"
extract "${ver#v}" "$(dirname "$0")/../CHANGELOG.md"
