<script setup lang="ts">
import { computed } from "vue";
import awsCosts from "../generated/aws-costs.json";

type ServiceCost = {
  service: string;
  cost_usd: number;
  share_of_total_pct: number;
};

type MonthBucket = {
  key: string;
  label: string;
  start: string;
  end_exclusive: string;
  total_usd: number;
  estimated: boolean;
  tax_usd: number;
  by_service: ServiceCost[];
};

type DailyBucket = {
  date: string;
  end_exclusive: string;
  total_usd: number;
  estimated: boolean;
};

type CurrentMonthBucket = MonthBucket & {
  daily: DailyBucket[];
};

type CostPayload = {
  available: boolean;
  generated_at_utc: string;
  metric: string;
  currency: string;
  history_months: number;
  max_stacked_services: number;
  public_data_path: string;
  error?: string;
  history: {
    months: MonthBucket[];
    top_services: Array<{ service: string; cost_usd: number }>;
  };
  previous_month: MonthBucket | null;
  current_month: CurrentMonthBucket;
};

const payload = awsCosts as CostPayload;
const currencyFormatter = new Intl.NumberFormat("en-US", {
  style: "currency",
  currency: payload.currency || "USD",
  maximumFractionDigits: 2,
});
const generatedAtFormatter = new Intl.DateTimeFormat("en-US", {
  dateStyle: "medium",
  timeStyle: "short",
  timeZone: "UTC",
});
const monthAxisFormatter = new Intl.DateTimeFormat("en-US", {
  month: "short",
  timeZone: "UTC",
});
const dayAxisFormatter = new Intl.DateTimeFormat("en-US", {
  day: "numeric",
  timeZone: "UTC",
});

const historyMonths = computed(() => payload.history?.months ?? []);
const currentMonth = computed(() => payload.current_month);
const previousMonth = computed(() => payload.previous_month);
const historicalAverage = computed(() => {
  const fullMonths = historyMonths.value.filter((month) => !month.estimated);
  if (!fullMonths.length) {
    return 0;
  }
  const total = fullMonths.reduce((sum, month) => sum + month.total_usd, 0);
  return total / fullMonths.length;
});

const overviewCards = computed(() => [
  {
    label: currentMonth.value.label || "Current month",
    value: currentMonth.value.total_usd,
    meta: currentMonth.value.estimated ? "Estimated month to date" : "Finalized month",
  },
  {
    label: previousMonth.value?.label || "Previous month",
    value: previousMonth.value?.total_usd ?? 0,
    meta: previousMonth.value ? "Last full month" : "Unavailable",
  },
  {
    label: "Full-month average",
    value: historicalAverage.value,
    meta: "Average of finalized months in history window",
  },
]);

const currentMonthServices = computed(() =>
  (currentMonth.value.by_service ?? []).filter((item) => item.cost_usd > 0)
);
const currentMonthPositiveTotal = computed(() =>
  currentMonthServices.value.reduce((sum, item) => sum + item.cost_usd, 0)
);
const currentMonthAdjustmentTotal = computed(() =>
  (currentMonth.value.by_service ?? [])
    .filter((item) => item.cost_usd < 0)
    .reduce((sum, item) => sum + item.cost_usd, 0)
);

const monthlyChart = computed(() => {
  const buckets = historyMonths.value;
  const max = Math.max(...buckets.map((bucket) => bucket.total_usd), 0);
  return { buckets, max };
});

const currentMonthDailyChart = computed(() => {
  const buckets = currentMonth.value.daily ?? [];
  const max = Math.max(...buckets.map((bucket) => bucket.total_usd), 0);
  return { buckets, max };
});

const historyTopServices = computed(() => {
  const totals = new Map<string, number>();
  for (const month of historyMonths.value) {
    for (const service of month.by_service.filter((item) => item.cost_usd > 0)) {
      totals.set(service.service, (totals.get(service.service) ?? 0) + service.cost_usd);
    }
  }
  return Array.from(totals.entries())
    .sort((a, b) => b[1] - a[1])
    .slice(0, payload.max_stacked_services || 5)
    .map(([service]) => service);
});

