<script setup lang="ts">
import { computed } from "vue";
import awsCosts from "../generated/aws-costs.json";
import grantWallet from "../generated/grant-wallet.json";

type MonthBucket = {
  key: string;
  label: string;
  total_usd: number;
  estimated: boolean;
};

type CostPayload = {
  available: boolean;
  generated_at_utc: string;
  currency: string;
  public_data_path: string;
  history: {
    months: MonthBucket[];
  };
};

type GrantPayload = {
  available: boolean;
  generated_at_utc: string;
  currency: string;
  address: string;
  public_data_path: string;
  source_label: string;
  coverage: string;
  zerion_url: string;
  etherscan_url: string;
  portfolio: {
    total_usd: number | null;
  };
};

const costs = awsCosts as CostPayload;
const wallet = grantWallet as GrantPayload;

const currencyFormatter = new Intl.NumberFormat("en-US", {
  style: "currency",
  currency: wallet.currency || costs.currency || "USD",
  maximumFractionDigits: 2,
});
const updatedAtFormatter = new Intl.DateTimeFormat("en-US", {
  dateStyle: "medium",
  timeStyle: "short",
  timeZone: "UTC",
});

const historyMonths = computed(() => costs.history?.months ?? []);
const trackedFunding = computed<number | null>(() => {
  const value = wallet.portfolio?.total_usd;
  return wallet.available && typeof value === "number" && Number.isFinite(value) ? value : null;
});
const awsCostsToDate = computed<number | null>(() => {
  if (!costs.available) {
    return null;
  }
  return historyMonths.value.reduce((sum, month) => sum + month.total_usd, 0);
});
const estimatedRunway = computed<number | null>(() => {
  if (trackedFunding.value === null || awsCostsToDate.value === null) {
    return null;
  }
  return trackedFunding.value - awsCostsToDate.value;
});
const cashFlowRows = computed(() =>
  [...historyMonths.value]
    .reverse()
    .map((month) => ({ ...month, cash_flow_usd: -month.total_usd }))
);
const firstCostMonth = computed(() => {
  const firstNonZero = historyMonths.value.find((month) => month.total_usd !== 0);
  return firstNonZero?.label ?? historyMonths.value[0]?.label ?? null;
});
const hasPositiveCashFlow = computed(() => cashFlowRows.value.some((month) => month.cash_flow_usd > 0));
const shortAddress = computed(() => {
  if (!wallet.address) {
    return "Unavailable";
  }
  return `${wallet.address.slice(0, 6)}…${wallet.address.slice(-4)}`;
});

function formatUsd(value: number | null): string {
  return value === null ? "Unavailable" : currencyFormatter.format(value);
}

function formatCashFlow(value: number): string {
  if (Math.abs(value) < 0.005) {
    return currencyFormatter.format(0);
  }
  const formatted = currencyFormatter.format(value);
  return value > 0 ? `+${formatted}` : formatted;
}

function formatUpdatedAt(value: string): string {
  if (!value) {
    return "Update unavailable";
  }
  const date = new Date(value);
  return Number.isNaN(date.getTime())
    ? "Update unavailable"
    : `Updated ${updatedAtFormatter.format(date)} UTC`;
}
</script>

