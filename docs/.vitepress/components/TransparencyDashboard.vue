<script setup lang="ts">
import { computed, ref } from "vue";
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

type CashFlowMode = "monthly" | "cumulative";

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
const cashFlowMode = ref<CashFlowMode>("monthly");

const currencyFormatter = new Intl.NumberFormat("en-US", {
  style: "currency",
  currency: wallet.currency || costs.currency || "USD",
  maximumFractionDigits: 2,
});
const compactCurrencyFormatter = new Intl.NumberFormat("en-US", {
  style: "currency",
  currency: wallet.currency || costs.currency || "USD",
  notation: "compact",
  maximumFractionDigits: 1,
});
const roundedCurrencyFormatter = new Intl.NumberFormat("en-US", {
  style: "currency",
  currency: wallet.currency || costs.currency || "USD",
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
const cashFlowRows = computed(() => {
  let cumulativeCashFlow = 0;
  return historyMonths.value.map((month) => {
    const monthlyCashFlow = -month.total_usd;
    cumulativeCashFlow += monthlyCashFlow;
    return {
      ...month,
      cash_flow_usd:
        cashFlowMode.value === "cumulative" ? cumulativeCashFlow : monthlyCashFlow,
    };
  });
});
const cashFlowScale = computed(() => {
  const values = cashFlowRows.value.map((month) => month.cash_flow_usd);
  const minimum = Math.min(0, ...values);
  const maximum = Math.max(0, ...values);
  const span = maximum - minimum || 1;
  return {
    span,
    verticalZero: (maximum / span) * 100,
    inlineZero: (-minimum / span) * 100,
  };
});
const cashFlowScaleStyle = computed(() => ({
  "--zero-position": `${cashFlowScale.value.verticalZero}%`,
  "--zero-inline-position": `${cashFlowScale.value.inlineZero}%`,
}));
const cashFlowViewLabel = computed(() =>
  cashFlowMode.value === "cumulative" ? "Cumulative AWS outflow" : "Monthly AWS outflow"
);
const cashFlowPeriod = computed(() =>
  cashFlowRows.value.length === 1 ? "1 month" : `All ${cashFlowRows.value.length} months`
);
const cashFlowStatus = computed(
  () =>
    `Showing ${cashFlowMode.value} AWS outflow for ${
      cashFlowRows.value.length === 1 ? "1 month" : `all ${cashFlowRows.value.length} months`
    }.`
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
    return roundedCurrencyFormatter.format(0);
  }
  const absoluteValue = Math.abs(value);
  let formatted: string;
  if (absoluteValue < 10) {
    formatted = currencyFormatter.format(absoluteValue);
  } else if (absoluteValue < 1000) {
    formatted = roundedCurrencyFormatter.format(absoluteValue);
  } else {
    formatted = compactCurrencyFormatter.format(absoluteValue);
  }
  return value > 0 ? `+${formatted}` : `−${formatted}`;
}

function monthAbbreviation(label: string): string {
  return label.split(/\s+/)[0] || label;
}

function abbreviatedYear(label: string): string {
  const year = label.match(/\b(\d{4})\b/)?.[1];
  return year ? `’${year.slice(-2)}` : "";
}

function cashFlowBarSize(value: number): string {
  if (Math.abs(value) < 0.005) {
    return "0%";
  }
  return `${(Math.abs(value) / cashFlowScale.value.span) * 100}%`;
}

function cashFlowAriaLabel(month: MonthBucket & { cash_flow_usd: number }): string {
  const view = cashFlowMode.value === "cumulative" ? "cumulative AWS cash flow" : "AWS cash flow";
  const estimate = month.estimated ? ", month-to-date estimate" : "";
  return `${month.label}: ${formatCompactCashFlow(month.cash_flow_usd)} ${view}${estimate}`;
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
        <header class="chart-card__heading chart-card__heading--cash-flow">
          <div>
            <p class="chart-card__eyebrow">Cash flow</p>
            <h2 id="cash-flow-title">AWS outflow</h2>
          </div>
          <div v-if="cashFlowRows.length" class="chart-card__controls">
            <span class="chart-card__period">{{ cashFlowPeriod }}</span>
            <div class="cash-flow-toggle" role="group" aria-label="Cash flow view">
              <button
                type="button"
                :aria-pressed="cashFlowMode === 'monthly'"
                aria-controls="aws-cash-flow-chart"
                @click="cashFlowMode = 'monthly'"
              >
                Monthly
              </button>
              <button
                type="button"
                :aria-pressed="cashFlowMode === 'cumulative'"
                aria-controls="aws-cash-flow-chart"
                @click="cashFlowMode = 'cumulative'"
              >
                Cumulative
              </button>
            </div>
          </div>
          <p v-if="cashFlowRows.length" class="visually-hidden" aria-live="polite">
            {{ cashFlowStatus }}
          </p>
        </header>

        <div v-if="!costs.available" class="chart-card__empty" role="status">
          <strong>Monthly costs are unavailable.</strong>
          <span>Data will refresh after the next successful update.</span>
        </div>

        <div v-else-if="!cashFlowRows.length" class="chart-card__empty" role="status">
          <strong>No AWS costs reported.</strong>
          <span>Monthly outflow will appear when costs are published.</span>
        </div>

        <figure v-else id="aws-cash-flow-chart" class="monthly-chart">
          <ol
            class="monthly-chart__plot"
            :aria-label="cashFlowViewLabel"
            :style="{ '--month-count': cashFlowRows.length }"
          >
            <li
              v-for="month in cashFlowRows"
              :key="month.key"
              class="monthly-chart__item"
              :aria-label="cashFlowAriaLabel(month)"
            >
              <span class="monthly-chart__amount" aria-hidden="true">
                {{ formatCompactCashFlow(month.cash_flow_usd) }}
              </span>
              <span
                class="monthly-chart__track"
                :style="cashFlowScaleStyle"
                aria-hidden="true"
              >
                <span
                  v-if="Math.abs(month.cash_flow_usd) >= 0.005"
                  class="monthly-chart__bar"
                  :class="{ 'monthly-chart__bar--inflow': month.cash_flow_usd > 0 }"
                  :style="{ '--bar-size': cashFlowBarSize(month.cash_flow_usd) }"
                />
              </span>
              <span class="monthly-chart__month" aria-hidden="true">
                <span>{{ monthAbbreviation(month.label) }}</span>
                <span class="monthly-chart__year">{{ abbreviatedYear(month.label) }}</span>
              </span>
              <span v-if="month.estimated" class="monthly-chart__estimate" aria-hidden="true">
                MTD
              </span>
            </li>
          </ol>
          <figcaption class="monthly-chart__caption">
            <span>Bars start at zero. Outflows are negative; credits appear positive.</span>
            <span v-if="cashFlowRows.some((month) => month.estimated)">MTD is estimated.</span>
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
  grid-template-columns: minmax(0, 1fr);
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

.chart-card__controls {
  align-items: end;
  display: grid;
  gap: 0.5rem;
  justify-items: end;
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

.cash-flow-toggle {
  background: color-mix(in srgb, var(--vp-c-bg) 64%, transparent);
  border: 1px solid var(--vp-c-divider);
  border-radius: 9px;
  display: inline-grid;
  gap: 0.15rem;
  grid-auto-flow: column;
  padding: 0.18rem;
}

.cash-flow-toggle button {
  appearance: none;
  background: transparent;
  border: 0;
  border-radius: 6px;
  color: var(--vp-c-text-2);
  cursor: pointer;
  font-family: inherit;
  font-size: 0.72rem;
  font-weight: 600;
  line-height: 1.2;
  padding: 0.38rem 0.58rem;
}

.cash-flow-toggle button:hover {
  color: var(--vp-c-text-1);
}

.cash-flow-toggle button[aria-pressed="true"] {
  background: var(--vp-c-bg);
  box-shadow: inset 0 0 0 1px var(--vp-c-divider);
  color: var(--vp-c-brand-1);
}

.cash-flow-toggle button:focus-visible {
  outline: 2px solid var(--vp-c-brand-1);
  outline-offset: 2px;
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
  gap: 0.7rem;
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
  grid-template-rows: 1rem minmax(8rem, 1fr) auto 0.75rem;
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
  background: transparent;
  height: 100%;
  justify-self: center;
  position: relative;
  width: min(68%, 2rem);
}

.monthly-chart__track::before {
  border-top: 1px solid var(--vp-c-divider);
  content: "";
  left: 0;
  position: absolute;
  right: 0;
  top: var(--zero-position);
}

.monthly-chart__bar {
  background: var(--vp-c-brand-1);
  border-radius: 0 0 5px 5px;
  height: var(--bar-size);
  left: 0;
  min-height: 1px;
  position: absolute;
  top: var(--zero-position);
  width: 100%;
}

.monthly-chart__bar--inflow {
  border-radius: 5px 5px 0 0;
  bottom: calc(100% - var(--zero-position));
  top: auto;
}

.monthly-chart__bar--inflow,
.service-chart__bar--credit {
  background: var(--finance-inflow);
}

.monthly-chart__month {
  align-items: baseline;
  color: var(--vp-c-text-2);
  display: flex;
  font-size: 0.7rem;
  font-weight: 600;
  gap: 0.18rem;
  justify-content: center;
  white-space: nowrap;
}

.monthly-chart__year {
  color: var(--vp-c-text-3);
  font-size: 0.62rem;
  font-weight: 500;
}

.monthly-chart__estimate {
  color: var(--vp-c-brand-1);
  font-size: 0.58rem;
  font-weight: 700;
  letter-spacing: 0.07em;
  line-height: 1;
  text-transform: uppercase;
}

.monthly-chart__caption {
  color: var(--vp-c-text-3);
  display: flex;
  flex-wrap: wrap;
  font-size: 0.72rem;
  gap: 0.2rem 0.8rem;
  line-height: 1.45;
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

@container transparency (max-width: 620px) {
  .finance-summary {
    grid-template-columns: 1fr;
  }

  .finance-summary__item + .finance-summary__item {
    border-left: 0;
    border-top: 1px solid var(--vp-c-divider);
  }

  .chart-card__heading--cash-flow {
    flex-direction: column;
  }

  .chart-card__controls {
    align-items: center;
    display: flex;
    justify-content: space-between;
    width: 100%;
  }

  .monthly-chart__plot {
    gap: 0;
    grid-template-columns: minmax(0, 1fr);
  }

  .monthly-chart__item {
    align-items: center;
    gap: 0.35rem 0.65rem;
    grid-template-columns: minmax(0, 1fr) auto;
    grid-template-rows: auto 0.5rem auto;
    padding: 0.5rem 0;
    text-align: left;
  }

  .monthly-chart__item + .monthly-chart__item {
    border-top: 1px solid var(--vp-c-divider);
  }

  .monthly-chart__amount {
    grid-column: 2;
    grid-row: 1;
    text-align: right;
  }

  .monthly-chart__track {
    grid-column: 1 / -1;
    grid-row: 2;
    height: 100%;
    justify-self: stretch;
    width: 100%;
  }

  .monthly-chart__track::before {
    border-left: 1px solid var(--vp-c-divider);
    border-top: 0;
    bottom: 0;
    left: var(--zero-inline-position);
    right: auto;
    top: 0;
  }

  .monthly-chart__bar {
    border-radius: 999px 0 0 999px;
    bottom: 0;
    height: 100%;
    left: auto;
    right: calc(100% - var(--zero-inline-position));
    top: 0;
    width: var(--bar-size);
  }

  .monthly-chart__bar--inflow {
    border-radius: 0 999px 999px 0;
    left: var(--zero-inline-position);
    right: auto;
  }

  .monthly-chart__month {
    grid-column: 1;
    grid-row: 1;
    justify-content: start;
  }

  .monthly-chart__estimate {
    grid-column: 1;
    grid-row: 3;
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

  .chart-card__controls {
    align-items: flex-start;
    flex-direction: column;
  }

  .cash-flow-toggle button {
    padding-left: 0.45rem;
    padding-right: 0.45rem;
  }
}
</style>
