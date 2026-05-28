# Transparency

This page shows the grant wallet and AWS cost footprint behind the public
`scfuzzbench` benchmark account.

- Grant source: Zerion API when `ZERION_API_KEY` is configured; otherwise an
  ETH-only public Ethereum RPC plus CoinGecko estimate
- Grant raw data: [`/data/grant-wallet.json`](/data/grant-wallet.json)
- AWS data source: AWS Cost Explorer
- AWS metric: `UnblendedCost`
- Currency: `USD`
- Refresh model: generated during the docs build and published as a static page
- AWS raw data: [`/data/aws-costs.json`](/data/aws-costs.json)

Month-to-date values are estimated and the current UTC day may be partial.

<GrantTransparencyTracker />

<CostTransparencyDashboard />