<template>
  <section class="transparency-dashboard" aria-label="scfuzzbench financial transparency">
    <dl class="finance-summary">
      <div class="finance-summary__item">
        <dt>Tracked funding</dt>
        <dd>{{ formatUsd(trackedFunding) }}</dd>
        <dd class="finance-summary__meta">
          {{ wallet.available ? wallet.coverage : "Wallet data is unavailable" }}
        </dd>
      </div>

      <div class="finance-summary__item">
        <dt>AWS costs to date</dt>
        <dd>{{ formatUsd(awsCostsToDate) }}</dd>
        <dd class="finance-summary__meta">
          <template v-if="costs.available && firstCostMonth">Published since {{ firstCostMonth }}</template>
          <template v-else-if="costs.available">No AWS costs reported</template>
          <template v-else>AWS cost data is unavailable</template>
        </dd>
      </div>

      <div
        class="finance-summary__item finance-summary__item--runway"
        :class="{ 'finance-summary__item--shortfall': estimatedRunway !== null && estimatedRunway < 0 }"
      >
        <dt>Estimated runway</dt>
        <dd>{{ formatUsd(estimatedRunway) }}</dd>
        <dd class="finance-summary__meta">Tracked funding minus AWS costs</dd>
      </div>
    </dl>

    <p class="runway-assumption">
      <strong>Runway assumption:</strong>
      The current tracked wallet will reimburse maintainers for all AWS costs shown.
    </p>

    <section class="cash-flow" aria-labelledby="cash-flow-title">
      <header class="cash-flow__heading">
        <div>
          <p class="cash-flow__eyebrow">Cash flow</p>
          <h2 id="cash-flow-title">Monthly AWS outflow</h2>
        </div>
      </header>

      <div v-if="!costs.available" class="cash-flow__empty" role="status">
        <strong>Monthly costs are unavailable.</strong>
        <span>The next successful docs build will refresh this section.</span>
      </div>

      <div v-else-if="!cashFlowRows.length" class="cash-flow__empty" role="status">
        <strong>No AWS costs reported.</strong>
        <span>Monthly outflow will appear here when costs are published.</span>
      </div>

      <ol v-else class="cash-flow__list" aria-label="Monthly AWS cash flow">
        <li class="cash-flow__row cash-flow__row--header" aria-hidden="true">
          <span>Month</span>
          <span>AWS outflow</span>
        </li>
        <li v-for="month in cashFlowRows" :key="month.key" class="cash-flow__row">
          <span class="cash-flow__month">
            <span>{{ month.label }}</span>
            <span v-if="month.estimated" class="cash-flow__estimate">MTD estimate</span>
          </span>
          <span
            class="cash-flow__amount"
            :class="{
              'cash-flow__amount--inflow': month.cash_flow_usd > 0,
              'cash-flow__amount--neutral': month.cash_flow_usd === 0,
            }"
          >
            <span class="visually-hidden">AWS cash flow: </span>
            {{ formatCashFlow(month.cash_flow_usd) }}
          </span>
        </li>
      </ol>

      <p v-if="hasPositiveCashFlow" class="cash-flow__note">
        Positive values reflect credits, refunds, or other downward adjustments.
      </p>
    </section>

    <footer class="transparency-sources">
      <div class="transparency-sources__wallet">
        <span>Project wallet</span>
        <code :title="wallet.address">{{ shortAddress }}</code>
      </div>
      <nav class="transparency-sources__links" aria-label="Financial data sources">
        <a :href="wallet.etherscan_url" target="_blank" rel="noopener">View on Etherscan</a>
        <a :href="wallet.zerion_url" target="_blank" rel="noopener">View in Zerion</a>
        <a :href="wallet.public_data_path">Wallet data</a>
        <a :href="costs.public_data_path">AWS cost data</a>
      </nav>
      <div class="transparency-sources__meta">
        <span>Wallet: {{ wallet.source_label }} · {{ formatUpdatedAt(wallet.generated_at_utc) }}</span>
        <span>AWS: Cost Explorer · {{ formatUpdatedAt(costs.generated_at_utc) }}</span>
      </div>
    </footer>
  </section>
</template>

<style scoped>
.transparency-dashboard {
  container-name: transparency;
  container-type: inline-size;
  display: grid;
  gap: 1.5rem;
  margin: 1.5rem 0 2rem;
  min-width: 0;
  --finance-shortfall: oklch(0.55 0.16 29);
  --finance-inflow: oklch(0.48 0.12 168);
}

:global(.dark) .transparency-dashboard {
  --finance-shortfall: oklch(0.72 0.14 29);
  --finance-inflow: oklch(0.76 0.12 168);
}

.finance-summary {
  background: color-mix(in srgb, var(--vp-c-bg-soft) 88%, transparent);
  border: 1px solid var(--vp-c-divider);
  border-radius: 16px;
  display: grid;
  grid-template-columns: repeat(3, minmax(0, 1fr));
  margin: 0;
  overflow: hidden;
}

.finance-summary__item {
  display: grid;
  gap: 0.35rem;
  min-width: 0;
  padding: 1.1rem;
}

.finance-summary__item + .finance-summary__item {
  border-left: 1px solid var(--vp-c-divider);
}

.finance-summary__item--runway {
  background: color-mix(in srgb, var(--vp-c-brand-soft) 54%, transparent);
}

.finance-summary dt,
.cash-flow__eyebrow {
  color: var(--vp-c-text-2);
  font-size: 0.75rem;
  font-weight: 650;
  letter-spacing: 0.08em;
  line-height: 1.35;
  margin: 0;
  text-transform: uppercase;
}

.finance-summary dd {
  font-size: 1.65rem;
  font-variant-numeric: tabular-nums;
  font-weight: 700;
  letter-spacing: -0.025em;
  line-height: 1.15;
  margin: 0;
  overflow-wrap: anywhere;
}

.finance-summary__item--runway dd:not(.finance-summary__meta) {
  color: var(--vp-c-brand-1);
}

.finance-summary__item--shortfall dd:not(.finance-summary__meta) {
  color: var(--finance-shortfall);
}

.finance-summary dd.finance-summary__meta {
  color: var(--vp-c-text-2);
  font-size: 0.78rem;
  font-weight: 400;
  letter-spacing: normal;
  line-height: 1.45;
}

.runway-assumption {
  align-items: baseline;
  background: color-mix(in srgb, var(--vp-c-brand-soft) 44%, transparent);
  border: 1px solid color-mix(in srgb, var(--vp-c-brand-1) 28%, var(--vp-c-divider));
  border-radius: 12px;
  color: var(--vp-c-text-2);
  display: flex;
  flex-wrap: wrap;
  gap: 0.3rem;
  margin: -0.5rem 0 0;
  padding: 0.8rem 0.9rem;
}