const stackedHistory = computed(() =>
  historyMonths.value.map((month) => {
    const positiveServices = month.by_service.filter((item) => item.cost_usd > 0);
    const serviceMap = new Map(positiveServices.map((item) => [item.service, item.cost_usd]));
    const topServices = historyTopServices.value.map((service) => ({
      service,
      cost_usd: serviceMap.get(service) ?? 0,
    }));
    const known = topServices.reduce((sum, item) => sum + item.cost_usd, 0);
    const positiveTotal = positiveServices.reduce((sum, item) => sum + item.cost_usd, 0);
    const other = Math.max(positiveTotal - known, 0);
    const adjustmentTotal = month.by_service
      .filter((item) => item.cost_usd < 0)
      .reduce((sum, item) => sum + item.cost_usd, 0);
    return {
      ...month,
      positive_total_usd: positiveTotal,
      adjustment_total_usd: adjustmentTotal,
      segments: other > 0 ? [...topServices, { service: "Other", cost_usd: other }] : topServices,
    };
  })
);

const serviceLegend = computed(() => {
  const labels = [...historyTopServices.value];
  if (stackedHistory.value.some((month) => month.segments.some((segment) => segment.service === "Other"))) {
    labels.push("Other");
  }
  return labels;
});

const serviceBreakdownMax = computed(() =>
  Math.max(...currentMonthServices.value.map((item) => item.cost_usd), 0)
);
const stackedHistoryMax = computed(() =>
  Math.max(...stackedHistory.value.map((month) => month.positive_total_usd), 0)
);
const hasHistoricalAdjustments = computed(() =>
  stackedHistory.value.some((month) => month.adjustment_total_usd < 0)
);

const monthlyChartWidth = 880;
const monthlyChartHeight = 260;
const chartPadding = { top: 20, right: 20, bottom: 42, left: 20 };
const plotHeight = monthlyChartHeight - chartPadding.top - chartPadding.bottom;
const plotWidth = monthlyChartWidth - chartPadding.left - chartPadding.right;
const dailyChartWidth = 880;
const dailyChartHeight = 220;
const dailyPlotHeight = dailyChartHeight - chartPadding.top - chartPadding.bottom;
const dailyPlotWidth = dailyChartWidth - chartPadding.left - chartPadding.right;

function formatUsd(value: number): string {
  return currencyFormatter.format(value);
}

function formatGeneratedAt(value: string): string {
  if (!value) {
    return "Unavailable";
  }
  return generatedAtFormatter.format(new Date(value));
}

function monthAxisLabel(iso: string): string {
  return monthAxisFormatter.format(new Date(`${iso}T00:00:00Z`));
}

function dayAxisLabel(iso: string): string {
  return dayAxisFormatter.format(new Date(`${iso}T00:00:00Z`));
}

function percent(value: number): string {
  return `${value.toFixed(1)}%`;
}

function percentOfTotal(value: number, total: number): string {
  if (total <= 0) {
    return "0.0%";
  }
  return percent((value / total) * 100);
}

function serviceColor(service: string, indexHint = 0): string {
  if (service === "Other") {
    return "#64748b";
  }
  const palette = [
    "#2563eb",
    "#059669",
    "#dc2626",
    "#d97706",
    "#7c3aed",
    "#0f766e",
    "#db2777",
  ];
  const index = Math.max(indexHint, historyTopServices.value.indexOf(service));
  return palette[(index >= 0 ? index : 0) % palette.length];
}

function monthlyBarX(index: number, count: number): number {
  const step = plotWidth / Math.max(count, 1);
  return chartPadding.left + step * index + step * 0.12;
}

function monthlyBarWidth(count: number): number {
  const step = plotWidth / Math.max(count, 1);
  return Math.max(step * 0.76, 10);
}

function monthlyBarHeight(value: number, max: number): number {
  if (max <= 0) {
    return 0;
  }
  return (value / max) * plotHeight;
}

function dailyBarX(index: number, count: number): number {
  const step = dailyPlotWidth / Math.max(count, 1);
  return chartPadding.left + step * index + step * 0.12;
}

function dailyBarWidth(count: number): number {
  const step = dailyPlotWidth / Math.max(count, 1);
  return Math.max(step * 0.76, 8);
}

function dailyBarHeight(value: number, max: number): number {
  if (max <= 0) {
    return 0;
  }
  return (value / max) * dailyPlotHeight;
}
</script>

