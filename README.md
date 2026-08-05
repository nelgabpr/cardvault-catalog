# CardVault AI production catalog

This directory is generated and deployed by `.github/workflows/update-catalog.yml`
three times daily. The iOS app should point `CATALOG_MANIFEST_URL` at the deployed
`manifest.json`. The manifest SHA-256, schema, version, minimum app version, and
card count are validated on-device before an atomic install.

Pricing is stored by physical finish so normal, reverse-holofoil, holofoil, and
edition-specific estimates are not treated as interchangeable. Each manifest
reports priced-card coverage and the total number of finish-specific options.

Required repository setup:

1. Enable GitHub Pages with **GitHub Actions** as the source.
2. Add the optional `POKEMON_TCG_API_KEY` repository secret for reliable API limits.
3. Set the Xcode build setting `CATALOG_MANIFEST_URL` to the published HTTPS URL.