.runway-assumption strong {
  color: var(--vp-c-text-1);
}

.cash-flow {
  display: grid;
  gap: 1rem;
  min-width: 0;
}

.cash-flow__heading {
  align-items: end;
  display: flex;
  gap: 1rem;
  justify-content: space-between;
  min-width: 0;
}

.cash-flow__heading h2 {
  font-size: 1.25rem;
  margin: 0.2rem 0 0;
}

.cash-flow__list {
  border-bottom: 1px solid var(--vp-c-divider);
  border-top: 1px solid var(--vp-c-divider);
  list-style: none;
  margin: 0;
  padding: 0;
}

.cash-flow__row {
  align-items: center;
  display: grid;
  gap: 1rem;
  grid-template-columns: minmax(0, 1fr) minmax(7.5rem, auto);
  min-width: 0;
  padding: 0.72rem 0.8rem;
}

.cash-flow__row + .cash-flow__row {
  border-top: 1px solid var(--vp-c-divider);
}

.cash-flow__row:not(.cash-flow__row--header):hover {
  background: color-mix(in srgb, var(--vp-c-brand-soft) 25%, transparent);
}

.cash-flow__row--header {
  color: var(--vp-c-text-3);
  font-size: 0.72rem;
  font-weight: 650;
  letter-spacing: 0.07em;
  padding-bottom: 0.55rem;
  padding-top: 0.55rem;
  text-transform: uppercase;
}

.cash-flow__row > :last-child {
  text-align: right;
}

.cash-flow__month {
  align-items: center;
  display: flex;
  flex-wrap: wrap;
  gap: 0.5rem;
  min-width: 0;
}

.cash-flow__estimate {
  align-items: center;
  color: var(--vp-c-text-2);
  display: inline-flex;
  font-size: 0.72rem;
  gap: 0.35rem;
  white-space: nowrap;
}

.cash-flow__estimate::before {
  background: var(--vp-c-brand-1);
  border-radius: 50%;
  content: "";
  height: 0.4rem;
  width: 0.4rem;
}

.cash-flow__amount {
  font-family: var(--vp-font-family-mono);
  font-size: 0.9rem;
  font-variant-numeric: tabular-nums;
  font-weight: 600;
}

.cash-flow__amount--inflow {
  color: var(--finance-inflow);
}

.cash-flow__amount--neutral {
  color: var(--vp-c-text-3);
}

.cash-flow__empty {
  background: color-mix(in srgb, var(--vp-c-bg-soft) 88%, transparent);
  border: 1px solid var(--vp-c-divider);
  border-radius: 14px;
  display: grid;
  gap: 0.2rem;
  padding: 1rem;
}

.cash-flow__empty span,
.cash-flow__note {
  color: var(--vp-c-text-2);
}

.cash-flow__note {
  font-size: 0.82rem;
  margin: 0;
}

.transparency-sources {
  border-top: 1px solid var(--vp-c-divider);
  display: grid;
  gap: 0.75rem;
  min-width: 0;
  padding-top: 1rem;
}

.transparency-sources__wallet,
.transparency-sources__links,
.transparency-sources__meta {
  align-items: center;
  display: flex;
  flex-wrap: wrap;
  gap: 0.55rem 0.9rem;
  min-width: 0;
}

.transparency-sources__wallet > span {
  color: var(--vp-c-text-2);
  font-size: 0.78rem;
  font-weight: 650;
  letter-spacing: 0.06em;
  text-transform: uppercase;
}

.transparency-sources__wallet code {
  overflow-wrap: anywhere;
}

.transparency-sources__links a {
  font-size: 0.84rem;
  text-decoration: underline;
  text-decoration-color: color-mix(in srgb, currentColor 45%, transparent);
  text-underline-offset: 3px;
}

.transparency-sources__meta {
  color: var(--vp-c-text-3);
  display: grid;
  font-size: 0.75rem;
  gap: 0.15rem;
  line-height: 1.5;
}

.visually-hidden {
  clip: rect(0 0 0 0);
  clip-path: inset(50%);
  height: 1px;
  overflow: hidden;
  position: absolute;
  white-space: nowrap;
  width: 1px;
}

@container transparency (max-width: 620px) {
  .finance-summary {
    grid-template-columns: 1fr;
  }

  .finance-summary__item + .finance-summary__item {
    border-left: 0;
    border-top: 1px solid var(--vp-c-divider);
  }

  .cash-flow__heading {
    align-items: start;
    flex-direction: column;
    gap: 0.35rem;
  }

  .transparency-sources__meta {
    align-items: start;
    gap: 0.15rem;
  }
}

@container transparency (max-width: 360px) {
  .cash-flow__row {
    gap: 0.65rem;
    grid-template-columns: minmax(0, 1fr) minmax(6.6rem, auto);
    padding-left: 0.25rem;
    padding-right: 0.25rem;
  }
}
</style>