<template>
  <div class="cost-page">
    <div class="cost-page__header">
      <div>
        <p class="cost-page__eyebrow">AWS Transparency</p>
        <h2>Live cost snapshot for the benchmark account</h2>
      </div>
      <div class="cost-page__meta">
        <span>Metric: {{ payload.metric }}</span>
        <span>Updated: {{ formatGeneratedAt(payload.generated_at_utc) }}</span>
        <a :href="payload.public_data_path">Raw JSON</a>
      </div>
    </div>

    <div v-if="!payload.available" class="cost-page__empty">
      <h3>Cost data is currently unavailable</h3>
      <p>{{ payload.error || "The build did not generate a cost payload." }}</p>
    </div>

    <template v-else>
      <div class="cost-page__cards">
        <article v-for="card in overviewCards" :key="card.label" class="cost-card">
          <p class="cost-card__label">{{ card.label }}</p>
          <p class="cost-card__value">{{ formatUsd(card.value) }}</p>
          <p class="cost-card__meta">{{ card.meta }}</p>
        </article>
      </div>

      <div class="cost-page__notes">
        <p>Month-to-date buckets are estimated and the current UTC day may still be partial.</p>
        <p>Totals include every service Cost Explorer reports for this account, including tax when present.</p>
        <p v-if="currentMonthAdjustmentTotal < 0">
          Net totals currently include {{ formatUsd(currentMonthAdjustmentTotal) }} in credits, refunds, or other
          downward adjustments.
        </p>
      </div>

      <section class="cost-section">
        <div class="cost-section__heading">
          <div>
            <p class="cost-section__eyebrow">Historical</p>
            <h3>Monthly totals</h3>
          </div>
          <p>{{ historyMonths.length }} monthly buckets, including the current month to date.</p>
        </div>
        <div class="chart-shell">
          <svg
            class="chart-svg"
            :viewBox="`0 0 ${monthlyChartWidth} ${monthlyChartHeight}`"
            role="img"
            aria-label="Monthly AWS costs"
          >
            <line
              :x1="chartPadding.left"
              :x2="monthlyChartWidth - chartPadding.right"
              :y1="monthlyChartHeight - chartPadding.bottom"
              :y2="monthlyChartHeight - chartPadding.bottom"
              class="chart-axis"
            />
            <g v-for="(bucket, index) in monthlyChart.buckets" :key="bucket.key">
              <rect
                :x="monthlyBarX(index, monthlyChart.buckets.length)"
                :y="chartPadding.top + plotHeight - monthlyBarHeight(bucket.total_usd, monthlyChart.max)"
                :width="monthlyBarWidth(monthlyChart.buckets.length)"
                :height="monthlyBarHeight(bucket.total_usd, monthlyChart.max)"
                :class="bucket.estimated ? 'chart-bar chart-bar--estimated' : 'chart-bar'"
              />
              <text
                :x="monthlyBarX(index, monthlyChart.buckets.length) + monthlyBarWidth(monthlyChart.buckets.length) / 2"
                :y="monthlyChartHeight - 16"
                class="chart-label"
                text-anchor="middle"
              >
                {{ monthAxisLabel(bucket.start) }}
              </text>
            </g>
          </svg>
        </div>
      </section>

      <section class="cost-section">
        <div class="cost-section__heading">
          <div>
            <p class="cost-section__eyebrow">Month To Date</p>
            <h3>Daily spend in {{ currentMonth.label }}</h3>
          </div>
          <p>{{ currentMonth.daily.length }} daily buckets so far this month.</p>
        </div>
        <div class="chart-shell">
          <svg
            class="chart-svg"
            :viewBox="`0 0 ${dailyChartWidth} ${dailyChartHeight}`"
            role="img"
            :aria-label="`Daily spend for ${currentMonth.label}`"
          >
            <line
              :x1="chartPadding.left"
              :x2="dailyChartWidth - chartPadding.right"
              :y1="dailyChartHeight - chartPadding.bottom"
              :y2="dailyChartHeight - chartPadding.bottom"
              class="chart-axis"
            />
            <g v-for="(bucket, index) in currentMonthDailyChart.buckets" :key="bucket.date">
              <rect
                :x="dailyBarX(index, currentMonthDailyChart.buckets.length)"
                :y="chartPadding.top + dailyPlotHeight - dailyBarHeight(bucket.total_usd, currentMonthDailyChart.max)"
                :width="dailyBarWidth(currentMonthDailyChart.buckets.length)"
                :height="dailyBarHeight(bucket.total_usd, currentMonthDailyChart.max)"
                :class="bucket.estimated ? 'chart-bar chart-bar--estimated' : 'chart-bar chart-bar--accent'"
              />
              <text
                :x="dailyBarX(index, currentMonthDailyChart.buckets.length) + dailyBarWidth(currentMonthDailyChart.buckets.length) / 2"
                :y="dailyChartHeight - 14"
                class="chart-label"
                text-anchor="middle"
              >
                {{ dayAxisLabel(bucket.date) }}
              </text>
            </g>
          </svg>
        </div>
      </section>

      <section class="cost-section cost-section--split">
        <div>
          <div class="cost-section__heading">
            <div>
              <p class="cost-section__eyebrow">Current Mix</p>
              <h3>Positive service breakdown for {{ currentMonth.label }}</h3>
            </div>
            <p>{{ currentMonthServices.length }} services with positive net cost.</p>
          </div>
          <div class="service-bars">
            <div v-for="(service, index) in currentMonthServices" :key="service.service" class="service-row">
              <div class="service-row__label">
                <span class="service-row__swatch" :style="{ backgroundColor: serviceColor(service.service, index) }" />
                <span>{{ service.service }}</span>
              </div>
              <div class="service-row__bar-track">
                <div
                  class="service-row__bar"
                  :style="{
                    width: `${serviceBreakdownMax > 0 ? (service.cost_usd / serviceBreakdownMax) * 100 : 0}%`,
                    backgroundColor: serviceColor(service.service, index),
                  }"
                />
              </div>
              <div class="service-row__value">
                <strong>{{ formatUsd(service.cost_usd) }}</strong>
                <span>{{ percentOfTotal(service.cost_usd, currentMonthPositiveTotal) }}</span>
              </div>
            </div>
          </div>
        </div>

        <div>
          <div class="cost-section__heading">
            <div>
              <p class="cost-section__eyebrow">Historical Mix</p>
              <h3>Positive service composition over time</h3>
            </div>
            <p>Top positive-cost services across the full history window; smaller categories collapse into Other.</p>
          </div>
          <p v-if="hasHistoricalAdjustments" class="cost-section__note">
            Credits, refunds, and other downward adjustments remain in the net totals and raw JSON, but are excluded
            from this positive-charge mix chart.
          </p>
          <div class="chart-shell">
            <svg
              class="chart-svg"
              :viewBox="`0 0 ${monthlyChartWidth} ${monthlyChartHeight}`"
              role="img"
              aria-label="Historical AWS costs by service"
            >
              <line
                :x1="chartPadding.left"
                :x2="monthlyChartWidth - chartPadding.right"
                :y1="monthlyChartHeight - chartPadding.bottom"
                :y2="monthlyChartHeight - chartPadding.bottom"
                class="chart-axis"
              />
              <g v-for="(bucket, index) in stackedHistory" :key="bucket.key">
                <template v-for="(segment, segmentIndex) in bucket.segments" :key="`${bucket.key}-${segment.service}`">
                  <rect
                    :x="monthlyBarX(index, stackedHistory.length)"
                    :width="monthlyBarWidth(stackedHistory.length)"
                    :y="
                      chartPadding.top +
                      plotHeight -
                      monthlyBarHeight(
                        bucket.segments
                          .slice(0, segmentIndex + 1)
                          .reduce((sum, item) => sum + item.cost_usd, 0),
                        stackedHistoryMax
                      )
                    "
                    :height="monthlyBarHeight(segment.cost_usd, stackedHistoryMax)"
                    :fill="serviceColor(segment.service, segmentIndex)"
                  />
                </template>
                <text
                  :x="monthlyBarX(index, stackedHistory.length) + monthlyBarWidth(stackedHistory.length) / 2"
                  :y="monthlyChartHeight - 16"
                  class="chart-label"
                  text-anchor="middle"
                >
                  {{ monthAxisLabel(bucket.start) }}
                </text>
              </g>
            </svg>
          </div>
          <div class="service-legend">
            <div v-for="(service, index) in serviceLegend" :key="service" class="service-legend__item">
              <span class="service-row__swatch" :style="{ backgroundColor: serviceColor(service, index) }" />
              <span>{{ service }}</span>
            </div>
          </div>
        </div>
      </section>
    </template>
  </div>
