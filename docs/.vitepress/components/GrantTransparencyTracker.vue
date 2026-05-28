<script setup lang="ts">
import { computed } from "vue";
import grantWallet from "../generated/grant-wallet.json";

type Portfolio = {
  total_usd: number | null;
  absolute_1d_usd: number | null;
  percent_1d: number | null;
  eth_balance: number | null;
  eth_price_usd: number | null;
  positions_distribution_by_chain: Record<string, number | null>;
  positions_distribution_by_type: Record<string, number | null>;
};

type GrantPayload = {
  available: boolean;
  generated_at_utc: string;
  currency: string;
  address: string;
  public_data_path: string;
  source: string;
  source_label: string;
  coverage: string;
  error?: string;
  zerion_url: string;
  etherscan_url: string;
  round: {
    name: string;
    results_url: string;
    dates: string;
    matching_pool_eth: number;
    total_donated_usd: number;
    networks: string[];
  };
  portfolio: Portfolio;
};

const payload = grantWallet as GrantPayload;

const currencyFormatter = computed(
  () =>
    new Intl.NumberFormat("en-US", {
      style: "currency",
      currency: payload.currency || "USD",
      maximumFractionDigits: 2,
    })
);
const compactCurrencyFormatter = computed(
  () =>
    new Intl.NumberFormat("en-US", {
      style: "currency",
      currency: payload.currency || "USD",
      maximumFractionDigits: 0,
    })
);
const generatedAtFormatter = new Intl.DateTimeFormat("en-US", {
  dateStyle: "medium",
  timeStyle: "short",
  timeZone: "UTC",
});
const ethFormatter = new Intl.NumberFormat("en-US", {
  maximumFractionDigits: 6,
});

const addressShort = computed(() => `${payload.address.slice(0, 6)}...${payload.address.slice(-4)}`);
const generatedAt = computed(() => {
  if (!payload.generated_at_utc) {
    return "Unavailable";
  }
  return generatedAtFormatter.format(new Date(payload.generated_at_utc));
});

const chainRows = computed(() => distributionRows(payload.portfolio.positions_distribution_by_chain));
const typeRows = computed(() => distributionRows(payload.portfolio.positions_distribution_by_type));
const distributionMax = computed(() => Math.max(...chainRows.value.map((row) => row.value), 0));

function distributionRows(source: Record<string, number | null>): Array<{ label: string; value: number }> {
  return Object.entries(source)
    .map(([label, value]) => ({ label, value: value ?? 0 }))
    .filter((row) => row.value > 0)
    .sort((a, b) => b.value - a.value);
}

function formatUsd(value: number | null | undefined): string {
  if (value === null || value === undefined) {
    return "Unavailable";
  }
  return currencyFormatter.value.format(value);
}

function formatCompactUsd(value: number): string {
  return compactCurrencyFormatter.value.format(value);
}

function formatEth(value: number | null): string {
  if (value === null) {
    return "Unavailable";
  }
  return `${ethFormatter.format(value)} ETH`;
}

function formatPercent(value: number | null): string {
  if (value === null) {
    return "Unavailable";
  }
  return `${value >= 0 ? "+" : ""}${value.toFixed(2)}%`;
}

function displayLabel(value: string): string {
  return value
    .split("-")
    .map((part) => part.charAt(0).toUpperCase() + part.slice(1))
    .join(" ");
}
</script>

