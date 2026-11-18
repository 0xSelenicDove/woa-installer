#!/bin/sh
# Cross-builds static aarch64 mkntfs, ntfs-3g and mkfs.fat for the phone.
# Run on Debian/Ubuntu (see .github/workflows/phone-tools.yml). Output: dist/phone-tools-aarch64.tar.gz
set -eu
NTFS_VER=2022.10.3
FAT_VER=4.2
export CC=aarch64-linux-gnu-gcc
OUT="$PWD/dist/stage"
rm -rf dist build; mkdir -p "$OUT" build; cd build

curl -fsSLO "https://download.tuxfamily.org/ntfs-3g/ntfs-3g_ntfsprogs-$NTFS_VER.tgz" 2>/dev/null || \
  curl -fsSLO "https://tuxera.com/opensource/ntfs-3g_ntfsprogs-$NTFS_VER.tgz"
tar xf "ntfs-3g_ntfsprogs-$NTFS_VER.tgz"
(cd "ntfs-3g_ntfsprogs-$NTFS_VER" &&
  ./configure --host=aarch64-linux-gnu --disable-shared --enable-static \
    --with-fuse=internal --disable-ntfsprogs-static --disable-library \
    --disable-mount-helper --disable-mtab --without-uuid \
    LDFLAGS=-static >/dev/null &&
  make -j"$(nproc)" LDFLAGS=-all-static >/dev/null &&
  cp src/ntfs-3g "$OUT/ntfs-3g" && cp ntfsprogs/mkntfs "$OUT/mkntfs")

curl -fsSLO "https://github.com/dosfstools/dosfstools/releases/download/v$FAT_VER/dosfstools-$FAT_VER.tar.gz"
tar xf "dosfstools-$FAT_VER.tar.gz"
(cd "dosfstools-$FAT_VER" &&
  ./configure --host=aarch64-linux-gnu LDFLAGS=-static >/dev/null &&
  make -j"$(nproc)" >/dev/null &&
  cp src/mkfs.fat "$OUT/mkfs.fat")

aarch64-linux-gnu-strip "$OUT"/* || true
for f in "$OUT"/*; do file "$f" | grep -q "statically linked" || { echo "not static: $f"; exit 1; }; done
cd .. && tar -C "$OUT" -czf dist/phone-tools-aarch64.tar.gz . 
(cd dist && sha256sum phone-tools-aarch64.tar.gz > phone-tools-aarch64.tar.gz.sha256)
ls -l dist