</template>

<style scoped>
.cost-page {
  display: grid;
  gap: 1.5rem;
}

.cost-page__header,
.cost-section__heading,
.cost-page__meta,
.cost-page__notes,
.service-row,
.service-row__label,
.service-row__value,
.service-legend,
.service-legend__item {
  display: flex;
}

.cost-page__header,
.cost-section__heading {
  align-items: end;
  justify-content: space-between;
  gap: 1rem;
}

.cost-page__eyebrow,
.cost-section__eyebrow,
.cost-card__label {
  color: var(--vp-c-text-2);
  font-size: 0.82rem;
  letter-spacing: 0.08em;
  margin: 0 0 0.3rem;
  text-transform: uppercase;
}

.cost-page__header h2,
.cost-section__heading h3,
.cost-page__empty h3 {
  margin: 0;
}

.cost-page__meta {
  align-items: center;
  color: var(--vp-c-text-2);
  flex-wrap: wrap;
  gap: 0.9rem;
  justify-content: flex-end;
  text-align: right;
}

.cost-page__cards {
  display: grid;
  gap: 1rem;
  grid-template-columns: repeat(3, minmax(0, 1fr));
}

.cost-card,
.cost-page__empty,
.chart-shell,
.service-bars {
  background: linear-gradient(180deg, rgba(37, 99, 235, 0.08), rgba(15, 23, 42, 0.02));
  border: 1px solid var(--vp-c-divider);
  border-radius: 18px;
}