<template>
  <section class="grant-tracker">
    <div class="grant-tracker__header">
      <img
        src="/images/ethereum-security-qf-grant.png"
        alt="Ethereum Security QF grant"
        class="grant-tracker__badge"
      />
      <div class="grant-tracker__title">
        <p class="grant-tracker__eyebrow">Grant Transparency</p>
        <h2>{{ payload.round.name }}</h2>
        <p>
          Tracking the public payout address connected to the Ethereum security quadratic funding round.
        </p>
      </div>
      <div class="grant-tracker__meta">
        <span>{{ payload.source_label }}</span>
        <span>Updated: {{ generatedAt }}</span>
        <a :href="payload.public_data_path">Raw JSON</a>
      </div>
    </div>

    <div class="grant-tracker__cards">
      <article class="grant-card grant-card--value">
        <p class="grant-card__label">Tracked value</p>
        <p class="grant-card__value">{{ formatUsd(payload.portfolio.total_usd) }}</p>
        <p class="grant-card__meta">{{ payload.coverage }}</p>
      </article>
      <article class="grant-card">
        <p class="grant-card__label">QF matching pool</p>
        <p class="grant-card__value">
          {{ payload.round.matching_pool_eth.toLocaleString("en-US", { maximumFractionDigits: 4 }) }} ETH
        </p>
        <p class="grant-card__meta">{{ payload.round.dates }}</p>
      </article>
      <article class="grant-card">
        <p class="grant-card__label">Round donations</p>
        <p class="grant-card__value">{{ formatCompactUsd(payload.round.total_donated_usd) }}</p>
        <p class="grant-card__meta">{{ payload.round.networks.join(", ") }}</p>
      </article>
    </div>

    <div v-if="!payload.available" class="grant-tracker__notice">
      <strong>Grant wallet data is currently unavailable.</strong>
      <span>{{ payload.error || "The build did not generate a grant wallet payload." }}</span>
    </div>

    <div class="grant-tracker__grid">
      <article class="grant-panel grant-panel--address">
        <div>
          <p class="grant-panel__label">Payout address</p>
          <p class="grant-panel__address">{{ addressShort }}</p>
        </div>
        <code>{{ payload.address }}</code>
        <div class="grant-panel__links">
          <a :href="payload.zerion_url" target="_blank" rel="noopener">Open Zerion</a>
          <a :href="payload.etherscan_url" target="_blank" rel="noopener">Open Etherscan</a>
          <a :href="payload.round.results_url" target="_blank" rel="noopener">Round results</a>
        </div>
      </article>

      <article class="grant-panel">
        <p class="grant-panel__label">Daily movement</p>
        <p class="grant-panel__metric">{{ formatUsd(payload.portfolio.absolute_1d_usd) }}</p>
        <p v-if="payload.portfolio.percent_1d !== null" class="grant-panel__meta">
          {{ formatPercent(payload.portfolio.percent_1d) }} over 24h
        </p>
        <p v-else class="grant-panel__meta">24h change is unavailable for this source.</p>
        <p v-if="payload.portfolio.eth_balance !== null" class="grant-panel__meta">
          ETH balance: {{ formatEth(payload.portfolio.eth_balance) }}
        </p>
      </article>

      <article class="grant-panel grant-panel--wide">
        <div class="grant-panel__heading">
          <div>
            <p class="grant-panel__label">Current composition</p>
            <h3>Tracked value by chain</h3>
          </div>
          <p>{{ chainRows.length || 0 }} tracked {{ chainRows.length === 1 ? "network" : "networks" }}</p>
        </div>
        <div v-if="chainRows.length" class="grant-bars">
          <div v-for="row in chainRows" :key="row.label" class="grant-bar">
            <div class="grant-bar__label">
              <span>{{ displayLabel(row.label) }}</span>
              <strong>{{ formatUsd(row.value) }}</strong>
            </div>
            <div class="grant-bar__track">
              <div
                class="grant-bar__fill"
                :style="{ width: `${distributionMax > 0 ? (row.value / distributionMax) * 100 : 0}%` }"
              />
            </div>
          </div>
        </div>
        <p v-else class="grant-panel__meta">No positive wallet value was reported in the generated payload.</p>
      </article>

      <article class="grant-panel grant-panel--types">
        <p class="grant-panel__label">Position types</p>
        <div v-if="typeRows.length" class="grant-type-list">
          <div v-for="row in typeRows" :key="row.label">
            <span>{{ displayLabel(row.label) }}</span>
            <strong>{{ formatUsd(row.value) }}</strong>
          </div>
        </div>
        <p v-else class="grant-panel__meta">No position categories available.</p>
      </article>
    </div>
  </section>
</template>

<style scoped>
.grant-tracker {
  display: grid;
  gap: 1rem;
  margin: 1.5rem 0 2rem;
}

.grant-tracker__header,
.grant-tracker__meta,
.grant-tracker__cards,
.grant-tracker__grid,
.grant-panel__links,
.grant-panel__heading,
.grant-bar__label,
.grant-type-list div {
  display: flex;
}

.grant-tracker__header {
  align-items: center;
  gap: 1rem;
}

.grant-tracker__badge {
  flex: 0 0 auto;
  height: 72px;
  width: 72px;
}

.grant-tracker__title {
  flex: 1 1 auto;
  min-width: 0;
}

.grant-tracker__eyebrow,
.grant-card__label,
.grant-panel__label {
  color: var(--vp-c-text-2);
  font-size: 0.82rem;
  letter-spacing: 0.08em;
  margin: 0 0 0.32rem;
  text-transform: uppercase;
}

