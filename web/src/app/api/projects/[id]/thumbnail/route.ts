export async function GET(request: Request, { params }: { params: Promise<{ id: string }> }) {
  const { id } = await params;
  const query = new URL(request.url).searchParams;
  const month = query.get("month");
  const year = query.get("year");
  const kind = query.get("kind") ?? "natural";
  const validId = /^[1-9]\d*$/.test(id);
  const validMonthlyRequest =
    ["natural", "ndvi"].includes(kind) &&
    month !== null &&
    /^\d{4}-(0[1-9]|1[0-2])-01$/.test(month) &&
    year === null;
  const validLossRequest =
    kind === "loss" &&
    year !== null &&
    /^(200[1-9]|201\d|202[0-4])$/.test(year) &&
    month === null;
  if (!validId || (!validMonthlyRequest && !validLossRequest)) {
    return Response.json(
      { detail: "Especifica un mes con ndvi/natural o un año con loss." },
      { status: 400 },
    );
  }

  const baseUrl = (process.env.BACKEND_URL ?? "http://127.0.0.1:8000").replace(/\/$/, "");
  let upstream: Response;
  try {
    const upstreamQuery =
      kind === "loss"
        ? `year=${year}&kind=loss`
        : `month=${encodeURIComponent(month!)}&kind=${kind}`;
    upstream = await fetch(
      `${baseUrl}/api/v1/projects/${id}/thumbnail?${upstreamQuery}`,
      { cache: "no-store", signal: AbortSignal.timeout(120_000) },
    );
  } catch {
    return Response.json(
      { detail: "No pudimos conectar con la API para generar la composición mensual." },
      { status: 503 },
    );
  }

  const headers = new Headers();
  for (const name of ["content-type", "cache-control", "x-image-bounds"]) {
    const value = upstream.headers.get(name);
    if (value) headers.set(name, value);
  }
  return new Response(upstream.body, { status: upstream.status, headers });
}
