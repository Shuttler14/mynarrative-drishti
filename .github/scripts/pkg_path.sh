#!/usr/bin/env bash
# Maps a paths-filter package name to its directory.
set -euo pipefail
case "$1" in
  user-svc)         echo "services/user-svc" ;;
  reco-svc)         echo "services/reco-svc" ;;
  shopify-sync)     echo "services/shopify-sync-svc" ;;
  scrapers)         echo "scrapers" ;;
  gap)              echo "ai/gap" ;;
  vtoe)             echo "ai/vtoe" ;;
  ifil)             echo "ai/ore/ifil" ;;
  vton-colab)       echo "vtoe" ;;
  py-observability) echo "libs/py-observability" ;;
  drishti-db)       echo "services/shared/database" ;;
  *) echo "ERROR: unknown package $1" >&2; exit 1 ;;
esac
