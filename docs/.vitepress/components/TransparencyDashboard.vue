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

type ServiceCost = {
  service: string;
  cost_usd: number;
  share_of_total_pct: number;
};

type CostPayload = {
  available: boolean;
  generated_at_utc: string;
  currency: string;
  public_data_path: string;
  history: {
    months: MonthBucket[];
  };
  current_month: {
    label: string;
    estimated: boolean;
    by_service: ServiceCost[];
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
const compactCurrencyFormatter = new Intl.NumberFormat("en-US", {
  style: "currency",
  currency: wallet.currency || costs.currency || "USD",
  notation: "compact",
  maximumFractionDigits: 0,
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
  historyMonths.value
    .slice(-6)
    .map((month) => ({ ...month, cash_flow_usd: -month.total_usd }))
);
const monthlyMax = computed(() =>
  Math.max(...cashFlowRows.value.map((month) => Math.abs(month.cash_flow_usd)), 0)
);
const cashFlowPeriod = computed(() =>
  cashFlowRows.value.length === 1 ? "Latest month" : `Latest ${cashFlowRows.value.length} months`
);
const serviceRows = computed<ServiceCost[]>(() => {
  const rows = [...(costs.current_month?.by_service ?? [])]
    .filter(
      (item) =>
        item.service &&
        Number.isFinite(item.cost_usd) &&
        Math.abs(item.cost_usd) >= 0.005
    )
    .sort((left, right) => Math.abs(right.cost_usd) - Math.abs(left.cost_usd));

  if (rows.length <= 6) {
    return rows;
  }

  const remainder = rows.slice(5).reduce((sum, item) => sum + item.cost_usd, 0);
  return [
    ...rows.slice(0, 5),
    {
      service: "Other services",
      cost_usd: remainder,
      share_of_total_pct: 0,
    },
  ];
});
const serviceMax = computed(() =>
  Math.max(...serviceRows.value.map((item) => Math.abs(item.cost_usd)), 0)
);
const firstCostMonth = computed(() => {
  const firstNonZero = historyMonths.value.find((month) => month.total_usd !== 0);
  return firstNonZero?.label ?? historyMonths.value[0]?.label ?? null;
});

function formatUsd(value: number | null): string {
  return value === null ? "Unavailable" : currencyFormatter.format(value);
}

function formatCompactCashFlow(value: number): string {
  if (Math.abs(value) < 0.005) {
    return currencyFormatter.format(0);
  }
  const formatted = compactCurrencyFormatter.format(Math.abs(value));
  return value > 0 ? `+${formatted}` : `−${formatted}`;
}

function monthAbbreviation(label: string): string {
  return label.split(/\s+/)[0] || label;
}

function monthlyBarHeight(value: number): string {
  if (monthlyMax.value === 0 || Math.abs(value) < 0.005) {
    return "0%";
  }
  const percentage = (Math.abs(value) / monthlyMax.value) * 100;
  return `${Math.max(percentage, 3)}%`;
}

function serviceBarWidth(value: number): string {
  if (serviceMax.value === 0) {
    return "0%";
  }
  return `${Math.max((Math.abs(value) / serviceMax.value) * 100, 1)}%`;
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

    <section class="finance-charts" aria-label="AWS cost charts">
      <article class="chart-card" aria-labelledby="cash-flow-title">
        <header class="chart-card__heading">
          <div>
            <p class="chart-card__eyebrow">Cash flow</p>
            <h2 id="cash-flow-title">Monthly AWS outflow</h2>
          </div>
          <span v-if="cashFlowRows.length" class="chart-card__period">
            {{ cashFlowPeriod }}
          </span>
        </header>

        <div v-if="!costs.available" class="chart-card__empty" role="status">
          <strong>Monthly costs are unavailable.</strong>
          <span>Data will refresh after the next successful update.</span>
        </div>

        <div v-else-if="!cashFlowRows.length" class="chart-card__empty" role="status">
          <strong>No AWS costs reported.</strong>
          <span>Monthly outflow will appear when costs are published.</span>
        </div>

        <figure v-else class="monthly-chart">
          <ol
            class="monthly-chart__plot"
            aria-label="Recent AWS outflow by month"
            :style="{ '--month-count': cashFlowRows.length }"
          >
            <li
              v-for="month in cashFlowRows"
              :key="month.key"
              class="monthly-chart__item"
              :aria-label="`${month.label}: ${formatCompactCashFlow(month.cash_flow_usd)}${month.estimated ? ', month-to-date estimate' : ''}`"
            >
              <span class="monthly-chart__amount" aria-hidden="true">
                {{ formatCompactCashFlow(month.cash_flow_usd) }}
              </span>
              <span class="monthly-chart__track" aria-hidden="true">
                <span
                  class="monthly-chart__bar"
                  :class="{ 'monthly-chart__bar--inflow': month.cash_flow_usd > 0 }"
                  :style="{ height: monthlyBarHeight(month.cash_flow_usd) }"
                />
              </span>
              <span class="monthly-chart__month" aria-hidden="true">
                {{ monthAbbreviation(month.label) }}
              </span>
              <span class="monthly-chart__estimate" aria-hidden="true">
                {{ month.estimated ? "MTD" : "" }}
              </span>
            </li>
          </ol>
          <figcaption class="visually-hidden">
            Negative values are AWS spending. MTD marks the current month-to-date estimate.
          </figcaption>
        </figure>
      </article>

      <article class="chart-card" aria-labelledby="service-cost-title">
        <header class="chart-card__heading">
          <div>
            <p class="chart-card__eyebrow">Cost drivers</p>
            <h2 id="service-cost-title">AWS cost by service</h2>
          </div>
          <span v-if="costs.current_month?.label" class="chart-card__period">
            {{ costs.current_month.label }}{{ costs.current_month.estimated ? " · MTD" : "" }}
          </span>
        </header>

        <div v-if="!costs.available" class="chart-card__empty" role="status">
          <strong>Service costs are unavailable.</strong>
          <span>Data will refresh after the next successful update.</span>
        </div>

        <div v-else-if="!serviceRows.length" class="chart-card__empty" role="status">
          <strong>No service costs reported.</strong>
          <span>The current-month breakdown will appear when available.</span>
        </div>

        <ol v-else class="service-chart" aria-label="Current-month AWS cost by service">
          <li
            v-for="item in serviceRows"
            :key="item.service"
            class="service-chart__item"
          >
            <span class="service-chart__label">{{ item.service }}</span>
            <span
              class="service-chart__amount"
              :class="{ 'service-chart__amount--credit': item.cost_usd < 0 }"
            >
              {{ formatUsd(item.cost_usd) }}
            </span>
            <span class="service-chart__track" aria-hidden="true">
              <span
                class="service-chart__bar"
                :class="{ 'service-chart__bar--credit': item.cost_usd < 0 }"
                :style="{ width: serviceBarWidth(item.cost_usd) }"
              />
            </span>
          </li>
        </ol>
      </article>
    </section>

    <footer class="transparency-sources">
      <div class="transparency-sources__wallet">
        <span>Project wallet</span>
        <code>{{ wallet.address || "Unavailable" }}</code>
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
.chart-card__eyebrow {
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

.finance-charts {
  align-items: stretch;
  display: grid;
  gap: 1rem;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  min-width: 0;
}

.chart-card {
  background: color-mix(in srgb, var(--vp-c-bg-soft) 72%, transparent);
  border: 1px solid var(--vp-c-divider);
  border-radius: 16px;
  display: grid;
  gap: 1.2rem;
  grid-template-rows: auto 1fr;
  margin: 0;
  min-width: 0;
  padding: 1rem;
}

.chart-card__heading {
  align-items: start;
  display: flex;
  gap: 0.75rem;
  justify-content: space-between;
  min-width: 0;
}

.chart-card__heading > div {
  min-width: 0;
}

.chart-card__heading h2 {
  border-top: 0;
  font-size: 1.05rem;
  line-height: 1.25;
  margin: 0.2rem 0 0;
  overflow-wrap: anywhere;
  padding-top: 0;
}

.chart-card__period {
  color: var(--vp-c-text-3);
  flex: 0 0 auto;
  font-size: 0.7rem;
  line-height: 1.35;
  max-width: 7rem;
  text-align: right;
}

.chart-card__empty {
  align-self: stretch;
  background: color-mix(in srgb, var(--vp-c-bg) 60%, transparent);
  border-radius: 10px;
  display: grid;
  gap: 0.2rem;
  padding: 0.9rem;
}

.chart-card__empty span {
  color: var(--vp-c-text-2);
  font-size: 0.82rem;
}

.monthly-chart {
  align-self: stretch;
  display: grid;
  margin: 0;
  min-width: 0;
}

.monthly-chart__plot {
  display: grid;
  gap: clamp(0.2rem, 1.5cqi, 0.45rem);
  grid-template-columns: repeat(var(--month-count), minmax(0, 1fr));
  list-style: none;
  margin: 0;
  min-width: 0;
  padding: 0;
}

.monthly-chart__item {
  display: grid;
  gap: 0.25rem;
  grid-template-rows: auto minmax(7rem, 1fr) auto 0.75rem;
  min-width: 0;
  text-align: center;
}

.monthly-chart__amount {
  font-family: var(--vp-font-family-mono);
  font-size: clamp(0.58rem, 2.2cqi, 0.68rem);
  font-variant-numeric: tabular-nums;
  line-height: 1.2;
  min-width: 0;
  white-space: nowrap;
}

.monthly-chart__track {
  align-items: end;
  background: color-mix(in srgb, var(--vp-c-brand-soft) 38%, transparent);
  border-radius: 6px 6px 3px 3px;
  display: flex;
  height: 100%;
  justify-self: center;
  overflow: hidden;
  width: min(72%, 1.8rem);
}

.monthly-chart__bar {
  background: var(--vp-c-brand-1);
  border-radius: 5px 5px 2px 2px;
  display: block;
  min-height: 1px;
  width: 100%;
}

.monthly-chart__bar--inflow,
.service-chart__bar--credit {
  background: var(--finance-inflow);
}

.monthly-chart__month {
  color: var(--vp-c-text-2);
  font-size: 0.7rem;
  font-weight: 600;
}

.monthly-chart__estimate {
  color: var(--vp-c-brand-1);
  font-size: 0.58rem;
  font-weight: 700;
  letter-spacing: 0.07em;
  line-height: 1;
  text-transform: uppercase;
}

.service-chart {
  align-content: start;
  display: grid;
  gap: 0.8rem;
  list-style: none;
  margin: 0;
  min-width: 0;
  padding: 0;
}

.service-chart__item {
  display: grid;
  gap: 0.35rem 0.65rem;
  grid-template-columns: minmax(0, 1fr) auto;
  min-width: 0;
}

.service-chart__label {
  font-size: 0.78rem;
  line-height: 1.3;
  min-width: 0;
  overflow-wrap: anywhere;
}

.service-chart__amount {
  font-family: var(--vp-font-family-mono);
  font-size: 0.72rem;
  font-variant-numeric: tabular-nums;
  white-space: nowrap;
}

.service-chart__track {
  background: color-mix(in srgb, var(--vp-c-brand-soft) 38%, transparent);
  border-radius: 999px;
  grid-column: 1 / -1;
  height: 0.45rem;
  overflow: hidden;
}

.service-chart__bar {
  background: var(--vp-c-brand-1);
  border-radius: inherit;
  display: block;
  height: 100%;
  min-width: 1px;
}

.service-chart__amount--credit {
  color: var(--finance-inflow);
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
  flex: 1 1 24rem;
  font-size: 0.78rem;
  line-height: 1.55;
  min-width: 0;
  overflow-wrap: anywhere;
  white-space: normal;
  word-break: break-word;
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

@container transparency (max-width: 680px) {
  .finance-charts {
    grid-template-columns: minmax(0, 1fr);
  }
}

@container transparency (max-width: 620px) {
  .finance-summary {
    grid-template-columns: 1fr;
  }

  .finance-summary__item + .finance-summary__item {
    border-left: 0;
    border-top: 1px solid var(--vp-c-divider);
  }

  .transparency-sources__meta {
    align-items: start;
    gap: 0.15rem;
  }
}

@container transparency (max-width: 360px) {
  .chart-card {
    padding: 0.8rem;
  }

  .chart-card__heading {
    gap: 0.45rem;
  }

  .monthly-chart__plot {
    gap: 0.15rem;
  }

  .monthly-chart__amount {
    font-size: 0.54rem;
  }
}
</style>
