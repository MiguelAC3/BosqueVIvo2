"use client";

import "maplibre-gl/dist/maplibre-gl.css";

import type { Feature } from "geojson";
import type { Map as MapLibreMap } from "maplibre-gl";
import { useEffect, useRef, useState } from "react";

import type { Category, ProjectGeometry } from "@/lib/api/schema";
import { categoryHex } from "@/lib/domain";

import { MAP_STYLE, WORKER_URL, boundsOf } from "./map-style";

type ImageCoordinates = [[number, number], [number, number], [number, number], [number, number]];
type ImageKind = "ndvi" | "loss";

interface MonthlyImage {
  period: string;
  kind: ImageKind;
  url: string;
  coordinates: ImageCoordinates;
}

function imageCoordinates(header: string | null, fallback: ReturnType<typeof boundsOf>): ImageCoordinates | null {
  const values = header?.split(",").map(Number);
  const bounds =
    values?.length === 4 &&
    values.every(Number.isFinite) &&
    values[0]! < values[2]! &&
    values[1]! < values[3]!
      ? (values as [number, number, number, number])
      : fallback
        ? [fallback[0][0], fallback[0][1], fallback[1][0], fallback[1][1]]
        : null;
  if (!bounds) return null;
  const [minX, minY, maxX, maxY] = bounds;
  return [
    [minX, maxY],
    [maxX, maxY],
    [maxX, minY],
    [minX, minY],
  ];
}

