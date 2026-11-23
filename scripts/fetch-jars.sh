#!/usr/bin/env bash
# Downloads the Spark image's jars (images/spark/jars.sha256) into a directory and checks each
# SHA-256 (make jars). A jar already there with the right sum is kept, so a cached directory
# needs no network. Each jar is tried from Maven Central, then from its Google Cloud Storage
# mirror, up to 3 rounds with a growing pause; the sum decides, wherever the bytes came from.
# A wrong answer is described (status, type, size, first bytes) before the next try (decision 36).
# Usage: fetch-jars.sh <sums file> <directory>
set -euo pipefail

sums=$1 dir=$2
# JAR_MIRRORS (space-separated) overrides the list, for tests.
read -r -a mirrors <<< "${JAR_MIRRORS:-https://repo1.maven.org/maven2 https://maven-central.storage-download.googleapis.com/maven2}"
mkdir -p "$dir"

ok() { [ -f "$1" ] && [ "$(sha256sum "$1" | cut -d' ' -f1)" = "$2" ]; }

grep -v '^#' "$sums" | while read -r sum path; do
  [ -n "$sum" ] || continue
  jar="$dir/$(basename "$path")"
  if ok "$jar" "$sum"; then echo "cached $(basename "$jar")"; continue; fi
  done_=false
  for round in 1 2 3; do
    for mirror in "${mirrors[@]}"; do
      part="$jar.part"
      info=$(curl -sS -L --retry 3 --retry-delay 2 --connect-timeout 20 -o "$part" \
             -w '%{http_code} %{content_type} %{size_download}' "$mirror/$path" 2>&1) || true
      if ok "$part" "$sum"; then
        mv "$part" "$jar"; echo "fetched $(basename "$jar") from $mirror"; done_=true; break
      fi
      got=$( [ -f "$part" ] && sha256sum "$part" | cut -d' ' -f1 || echo none)
      echo "wrong answer for $(basename "$jar") from $mirror (round $round): $info, sha256 $got" >&2
      [ -f "$part" ] && head -c 300 "$part" | tr -c '[:print:]\n' '.' >&2 && echo >&2
      rm -f "$part"
    done
    $done_ && break
    sleep $((round * 10))
  done
  $done_ || { echo "could not fetch $(basename "$jar") with sha256 $sum" >&2; exit 1; }
done
