#!/usr/bin/env python3
"""Sign a catalog payload and attach its Ed25519 signature to the manifest."""

from __future__ import annotations

import argparse
import base64
import json
import os
import pathlib

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--catalog", required=True, type=pathlib.Path)
    parser.add_argument("--manifest", required=True, type=pathlib.Path)
    parser.add_argument("--key-id", default="catalog-2026-01")
    args = parser.parse_args()

    encoded_key = os.environ.get("CATALOG_SIGNING_PRIVATE_KEY", "").strip()
    if not encoded_key:
        raise RuntimeError("CATALOG_SIGNING_PRIVATE_KEY is not configured")

    private_key = Ed25519PrivateKey.from_private_bytes(base64.b64decode(encoded_key))
    signature = private_key.sign(args.catalog.read_bytes())
    manifest = json.loads(args.manifest.read_text())
    manifest["signingKeyID"] = args.key_id
    manifest["signature"] = base64.b64encode(signature).decode("ascii")
    args.manifest.write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