/** Mapa satelital con el polígono de un proyecto, encuadrado sobre él. */
export function LocationMap({
  geometry,
  category,
  name,
  projectId,
  months,
  lossYears,
  monthlyCompositeEnabled,
}: {
  geometry: ProjectGeometry;
  category: Category | null;
  name: string;
  projectId: number;
  months: string[];
  lossYears: { year: number; ha_lost: number }[];
  monthlyCompositeEnabled: boolean;
}) {
  const availableLossYears = lossYears.map(({ year }) => String(year));
  const containerRef = useRef<HTMLDivElement>(null);
  const mapRef = useRef<MapLibreMap | null>(null);
  const [mapReady, setMapReady] = useState(false);
  const [selectedMonth, setSelectedMonth] = useState(months.at(-1) ?? "");
  const [selectedYear, setSelectedYear] = useState(availableLossYears.at(-1) ?? "");
  const [selectedKind, setSelectedKind] = useState<ImageKind>(months.length ? "ndvi" : "loss");
  const [requestedImage, setRequestedImage] = useState<{ period: string; kind: ImageKind } | null>(null);
  const [requestVersion, setRequestVersion] = useState(0);
  const [showComposite, setShowComposite] = useState(true);
  const [imageStatus, setImageStatus] = useState<"idle" | "loading" | "loaded" | "error">("idle");
  const [monthlyImage, setMonthlyImage] = useState<MonthlyImage | null>(null);
  const [failed, setFailed] = useState(false);

  useEffect(() => {
    let cancelled = false;
    let map: MapLibreMap | null = null;

    import("maplibre-gl")
      .then((maplibre) => {
        if (cancelled || !containerRef.current) return;
        maplibre.setWorkerUrl(WORKER_URL);
        const bounds = boundsOf([geometry.geometry]);
        map = new maplibre.Map({
          container: containerRef.current,
          style: MAP_STYLE,
          bounds: bounds ?? undefined,
          fitBoundsOptions: { padding: 40, maxZoom: 13 },
          center: bounds ? undefined : [-74, 4.5],
          zoom: bounds ? undefined : 5,
          attributionControl: { compact: true },
        });
        map.addControl(new maplibre.NavigationControl({ showCompass: false }), "top-right");
        map.on("load", () => {
          if (!map) return;
          map.addSource("proyecto", { type: "geojson", data: geometry as Feature });
          map.addLayer({
            id: "proyecto-relleno",
            type: "fill",
            source: "proyecto",
            paint: {
              "fill-color": categoryHex[category ?? "Insuficiente"],
              "fill-opacity": 0.4,
            },
          });
          map.addLayer({
            id: "proyecto-borde",
            type: "line",
            source: "proyecto",
            paint: { "line-color": "#f4f1e4", "line-width": 2 },
          });
          mapRef.current = map;
          setMapReady(true);
        });
        map.on("error", (event) => {
          if (event.error && !String(event.error.message).includes("Failed to fetch")) setFailed(true);
        });
      })
      .catch(() => setFailed(true));

    return () => {
      cancelled = true;
      setMapReady(false);
      mapRef.current = null;
      map?.remove();
    };
  }, [geometry, category]);

  useEffect(() => {
    if (!monthlyCompositeEnabled || !requestedImage) return;

    const controller = new AbortController();
    let objectUrl: string | null = null;
    const query =
      requestedImage.kind === "loss"
        ? new URLSearchParams({ year: requestedImage.period, kind: "loss" })
        : new URLSearchParams({
            month: `${requestedImage.period}-01`,
            kind: requestedImage.kind,
          });
    fetch(`/api/projects/${projectId}/thumbnail?${query}`, { signal: controller.signal })
      .then(async (response) => {
        if (!response.ok) {
          throw new Error(`La API respondió ${response.status}.`);
        }
        const coordinates = imageCoordinates(
          response.headers.get("x-image-bounds"),
          boundsOf([geometry.geometry]),
        );
        if (!coordinates) throw new Error("La API no devolvió los límites de la imagen.");
        objectUrl = URL.createObjectURL(await response.blob());
        setMonthlyImage({
          period: requestedImage.period,
          kind: requestedImage.kind,
          url: objectUrl,
          coordinates,
        });
        setImageStatus("loaded");
      })
      .catch((error: unknown) => {
        if (!controller.signal.aborted) {
          console.error("[bosque-vivo] no se pudo cargar la composición mensual", error);
          setImageStatus("error");
        }
      });

    return () => {
      controller.abort();
      if (objectUrl) URL.revokeObjectURL(objectUrl);
    };
  }, [geometry, monthlyCompositeEnabled, projectId, requestVersion, requestedImage]);

  useEffect(() => {
    const map = mapRef.current;
    if (!map || !mapReady) return;

    if (map.getLayer("composicion-mensual-layer")) {
      map.removeLayer("composicion-mensual-layer");
    }
    if (map.getSource("composicion-mensual")) {
      map.removeSource("composicion-mensual");
    }

    const bounds = boundsOf([geometry.geometry]);
    const selectedPeriod = selectedKind === "loss" ? selectedYear : selectedMonth;
    if (
      !monthlyCompositeEnabled ||
      !showComposite ||
      !selectedPeriod ||
      imageStatus !== "loaded" ||
      monthlyImage?.period !== selectedPeriod ||
      monthlyImage.kind !== selectedKind ||
      !bounds
    ) {
      return;
    }

    map.addSource("composicion-mensual", {
      type: "image",
      url: monthlyImage.url,
      coordinates: monthlyImage.coordinates,
    });
    map.addLayer(
      {
        id: "composicion-mensual-layer",
        type: "raster",
        source: "composicion-mensual",
        paint: { "raster-opacity": 0.95 },
      },
      "proyecto-borde",
    );
  }, [
    geometry,
    imageStatus,
    mapReady,
    monthlyCompositeEnabled,
    monthlyImage,
    projectId,
    selectedKind,
    selectedYear,
    selectedMonth,
    showComposite,
  ]);

  const formatMonth = (value: string) =>
    new Intl.DateTimeFormat("es-CO", { month: "long", year: "numeric", timeZone: "UTC" }).format(
      new Date(`${value}-01T00:00:00Z`),
    );

  return (
    <div className="space-y-3">
      <div className="relative overflow-hidden rounded-3xl bg-forest">
        <div
          ref={containerRef}
          className="h-[340px] w-full md:h-[420px]"
          role="application"
          aria-label={`Mapa satelital de ${name} con el contorno del proyecto.`}
        />
        {failed && (
          <p
            role="alert"
            className="glass absolute inset-x-4 top-4 rounded-2xl px-4 py-3 text-sm text-forest-foreground"
          >
            No pudimos cargar el mapa.
          </p>
        )}
      </div>
      {monthlyCompositeEnabled && (months.length > 0 || availableLossYears.length > 0) && (
        <div className="card-soft rounded-2xl p-4">
          <div className="flex flex-wrap items-end gap-3">
            <div>
              <label htmlFor={`image-kind-${projectId}`} className="block text-xs font-semibold">
                Visualización
              </label>
              <select
                id={`image-kind-${projectId}`}
                className="mt-1 max-w-full rounded-lg border border-border bg-background px-2 py-1.5 text-sm text-foreground"
                value={selectedKind}
                onChange={(event) => {
                  const kind = event.target.value;
                  if (kind === "ndvi" || kind === "loss") {
                    setSelectedKind(kind);
                    setRequestedImage(null);
                    setImageStatus("idle");
                    setMonthlyImage(null);
                  }
                }}
              >
                <option value="ndvi">NDVI (verdor)</option>
                {availableLossYears.length > 0 && <option value="loss">Pérdida de bosque</option>}
              </select>
            </div>
            {selectedKind === "loss" ? (
              <div>
                <label htmlFor={`year-${projectId}`} className="block text-xs font-semibold">
                  Año de pérdida
                </label>
                <select
                  id={`year-${projectId}`}
                  className="mt-1 max-w-full rounded-lg border border-border bg-background px-2 py-1.5 text-sm text-foreground"
                  value={selectedYear}
                  onChange={(event) => {
                    setSelectedYear(event.target.value);
                    setRequestedImage(null);
                    setImageStatus("idle");
                    setMonthlyImage(null);
                  }}
                >
                  {lossYears.map(({ year }) => (
                    <option key={year} value={year}>
                      {year}
                    </option>
                  ))}
                </select>
              </div>
            ) : (
              <div>
                <label htmlFor={`month-${projectId}`} className="block text-xs font-semibold">
                  Mes Sentinel-2
                </label>
                <select
                  id={`month-${projectId}`}
                  className="mt-1 max-w-full rounded-lg border border-border bg-background px-2 py-1.5 text-sm text-foreground"
                  value={selectedMonth}
                  onChange={(event) => {
                    setSelectedMonth(event.target.value);
                    setRequestedImage(null);
                    setImageStatus("idle");
                    setMonthlyImage(null);
                  }}
                >
                  {months.map((month) => (
                    <option key={month} value={month}>
                      {formatMonth(month)}
                    </option>
                  ))}
                </select>
              </div>
            )}
            {monthlyImage?.period === (selectedKind === "loss" ? selectedYear : selectedMonth) &&
            monthlyImage.kind === selectedKind &&
            imageStatus === "loaded" ? (
              <button
                type="button"
                className="rounded-lg border border-border bg-background px-3 py-2 text-sm text-foreground"
                onClick={() => setShowComposite((visible) => !visible)}
              >
                {showComposite ? "Ocultar composición" : "Mostrar composición"}
              </button>
            ) : (
              <button
                type="button"
                className="rounded-lg border border-border bg-background px-3 py-2 text-sm text-foreground disabled:opacity-60"
                disabled={imageStatus === "loading"}
                onClick={() => {
                  setShowComposite(true);
                  setImageStatus("loading");
                  setRequestedImage({
                    period: selectedKind === "loss" ? selectedYear : selectedMonth,
                    kind: selectedKind,
                  });
                  setRequestVersion((version) => version + 1);
                }}
              >
                {imageStatus === "loading"
                  ? "Cargando capa…"
                  : imageStatus === "error"
                    ? "Reintentar carga"
                    : selectedKind === "loss"
                      ? `Cargar pérdida de ${selectedYear}`
                      : "Cargar NDVI de este mes"}
              </button>
            )}
          </div>
          <p className="mt-3 text-sm text-muted-foreground">
            {imageStatus === "error"
              ? "No se pudo cargar. Comprueba la API y Earth Engine."
              : selectedKind === "loss"
                ? "Píxeles rojos: pérdida forestal marcada por Hansen para el año seleccionado."
                : "NDVI mensual (−1 a 1): marrón indica valores bajos y verde, valores altos."}
          </p>
          {selectedKind === "loss" && (
            <p className="mt-2 text-sm font-medium">
              Hansen registra{" "}
              {lossYears.find(({ year }) => String(year) === selectedYear)?.ha_lost.toLocaleString("es-CO", {
                maximumFractionDigits: 1,
              }) ?? "—"}{" "}
              ha de pérdida en {selectedYear}.
            </p>
          )}
          {selectedKind === "ndvi" && imageStatus === "loaded" && monthlyImage?.kind === "ndvi" && (
            <div className="mt-2 max-w-md" aria-label="Leyenda NDVI de menos uno a uno">
              <div className="h-2 rounded-full bg-gradient-to-r from-[#8c510a] via-[#f6e8c3] to-[#00441b]" />
              <div className="flex justify-between text-[10px] text-muted-foreground">
                <span>−1 · bajo</span>
                <span>0</span>
                <span>1 · alto</span>
              </div>
            </div>
          )}
          {selectedKind === "loss" && imageStatus === "loaded" && monthlyImage?.kind === "loss" && (
            <div className="mt-2 flex items-center gap-2 text-xs text-muted-foreground">
              <span className="inline-block h-3 w-3 rounded-sm bg-[#e31a1c]" />
              Píxel marcado como pérdida forestal por Hansen
            </div>
          )}
        </div>
      )}
    </div>
  );
}