.grant-tracker__title h2,
.grant-panel__heading h3 {
  margin: 0;
}

.grant-tracker__title p,
.grant-card__meta,
.grant-panel__meta,
.grant-panel__heading p,
.grant-tracker__notice span {
  color: var(--vp-c-text-2);
}

.grant-tracker__title p,
.grant-card__meta,
.grant-panel__meta,
.grant-panel__heading p {
  margin: 0.35rem 0 0;
}

.grant-tracker__meta {
  align-items: center;
  color: var(--vp-c-text-2);
  flex-wrap: wrap;
  gap: 0.75rem;
  justify-content: flex-end;
  text-align: right;
}

.grant-tracker__cards {
  gap: 1rem;
}

.grant-card,
.grant-panel,
.grant-tracker__notice {
  background:
    linear-gradient(135deg, rgba(255, 60, 56, 0.10), rgba(45, 212, 191, 0.07)),
    color-mix(in srgb, var(--vp-c-bg-soft) 88%, transparent);
  border: 1px solid var(--vp-c-divider);
  border-radius: 18px;
}

.grant-card {
  flex: 1 1 0;
  min-width: 0;
  padding: 1rem 1.1rem;
}

.grant-card__value,
.grant-panel__metric,
.grant-panel__address {
  font-weight: 700;
  margin: 0;
}

.grant-card__value {
  font-size: 1.75rem;
}

.grant-card--value .grant-card__value {
  color: var(--vp-c-brand-1);
}

.grant-tracker__notice {
  display: grid;
  gap: 0.2rem;
  padding: 0.9rem 1rem;
}

.grant-tracker__grid {
  align-items: stretch;
  gap: 1rem;
}

.grant-panel {
  flex: 1 1 0;
  min-width: 0;
  padding: 1rem;
}

.grant-panel--address,
.grant-panel--wide {
  flex: 1.4 1 0;
}

.grant-panel--types {
  flex: 0.85 1 0;
}

.grant-panel--address {
  display: grid;
  gap: 0.8rem;
}

.grant-panel__address {
  font-family: var(--vp-font-family-mono);
  font-size: 1.25rem;
}

.grant-panel code {
  display: block;
  overflow-wrap: anywhere;
  white-space: normal;
}

.grant-panel__links {
  flex-wrap: wrap;
  gap: 0.6rem;
}

.grant-panel__links a {
  border: 1px solid color-mix(in srgb, var(--vp-c-brand-1) 40%, var(--vp-c-border));
  border-radius: 999px;
  color: var(--vp-c-text-1);
  padding: 0.35rem 0.7rem;
  text-decoration: none;
}

.grant-panel__links a:hover {
  background: color-mix(in srgb, var(--vp-c-brand-soft) 58%, transparent);
  text-decoration: none;
}

.grant-panel__metric {
  font-size: 1.5rem;
}

.grant-panel__heading {
  align-items: end;
  justify-content: space-between;
  gap: 1rem;
}

.grant-bars,
.grant-type-list {
  display: grid;
  gap: 0.85rem;
  margin-top: 1rem;
}

.grant-bar {
  display: grid;
  gap: 0.35rem;
}

.grant-bar__label,
.grant-type-list div {
  align-items: center;
  justify-content: space-between;
  gap: 0.8rem;
}

.grant-bar__track {
  background: color-mix(in srgb, var(--vp-c-text-3) 14%, transparent);
  border-radius: 999px;
  height: 0.72rem;
  overflow: hidden;
}

.grant-bar__fill {
  background: linear-gradient(90deg, #ff3c38, var(--vp-c-brand-1));
  border-radius: 999px;
  height: 100%;
  min-width: 0.45rem;
}

.grant-type-list div {
  border-bottom: 1px solid var(--vp-c-divider);
  padding-bottom: 0.55rem;
}

.grant-type-list div:last-child {
  border-bottom: none;
  padding-bottom: 0;
}

@media (max-width: 960px) {
  .grant-tracker__header,
  .grant-tracker__cards,
  .grant-tracker__grid {
    align-items: stretch;
    flex-direction: column;
  }

  .grant-tracker__meta {
    justify-content: flex-start;
    text-align: left;
  }
}

@media (max-width: 640px) {
  .grant-tracker__header {
    align-items: flex-start;
  }

  .grant-tracker__badge {
    height: 56px;
    width: 56px;
  }

  .grant-panel__heading {
    align-items: start;
    flex-direction: column;
  }
}
</style>
