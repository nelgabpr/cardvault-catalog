# CardVault AI production catalog

This directory is generated and deployed by `.github/workflows/update-catalog.yml`
three times daily. The iOS app should point `CATALOG_MANIFEST_URL` at the deployed
`manifest.json`. The manifest schema, version, minimum app version, SHA-256,
card count, and Ed25519 signature are validated on-device before an atomic
install.

Pricing is stored by physical finish so normal, reverse-holofoil, holofoil, and
edition-specific estimates are not treated as interchangeable. Each manifest
reports priced-card coverage and the total number of finish-specific options.
When an upstream refresh is incomplete, the builder preserves exact-card,
exact-finish prices for up to 90 days with their original timestamps, so the app
can label them stale instead of silently dropping them. Artwork URLs are also
preserved from the last known good release.

Every run produces `health.json`, an immutable versioned catalog, and a rollback
manifest. Deployment is stopped if card count drops more than 5%, artwork drops
more than 2%, or price coverage drops more than two percentage points after the
last-known-good merge. Provider IDs and the exact upstream Git revision are
included for future provider adapters and audits.

Required repository setup:

1. Enable GitHub Pages with **GitHub Actions** as the source.
2. Add the optional `POKEMON_TCG_API_KEY` repository secret for reliable API limits.
3. Add `CATALOG_SIGNING_PRIVATE_KEY`, a base64-encoded raw Ed25519 private key.
   Keep the corresponding public key in the iOS app's trusted-key registry.
4. Set the Xcode build setting `CATALOG_MANIFEST_URL` to the published HTTPS URL.

The current pipeline uses only free/open data and GitHub infrastructure. No
commercial price-provider subscription is required to run it.
