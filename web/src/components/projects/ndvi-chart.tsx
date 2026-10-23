"use client";

import { useMemo } from "react";
import {
  CartesianGrid,
  Legend,
  Line,
  LineChart,
  ReferenceLine,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";

import type { NdviPoint } from "@/lib/api/schema";
import { formatNumber, formatPeriod } from "@/lib/format";

interface Row {
  period: string;
  landsat: number | null;
  s2: number | null;
  modis: number | null;
}

/** Une los dos sensores por mes para dibujarlos en el mismo eje. */
function toRows(points: NdviPoint[]): Row[] {
  const byPeriod = new Map<string, Row>();
  for (const point of points) {
    const row = byPeriod.get(point.period) ?? { period: point.period, landsat: null, s2: null, modis: null };
    if (point.sensor === "landsat") row.landsat = point.ndvi_mean;
    if (point.sensor === "s2") row.s2 = point.ndvi_mean;
    if (point.sensor === "modis") row.modis = point.ndvi_mean;
    byPeriod.set(point.period, row);
  }
  return [...byPeriod.values()].sort((a, b) => a.period.localeCompare(b.period));
}

export function NdviChart({ points, t0 }: { points: NdviPoint[]; t0: string | null }) {
  const rows = useMemo(() => toRows(points), [points]);

  // Un tick por cada dos años, en enero.
  const ticks = useMemo(() => {
    const years = new Set<number>();
    return rows
      .filter((row) => {
        const year = Number(row.period.slice(0, 4));
        if (years.has(year) || year % 2 !== 0) return false;
        years.add(year);
        return true;
      })
      .map((row) => row.period);
  }, [rows]);

  // El eje es categórico: T0 se ancla al primer mes disponible desde esa fecha.
  const t0Period = t0 ? rows.find((row) => row.period >= t0)?.period : undefined;

  if (rows.length === 0) {
    return <p className="py-10 text-center text-sm text-muted-foreground">Las series de verdor (NDVI) aún no se han calculado: requieren imágenes Sentinel-2 y Landsat procesadas con Earth Engine.</p>;
  }

  const first = rows[0]!.period;
  const last = rows[rows.length - 1]!.period;

  return (
    <div role="img" aria-label={`Serie mensual de NDVI entre ${formatPeriod(first)} y ${formatPeriod(last)}, por sensor.`}>
      <ResponsiveContainer width="100%" height={280} initialDimension={{ width: 560, height: 280 }}>
        <LineChart data={rows} margin={{ left: 10, right: 10, top: 10 }}>
          <CartesianGrid stroke="var(--border)" vertical={false} />
          <XAxis
            dataKey="period"
            ticks={ticks}
            tickFormatter={(value: string) => value.slice(0, 4)}
            tick={{ fontSize: 11, fill: "var(--muted-foreground)" }}
          />
          <YAxis
            domain={[0.2, 1]}
            tickFormatter={(value: number) => value.toFixed(1)}
            tick={{ fontSize: 11, fill: "var(--muted-foreground)" }}
          />
          <Tooltip
            labelFormatter={(label) => formatPeriod(String(label))}
            formatter={(value) => formatNumber(typeof value === "number" ? value : Number(value))}
            contentStyle={{ borderRadius: 12, border: "1px solid var(--border)" }}
          />
          <Legend wrapperStyle={{ fontSize: 12 }} />
          {t0Period && (
            <ReferenceLine
              x={t0Period}
              stroke="var(--foreground)"
              strokeDasharray="4 4"
              label={{ value: "T0", position: "top", fontSize: 12, fill: "var(--foreground)" }}
            />
          )}
          <Line type="monotone" dataKey="landsat" name="Landsat" stroke="var(--chart-2)" strokeWidth={2.5} dot={false} connectNulls={false} isAnimationActive={false} />
          <Line type="monotone" dataKey="modis" name="MODIS (250 m)" stroke="var(--chart-1)" strokeWidth={2.5} dot={false} connectNulls={false} isAnimationActive={false} />
          <Line type="monotone" dataKey="s2" name="Sentinel-2" stroke="var(--chart-1)" strokeWidth={2.5} dot={false} connectNulls={false} isAnimationActive={false} />
        </LineChart>
      </ResponsiveContainer>
    </div>
  );
}
