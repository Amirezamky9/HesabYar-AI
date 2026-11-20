/**
 * Tiny charts for the cheque reports (US-535, §7.5).
 *
 * No chart library: horizontal bars, a conic-gradient donut and a
 * two-series SVG area chart, all from the same data the tables
 * show. Every value is also in the table next to the chart, so the
 * chart is never the only source of a figure. All labels are
 * Persian; values render with Persian digits.
 */

import { faNumber } from "@/lib/utils";

export interface ChartItem {
  label: string;
  value: number;
  /** Optional display value (e.g. money) — defaults to faNumber(value). */
  display?: string;
}

const DONUT_COLORS = [
  "var(--primary)",
  "var(--success)",
  "var(--warning)",
  "var(--destructive)",
  "var(--muted-foreground)",
  "var(--accent)",
];

/** Horizontal bars, scaled to the largest value. */
export function BarChart({ items, unit }: { items: ChartItem[]; unit?: string }) {
  const max = Math.max(1, ...items.map((i) => i.value));
  return (
    <div className="space-y-3" role="img" aria-label={`نمودار میله‌ای: ${items.map((i) => `${i.label} ${i.display ?? faNumber(i.value)}`).join("، ")}`}>
      {items.map((item) => (
        <div key={item.label} className="space-y-1">
          <div className="flex items-center justify-between gap-4 text-sm">
            <span>{item.label}</span>
            <span className="font-medium tabular-nums">
              {item.display ?? faNumber(item.value)}
              {unit ? ` ${unit}` : ""}
            </span>
          </div>
          <div className="h-3 w-full overflow-hidden rounded-full bg-muted">
            <div className="h-full rounded-full bg-primary" style={{ width: `${(item.value / max) * 100}%` }} />
          </div>
        </div>
      ))}
    </div>
  );
}

/** Donut with a legend; slices in fixed token colours. */
export function DonutChart({ items }: { items: ChartItem[] }) {
  const total = items.reduce((sum, item) => sum + item.value, 0);
  if (total === 0) {
    return <p className="text-sm text-muted-foreground">داده‌ای برای نمودار نیست.</p>;
  }
  // Cumulative stops, computed in render order (no mutation inside a callback).
  const stops: string[] = [];
  let acc = 0;
  for (const [i, item] of items.entries()) {
    const from = (acc / total) * 100;
    acc += item.value;
    const to = (acc / total) * 100;
    stops.push(`${DONUT_COLORS[i % DONUT_COLORS.length]} ${from}% ${to}%`);
  }
  const stopsCss = stops.join(", ");
  return (
    <div className="flex flex-wrap items-center gap-6">
      <div
        className="size-36 shrink-0 rounded-full"
        style={{ background: `conic-gradient(${stopsCss})` }}
        role="img"
        aria-label={`نمودار دونات: ${items.map((i) => `${i.label} ${i.display ?? faNumber(i.value)}`).join("، ")}`}
      />
      <ul className="space-y-2 text-sm">
        {items.map((item, i) => (
          <li key={item.label} className="flex items-center gap-2">
            <span
              className="size-3 shrink-0 rounded-sm"
              style={{ background: DONUT_COLORS[i % DONUT_COLORS.length] }}
              aria-hidden
            />
            <span>{item.label}</span>
            <span className="font-medium tabular-nums">{item.display ?? faNumber(item.value)}</span>
            <span className="text-muted-foreground">({faNumber(Math.round((item.value / total) * 100))}٪)</span>
          </li>
        ))}
      </ul>
    </div>
  );
}

/**
 * Two-series area chart (report 14: expected inflow vs outflow over
 * 12 weeks). Pure SVG: the area under each polyline is filled at low
 * opacity; the table below the chart carries the same numbers.
 */
export function AreaChart({
  points,
  inflowLabel,
  outflowLabel,
}: {
  points: { label: string; inflow: number; outflow: number }[];
  inflowLabel: string;
  outflowLabel: string;
}) {
  const width = 640;
  const height = 220;
  const pad = 8;
  const max = Math.max(1, ...points.flatMap((p) => [p.inflow, p.outflow]));
  const x = (i: number) => pad + (i / Math.max(1, points.length - 1)) * (width - pad * 2);
  const y = (v: number) => height - pad - (v / max) * (height - pad * 2);
  const line = (key: "inflow" | "outflow") =>
    points.map((p, i) => `${i === 0 ? "M" : "L"}${x(i)},${y(p[key])}`).join(" ");
  const area = (key: "inflow" | "outflow") =>
    `${line(key)} L${x(points.length - 1)},${height - pad} L${x(0)},${height - pad} Z`;
  return (
    <div className="space-y-2">
      <svg
        viewBox={`0 0 ${width} ${height}`}
        className="w-full"
        role="img"
        aria-label={`نمودار ناحیه‌ای: ${inflowLabel} در برابر ${outflowLabel} در ۱۲ هفتهٔ آینده`}
      >
        <path d={area("inflow")} fill="var(--success)" opacity={0.25} />
        <path d={area("outflow")} fill="var(--destructive)" opacity={0.2} />
        <path d={line("inflow")} fill="none" stroke="var(--success)" strokeWidth={2} />
        <path d={line("outflow")} fill="none" stroke="var(--destructive)" strokeWidth={2} />
        {points.map((p, i) => (
          <text key={p.label} x={x(i)} y={height - 1} fontSize={9} textAnchor="middle" fill="var(--muted-foreground)">
            {p.label}
          </text>
        ))}
      </svg>
      <p className="flex flex-wrap gap-4 text-sm">
        <span className="flex items-center gap-2">
          <span className="size-3 rounded-sm bg-success" aria-hidden />
          {inflowLabel}
        </span>
        <span className="flex items-center gap-2">
          <span className="size-3 rounded-sm bg-destructive" aria-hidden />
          {outflowLabel}
        </span>
      </p>
    </div>
  );
}
