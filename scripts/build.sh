#!/bin/bash

set -euo pipefail

BASEDIR=$(realpath "$(dirname "$0")/..")
PACKAGE_NAME="layer_slider"
BUILD_DIR="${BASEDIR}/build"
VERSION=$(grep -E '^version=' "${BASEDIR}/metadata.txt" | cut -d'=' -f2)
VERSION=$(echo "$VERSION" | xargs)
RELEASE_DIR="${BASEDIR}/release"
RELEASE_NAME="${PACKAGE_NAME}-${VERSION}"

# clean build dir
rm -rf "${BUILD_DIR}"

# copy plugin files to build dir
cd "${BASEDIR}"
mkdir -p "${BUILD_DIR}/${PACKAGE_NAME}"
rsync -amR \
      metadata.txt \
      __init__.py \
      README.md \
      LICENSE \
      CITATION.cff \
      assets \
      "${BUILD_DIR}/${PACKAGE_NAME}/"
rsync -am --include='*/' \
      --include='*.py' \
      --include='*.pyi' \
      --include='*.ui' \
      --exclude='*' \
      "src/" "${BUILD_DIR}/${PACKAGE_NAME}/src/"
echo "build successful"

# create zip in release version subdir
rm -rf "${RELEASE_DIR}/${RELEASE_NAME}"
mkdir -p "${RELEASE_DIR}/${RELEASE_NAME}"
cd "${BUILD_DIR}" # change to build dir so paths in zip are relative to that
zip -q -r "${RELEASE_DIR}/${RELEASE_NAME}/${RELEASE_NAME}.zip" "${PACKAGE_NAME}"

# add checksum for release
cd "${RELEASE_DIR}/${RELEASE_NAME}" # change to dir so checksum path is relative to that
md5sum "${RELEASE_NAME}.zip" > "${RELEASE_NAME}.zip.md5"
md5sum -c "${RELEASE_NAME}.zip.md5"

echo "release successful: ${RELEASE_NAME}"
