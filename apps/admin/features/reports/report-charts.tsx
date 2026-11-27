/**
 * Charts for the US-550 financial reports (§7.5).
 *
 * No chart library: a grouped vertical bar chart (monthly
 * revenue/expense) and a waterfall chart (cash flow), both pure SVG
 * like the cheque AreaChart. Every value is also in the table next
 * to the chart, so the chart is never the only source of a figure.
 * All labels are Persian; values render with Persian digits.
 * Bar labels show تومان (IRR ÷ 10, rounded); the adjacent tables
 * carry the exact figures with the unit.
 */

import { faNumber } from "@/lib/utils";
import { formatMoney } from "@/lib/format-money";
import type { WaterfallStep } from "./financial-report-data";

/** IRR → rounded تومان for compact chart labels. */
function toToman(amountIrr: number): string {
  return faNumber(Math.round(amountIrr / 10));
}

/** Grouped vertical bars: revenue (green) vs expense (red) per month. */
export function GroupedBarChart({
  items,
  revenueLabel,
  expenseLabel,
}: {
  items: { label: string; revenue: number; expense: number }[];
  revenueLabel: string;
  expenseLabel: string;
}) {
  const width = 640;
  const height = 220;
  const pad = 8;
  const labelHeight = 20;
  if (items.length === 0) {
    return <p className="text-sm text-muted-foreground">داده‌ای برای نمودار نیست.</p>;
  }
  const min = Math.min(0, ...items.flatMap((i) => [i.revenue, i.expense]));
  const max = Math.max(1, ...items.flatMap((i) => [i.revenue, i.expense]));
  const plotHeight = height - pad * 2 - labelHeight;
  const y = (v: number) => pad + ((max - v) / (max - min)) * plotHeight;
  const zeroY = y(0);
  const groupWidth = (width - pad * 2) / items.length;
  const barWidth = Math.min(24, groupWidth / 3);
  const aria = items
    .map((i) => `${i.label}: ${revenueLabel} ${formatMoney(i.revenue)}، ${expenseLabel} ${formatMoney(i.expense)}`)
    .join("؛ ");
  return (
    <div className="space-y-2">
      <svg viewBox={`0 0 ${width} ${height}`} className="w-full" role="img" aria-label={`نمودار میله‌ای ماهانه: ${aria}`}>
        <line x1={pad} x2={width - pad} y1={zeroY} y2={zeroY} stroke="var(--border)" strokeWidth={1} />
        {items.map((item, i) => {
          const cx = pad + groupWidth * i + groupWidth / 2;
          const revenueTop = Math.min(y(item.revenue), zeroY);
          const revenueHeight = Math.abs(zeroY - y(item.revenue));
          const expenseTop = Math.min(y(item.expense), zeroY);
          const expenseHeight = Math.abs(zeroY - y(item.expense));
          return (
            <g key={item.label}>
              <rect
                x={cx - barWidth - 2}
                y={revenueTop}
                width={barWidth}
                height={Math.max(revenueHeight, item.revenue === 0 ? 0 : 1)}
                fill="var(--success)"
                rx={2}
              />
              <rect
                x={cx + 2}
                y={expenseTop}
                width={barWidth}
                height={Math.max(expenseHeight, item.expense === 0 ? 0 : 1)}
                fill="var(--destructive)"
                rx={2}
              />
              <text x={cx} y={height - 1} fontSize={9} textAnchor="middle" fill="var(--muted-foreground)">
                {item.label}
              </text>
            </g>
          );
        })}
      </svg>
      <p className="flex flex-wrap gap-4 text-sm">
        <span className="flex items-center gap-2">
          <span className="size-3 rounded-sm bg-success" aria-hidden />
          {revenueLabel}
        </span>
        <span className="flex items-center gap-2">
          <span className="size-3 rounded-sm bg-destructive" aria-hidden />
          {expenseLabel}
        </span>
        <span className="text-muted-foreground">مبالغ به تومان</span>
      </p>
    </div>
  );
}

/**
 * Waterfall chart: opening cash → one floating bar per section →
 * closing cash. Up-steps are green, down-steps red, the opening and
 * closing totals use the primary colour. Dashed connectors join the
 * steps at their running totals.
 */
export function WaterfallChart({ steps }: { steps: WaterfallStep[] }) {
  const width = 640;
  const height = 240;
  const pad = 8;
  const labelHeight = 20;
  const valueHeight = 16;
  if (steps.length === 0) {
    return <p className="text-sm text-muted-foreground">داده‌ای برای نمودار نیست.</p>;
  }
  const extremes = steps.flatMap((s) => [0, s.cumulative, s.kind === "section" ? s.cumulative - s.value : s.value]);
  const min = Math.min(...extremes);
  const max = Math.max(1, ...extremes);
  const plotTop = pad + valueHeight;
  const plotHeight = height - plotTop - pad - labelHeight;
  const y = (v: number) => plotTop + ((max - v) / (max - min)) * plotHeight;
  const zeroY = y(0);
  const slotWidth = (width - pad * 2) / steps.length;
  const barWidth = Math.min(72, slotWidth * 0.6);
  const aria = steps.map((s) => `${s.label}: ${formatMoney(s.value)}`).join("؛ ");
  return (
    <div className="space-y-2">
      <svg viewBox={`0 0 ${width} ${height}`} className="w-full" role="img" aria-label={`نمودار آبشاری جریان نقد: ${aria}`}>
        <line x1={pad} x2={width - pad} y1={zeroY} y2={zeroY} stroke="var(--border)" strokeWidth={1} />
        {steps.map((step, i) => {
          const cx = pad + slotWidth * i + slotWidth / 2;
          const from = step.kind === "section" ? step.cumulative - step.value : 0;
          const to = step.cumulative;
          const top = Math.min(y(from), y(to));
          const barHeight = Math.max(Math.abs(y(to) - y(from)), from === to ? 0 : 1);
          const fill =
            step.kind === "section" ? (step.value >= 0 ? "var(--success)" : "var(--destructive)") : "var(--primary)";
          const prevCumulative = i > 0 ? steps[i - 1].cumulative : null;
          return (
            <g key={step.label}>
              {prevCumulative !== null && i > 0 ? (
                <line
                  x1={pad + slotWidth * (i - 1) + slotWidth / 2 + barWidth / 2}
                  x2={cx - barWidth / 2}
                  y1={y(prevCumulative)}
                  y2={y(prevCumulative)}
                  stroke="var(--muted-foreground)"
                  strokeWidth={1}
                  strokeDasharray="4 3"
                />
              ) : null}
              <rect x={cx - barWidth / 2} y={top} width={barWidth} height={barHeight} fill={fill} rx={2} />
              <text
                x={cx}
                y={top - 4}
                fontSize={9}
                textAnchor="middle"
                fill="var(--foreground)"
                className="tabular-nums"
              >
                {toToman(step.value)}
              </text>
              <text x={cx} y={height - 1} fontSize={9} textAnchor="middle" fill="var(--muted-foreground)">
                {step.label}
              </text>
            </g>
          );
        })}
      </svg>
      <p className="text-sm text-muted-foreground">مبالغ به تومان؛ سبز افزایش نقد، قرمز کاهش نقد است.</p>
    </div>
  );
}