.cost-card {
  padding: 1rem 1.1rem;
}

.cost-card__value {
  font-size: 1.85rem;
  font-weight: 700;
  margin: 0;
}

.cost-card__meta,
.cost-page__notes,
.cost-section__heading p,
.cost-section__note,
.service-row__value span {
  color: var(--vp-c-text-2);
}

.cost-card__meta,
.cost-page__notes {
  margin: 0.35rem 0 0;
}

.cost-page__notes {
  flex-direction: column;
  gap: 0.25rem;
}

.cost-page__notes p,
.cost-section__heading p,
.cost-section__note {
  margin: 0;
}

.cost-section {
  display: grid;
  gap: 1rem;
}

.cost-section--split {
  grid-template-columns: minmax(0, 0.95fr) minmax(0, 1.05fr);
}

.chart-shell {
  overflow: hidden;
  padding: 0.75rem 0.8rem 0.2rem;
}

.chart-svg {
  display: block;
  height: auto;
  width: 100%;
}

.chart-axis {
  stroke: color-mix(in srgb, var(--vp-c-text-2) 32%, transparent);
  stroke-width: 1;
}

.chart-bar {
  fill: #2563eb;
  opacity: 0.88;
}

.chart-bar--accent {
  fill: #059669;
}

.chart-bar--estimated {
  fill: #0f172a;
  opacity: 0.45;
}

.chart-label {
  fill: var(--vp-c-text-2);
  font-size: 11px;
}

.service-bars {
  display: grid;
  gap: 0.85rem;
  padding: 1rem;
}

.service-row {
  align-items: center;
  gap: 0.85rem;
}

.service-row__label {
  align-items: center;
  flex: 0 0 34%;
  gap: 0.65rem;
  min-width: 0;
}

.service-row__label span:last-child {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.service-row__bar-track {
  background: color-mix(in srgb, var(--vp-c-text-3) 14%, transparent);
  border-radius: 999px;
  flex: 1 1 auto;
  height: 0.75rem;
  overflow: hidden;
}

.service-row__bar {
  border-radius: 999px;
  height: 100%;
  min-width: 0.45rem;
}

.service-row__value {
  align-items: end;
  flex: 0 0 18%;
  flex-direction: column;
}

.service-row__swatch {
  border-radius: 999px;
  display: inline-block;
  flex: 0 0 auto;
  height: 0.72rem;
  width: 0.72rem;
}

.service-legend {
  flex-wrap: wrap;
  gap: 0.75rem 1rem;
  margin-top: 0.75rem;
}

.service-legend__item {
  align-items: center;
  gap: 0.45rem;
}

.cost-page__empty {
  padding: 1rem 1.1rem;
}

@media (max-width: 960px) {
  .cost-page__cards,
  .cost-section--split {
    grid-template-columns: 1fr;
  }

  .cost-page__header,
  .cost-section__heading {
    align-items: start;
    flex-direction: column;
  }

  .cost-page__meta {
    justify-content: flex-start;
    text-align: left;
  }

  .service-row {
    align-items: start;
    flex-direction: column;
  }

  .service-row__label,
  .service-row__value {
    flex: none;
    width: 100%;
  }

  .service-row__value {
    align-items: start;
  }
}

@media (max-width: 640px) {
  .cost-page__cards {
    grid-template-columns: 1fr;
  }

  .chart-shell {
    margin-inline: -0.2rem;
    padding-inline: 0.4rem;
  }
}
</style>
