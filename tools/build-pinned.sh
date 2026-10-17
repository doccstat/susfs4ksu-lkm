#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-2.0-only
set -euo pipefail
root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
cd "$root"
[[ $(uname -s) == Linux && $(uname -m) == x86_64 ]] || {
  echo 'Use a Linux x86-64 Docker builder (or GitHub Actions).' >&2; exit 2;
}
[[ $(git rev-parse --show-toplevel) == "$root" ]] || {
  echo 'Build from a standalone Git checkout of this package.' >&2; exit 2;
}
git diff --quiet HEAD -- || { echo 'Commit tracked changes before building.' >&2; exit 2; }
source config/build.env
[[ $DDK_IMAGE =~ ^ghcr.io/ylarod/ddk-min@sha256:[0-9a-f]{64}$ ]] || {
  echo 'The DDK image must be pinned by digest.' >&2; exit 2;
}
cache="${XDG_CACHE_HOME:-$HOME/.cache}/susfs4ksu-lkm"
mkdir -p "$cache"
work=$(mktemp -d "$cache/build.XXXXXXXX")
printf 'Build evidence: %s\n' "$work"
if [[ -n ${GITHUB_OUTPUT:-} ]]; then printf 'work=%s\n' "$work" >> "$GITHUB_OUTPUT"; fi
source_commit=$(git rev-parse HEAD)
source_tree=$(git rev-parse 'HEAD^{tree}')
epoch=$(git show -s --format=%ct HEAD)
git archive --format=tar HEAD > "$work/source.tar"
docker pull --platform linux/amd64 "$DDK_IMAGE"
for pass in 1 2; do
  mkdir "$work/$pass"
  tar -xf "$work/source.tar" -C "$work/$pass"
  # Fresh containers see the same source path, but share neither build output nor caches.
  docker run --rm --platform linux/amd64 --network none --cap-drop=ALL \
    --security-opt=no-new-privileges --user "$(id -u):$(id -g)" \
    --env HOME=/build --env TMPDIR=/build/.tmp --env TZ=UTC --env LC_ALL=C \
    --env SOURCE_DATE_EPOCH="$epoch" \
    --mount "type=bind,source=$work/$pass,target=/build" --workdir /build \
    "$DDK_IMAGE" bash tools/build-one.sh 2>&1 | tee "$work/pass-$pass.log"
done
for name in "susfs_guard_lkm-$KMI.ko" susfs_insmod; do
  cmp "$work/1/out/$name" "$work/2/out/$name" || {
    echo "Reproducibility check failed: $name; no accepted artifacts produced." >&2
    exit 1
  }
done
mkdir "$work/artifacts"
cp -a "$work/1/out/." "$work/artifacts/"
cp "$work/source.tar" "$work/artifacts/"
{
  printf 'source_commit=%s\nsource_tree=%s\n' "$source_commit" "$source_tree"
  printf 'ddk_image=%s\nkmi=%s\n' "$DDK_IMAGE" "$KMI"
  printf 'source_date_epoch=%s\n' "$epoch"
  printf 'reproduction=two-clean-containers-byte-identical\n'
  printf 'device_validation=not-performed\n'
} > "$work/artifacts/provenance.txt"
(cd "$work/artifacts"; sha256sum ./* > "$work/SHA256SUMS")
mv "$work/SHA256SUMS" "$work/artifacts/"
printf 'Accepted build artifacts: %s/artifacts\n' "$work"
