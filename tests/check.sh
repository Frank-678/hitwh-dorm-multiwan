#!/bin/sh

set -eu

ROOT="$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)"

find "$ROOT" -type f \( -name '*.sh' -o -name '*.command' -o -path '*/usr/sbin/*' -o -path '*/init.d/*' -o -path '*/hotplug.d/*' \) |
while IFS= read -r FILE; do
	sh -n "$FILE"
done

grep -q "config globals 'main'" "$ROOT/files/etc/config/hitwh_mwan"
grep -q 'destroy table inet hitwh_mwan' "$ROOT/files/usr/share/nftables.d/ruleset-post/90-hitwh-mwan.nft"
grep -q 'hitwh-mwan add' "$ROOT/README.md"

if command -v shellcheck >/dev/null 2>&1; then
	find "$ROOT" -type f \( -name '*.sh' -o -name '*.command' -o -path '*/usr/sbin/*' -o -path '*/init.d/*' -o -path '*/hotplug.d/*' \) -print0 |
		xargs -0 shellcheck --shell=sh --severity=error
fi

echo "All checks passed."
