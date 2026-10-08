#!/usr/bin/env bash
# Build the statically linked bd that versions.json pins as "bd_static" (kittrial-5bb.161).
#
# The bd release that versions.json pins as "bd" is linked against glibc 2.34 and cannot
# start on RHEL 8 (glibc 2.28). Upstream publishes no static build of 1.2.2, so this script
# builds one from the same source commit, without cgo: the result needs no C library at
# all. Without cgo bd supports server mode only (upstream's beads_nocgo.go says so); that is
# the only mode this kit uses (bd init --server --external).
#
# Usage:  tools/build_bd_static.sh WORKDIR
#   WORKDIR  an absolute path to a new or empty directory; everything is written there.
#
# It needs docker and network access for two things only, both checked: the Go image, by
# digest, and the Go modules, each verified against the source's go.sum. The compile itself
# runs with the network off. Nothing is installed on the host. Output, under WORKDIR/out:
#   bd                                    the binary
#   beads_1.2.2_linux_amd64_static.tar.gz the archive versions.json pins (member "bd")
#   build-record.json                     what was used and what came out, with checksums
# Two runs are expected to give the same archive, byte for byte; the script prints the
# sha256 so that it can be compared with versions.json.
set -euo pipefail

GOLANG_IMAGE='golang@sha256:b54cbf583d390341599d7bcbc062425c081105cc5ef6d170ced98ef9d047c716'   # golang:1.26.2
BEADS_REPOSITORY='https://github.com/gastownhall/beads.git'
BEADS_TAG='v1.2.2'
BEADS_COMMIT='6c124203e771433a3550c348771a5b5e27fd3c21'
BEADS_COMMIT_TIME='2026-08-15T03:43:22Z'
GO_SUM_SHA256='ad753874d566d22c81da097ed3d8d59f2f17ff6e69a437aca914ad178a488efb'
VERSION='1.2.2'
ARCHIVE="beads_${VERSION}_linux_amd64_static.tar.gz"
# Upstream's release flags (.goreleaser.yml), with cgo off and -trimpath for a repeatable result.
TAGS='gms_pure_go'
LDFLAGS="-s -w -X main.Version=${VERSION} -X main.Build=${BEADS_COMMIT:0:9} -X main.Commit=${BEADS_COMMIT} -X main.Branch=HEAD"

WORK="${1:?usage: build_bd_static.sh WORKDIR (an absolute path to a new or empty directory)}"
case "$WORK" in /*) ;; *) echo "WORKDIR must be an absolute path" >&2; exit 2;; esac
mkdir -p "$WORK"
if [ -n "$(ls -A "$WORK")" ]; then echo "WORKDIR must be empty: $WORK" >&2; exit 2; fi
mkdir -p "$WORK/src" "$WORK/out" "$WORK/home" "$WORK/gomod" "$WORK/gocache"

run() {   # run NETWORK COMMAND: one container, removed afterwards, as the calling user
    local network="$1"; shift
    docker run --rm --network "$network" --user "$(id -u):$(id -g)" \
        -e HOME=/work/home -e GOMODCACHE=/work/gomod -e GOCACHE=/work/gocache \
        -e GOFLAGS=-mod=readonly -e GOTOOLCHAIN=local -e CGO_ENABLED=0 -e GOOS=linux -e GOARCH=amd64 \
        -v "$WORK:/work" -w /work/src "$GOLANG_IMAGE" sh -ec "$1"
}

echo "== 1. the source at ${BEADS_TAG}, and its modules (network on)" >&2
run bridge "
    git clone -q --depth 1 --branch '${BEADS_TAG}' '${BEADS_REPOSITORY}' beads
    cd beads
    test \"\$(git rev-parse HEAD)\" = '${BEADS_COMMIT}' || { echo 'the tag is not the pinned commit' >&2; exit 1; }
    echo '${GO_SUM_SHA256}  go.sum' | sha256sum -c - >&2
    go mod download
    go mod verify >&2
"

echo "== 2. the build (network off)" >&2
run none "
    cd beads
    test -z \"\$(git status --porcelain)\" || { echo 'the source tree is not clean' >&2; exit 1; }
    GOPROXY=off nice -n 10 go build -p 4 -trimpath -tags '${TAGS}' -ldflags '${LDFLAGS}' -o /work/out/bd ./cmd/bd
    go version -m /work/out/bd > /work/out/go-version-m.txt
    go version > /work/out/go-version.txt
    cd /work/out
    # The archive in the shape versions.json expects (one member, bd), with nothing in it that
    # changes from run to run: fixed time, owner and order, and no time in the gzip header.
    tar --sort=name --mtime='${BEADS_COMMIT_TIME}' --owner=0 --group=0 --numeric-owner --mode=0755 -cf - bd | gzip -n -9 > '${ARCHIVE}'
"

"$WORK/out/bd" --version >&2
binary_sha="$(sha256sum "$WORK/out/bd" | cut -d' ' -f1)"
archive_sha="$(sha256sum "$WORK/out/$ARCHIVE" | cut -d' ' -f1)"
cat > "$WORK/out/build-record.json" <<EOF
{
  "schema_version": 1,
  "what": "bd ${VERSION}, statically linked (CGO_ENABLED=0), for hosts whose glibc is older than 2.34",
  "image": "${GOLANG_IMAGE}",
  "go": "$(cat "$WORK/out/go-version.txt")",
  "source": {"repository": "${BEADS_REPOSITORY}", "tag": "${BEADS_TAG}", "commit": "${BEADS_COMMIT}",
             "commit_time": "${BEADS_COMMIT_TIME}", "go_sum_sha256": "${GO_SUM_SHA256}"},
  "build": {"CGO_ENABLED": "0", "GOOS": "linux", "GOARCH": "amd64", "trimpath": true, "tags": "${TAGS}",
            "ldflags": "${LDFLAGS}", "network_during_compile": "none"},
  "binary_sha256": "${binary_sha}",
  "archive": "${ARCHIVE}",
  "archive_sha256": "${archive_sha}",
  "archive_member": "bd"
}
EOF
echo "${binary_sha}  bd"
echo "${archive_sha}  ${ARCHIVE}"
