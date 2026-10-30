import argparse
import sys
import time
from datetime import date
from pathlib import Path

from bosquevivo.ai.agent import Agent, default_registry
from bosquevivo.ai.eval import EvalRunner, load_cases
from bosquevivo.ai.knowledge import KnowledgeBase
from bosquevivo.assistant.runtime import build_llm
from bosquevivo.core.config import get_settings
from bosquevivo.core.logging import configure_logging
from bosquevivo.db.session import get_session, init_db
from bosquevivo.demo import seed_demo
from bosquevivo.pipeline import (
    BatchItem,
    audit_project,
    generate_missing_reports,
    recompute_batch,
    run_batch,
    select_projects,
)
from bosquevivo.registry.importer import import_file
from bosquevivo.registry.karnik import import_karnik
from bosquevivo.satellite.factory import build_provider


def cmd_init_db(_: argparse.Namespace) -> int:
    init_db()
    return 0


def cmd_seed_demo(_: argparse.Namespace) -> int:
    init_db()
    settings = get_settings()
    with get_session() as session:
        projects = seed_demo(session, settings.images_dir)
        print(f"{len(projects)} proyectos de demostración creados")
    return 0


def cmd_import(args: argparse.Namespace) -> int:
    init_db()
    with get_session() as session:
        summary = import_file(
            session,
            Path(args.path),
            source=args.source,
            source_version=args.version,
            country=args.country,
            limit=args.limit,
        )
        print(
            f"creados={summary.created} actualizados={summary.updated} "
            f"reparados={summary.repaired} omitidos={summary.skipped}"
        )
    return 0


def cmd_import_karnik(args: argparse.Namespace) -> int:
    init_db()
    with get_session() as session:
        summary = import_karnik(session, Path(args.path), country=args.country, limit=args.limit)
        print(
            f"creados={summary.created} actualizados={summary.updated} "
            f"reparados={summary.repaired} simplificados={summary.simplified} "
            f"con_area_de_contabilidad={summary.accounting_area_used} "
            f"omitidos_por_ser_punto={len(summary.skipped_points)} "
            f"omitidos_otros={len(summary.skipped_other)}"
        )
        for item in summary.skipped_points:
            print(f"  punto (no auditable): {item}")
        for item in summary.skipped_other:
            print(f"  omitido: {item}")
    return 0


def cmd_audit(args: argparse.Namespace) -> int:
    init_db()
    settings = get_settings()
    provider = build_provider(settings)
    with get_session() as session:
        start = date.fromisoformat(args.start) if args.start else None
        run = audit_project(
            session,
            provider,
            settings.images_dir,
            args.project_id,
            start=start,
            index_version=args.index_version or settings.index_version,
        )
        result = run.result
        print(f"corrida={run.id} categoria={result.category if result else None}")
    return 0


def _print_batch_item(index: int, total: int, item: BatchItem) -> None:
    area = f"{item.area_ha:,.0f} ha" if item.area_ha else "área sin dato"
    if item.ok:
        score = "—" if item.score is None else f"{item.score:.0f}"
        outcome = f"{item.category} (índice {score})"
    else:
        outcome = f"ERROR {item.error}"
    label = f"[{index}/{total}] #{item.project_id} {item.name[:48]}"
    print(f"{label} · {area} -> {outcome} · {item.seconds}s")


def cmd_gemini_check(_: argparse.Namespace) -> int:
    """Verifica la clave de Gemini, lista los modelos disponibles y prueba el configurado."""
    from google import genai  # noqa: PLC0415 - solo se importa si se usa este comando

    settings = get_settings()
    if not settings.gemini_api_key:
        print(
            "Falta GEMINI_API_KEY en backend/.env (se crea en https://aistudio.google.com/apikey)"
        )
        return 1
    client = genai.Client(api_key=settings.gemini_api_key)
    try:
        models = sorted(
            (m.name or "").removeprefix("models/")
            for m in client.models.list()
            if "generateContent" in (m.supported_actions or [])
        )
    except Exception as exc:  # clave inválida, sin red o sin cuota
        print(f"No se pudo consultar Gemini: {exc}")
        return 1
    print(f"La clave funciona. Modelos que permiten generar texto ({len(models)}):")
    for name in models:
        print(f"  {name}")
    for label, model in (
        ("GEMINI_MODEL", settings.gemini_model),
        ("GEMINI_MODEL_LIGHT", settings.gemini_model_light),
    ):
        if not model:
            print(f"{label}: sin configurar (elige uno de la lista y ponlo en backend/.env)")
            continue
        try:
            reply = client.models.generate_content(
                model=model, contents="Responde solo con la palabra: listo"
            )
            print(f"{label}={model}: responde «{(reply.text or '').strip()[:40]}»")
        except Exception as exc:
            print(f"{label}={model}: ERROR {str(exc)[:160]}")
            return 1
    return 0


def cmd_reports(_: argparse.Namespace) -> int:
    init_db()
    with get_session() as session:
        created = generate_missing_reports(session, get_settings().reports_dir)
    print(f"reportes HTML generados: {created}")
    return 0


def _parse_shard(value: str | None) -> tuple[int, int] | None:
    """«2/4» → (1, 4): la parte 2 de 4 (numeradas desde 1)."""
    if not value:
        return None
    part, total = (int(piece) for piece in value.split("/"))
    if not 1 <= part <= total:
        raise SystemExit("--shard debe ser i/n con 1 <= i <= n, por ejemplo 2/4")
    return part - 1, total


def cmd_audit_batch(args: argparse.Namespace) -> int:
    init_db()
    settings = get_settings()
    provider = build_provider(settings)
    with get_session() as session:
        projects = select_projects(
            session,
            country=args.country,
            include_arr=args.include_arr,
            max_area_ha=args.max_area_ha,
            redo=args.redo,
            missing_index=args.missing_index,
            limit=args.limit,
            shard=_parse_shard(args.shard),
        )
        print(f"{len(projects)} proyectos por calcular con el proveedor «{provider.name}»")
        began = time.monotonic()
        items = run_batch(
            session,
            provider,
            settings.images_dir,
            projects,
            start=date.fromisoformat(args.start) if args.start else None,
            index_version=args.index_version or settings.index_version,
            reports_dir=settings.reports_dir,
            on_item=_print_batch_item,
        )
    failed = [item for item in items if not item.ok]
    print(
        f"terminados: {len(items) - len(failed)} bien, {len(failed)} con error, "
        f"{time.monotonic() - began:.0f}s en total"
    )
    return 1 if failed else 0


def cmd_recompute(args: argparse.Namespace) -> int:
    init_db()
    settings = get_settings()
    version = args.index_version or settings.index_version
    with get_session() as session:
        items = recompute_batch(
            session,
            version,
            settings.reports_dir,
            country=args.country,
            on_item=_print_batch_item,
        )
    failed = [item for item in items if not item.ok]
    print(f"índice {version} recalculado: {len(items) - len(failed)} bien, {len(failed)} con error")
    return 1 if failed else 0


def cmd_eval(args: argparse.Namespace) -> int:
    init_db()
    settings = get_settings()
    llm = build_llm(settings)
    with get_session() as session:
        KnowledgeBase(session).load_directory()
        agent = Agent(session, llm, default_registry(), settings)
        report = EvalRunner(session, agent).run(load_cases())
        output = Path(args.output)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(report.to_json(), encoding="utf-8")
        print(report.summary())
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="bosquevivo")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("init-db").set_defaults(func=cmd_init_db)
    sub.add_parser("seed-demo").set_defaults(func=cmd_seed_demo)
    imp = sub.add_parser("import")
    imp.add_argument("path")
    imp.add_argument("--source", required=True)
    imp.add_argument("--version")
    imp.add_argument("--country")
    imp.add_argument("--limit", type=int)
    imp.set_defaults(func=cmd_import)
    karnik = sub.add_parser("import-karnik", help="Importa la base de Karnik et al. (2025)")
    karnik.add_argument("path", help="GeoPackage continental, p. ej. south_america.gpkg")
    karnik.add_argument("--country")
    karnik.add_argument("--limit", type=int)
    karnik.set_defaults(func=cmd_import_karnik)
    batch = sub.add_parser("audit-batch", help="Calcula las auditorías de varios proyectos")
    batch.add_argument("--country")
    batch.add_argument("--limit", type=int, help="máximo de proyectos (los más chicos primero)")
    batch.add_argument("--max-area-ha", type=float, help="omite proyectos más grandes que esto")
    batch.add_argument("--include-arr", action="store_true", help="incluye reforestación (ARR)")
    batch.add_argument("--redo", action="store_true", help="recalcula los que ya tienen auditoría")
    batch.add_argument(
        "--missing-index",
        action="store_true",
        help="solo los que aún no tienen índice completo (sin auditoría o con resultado parcial)",
    )
    batch.add_argument("--start", help="AAAA-MM-DD; por defecto, 3 años antes de T0 de cada uno")
    batch.add_argument(
        "--shard", help="i/n: calcula solo la parte i de n, para correr varios procesos"
    )
    batch.add_argument("--index-version", help="v0 o v1; por defecto INDEX_VERSION (v1)")
    batch.set_defaults(func=cmd_audit_batch)
    recompute = sub.add_parser(
        "recompute", help="Recalcula el índice con otra versión usando los datos ya guardados"
    )
    recompute.add_argument("--country")
    recompute.add_argument("--index-version", help="v0 o v1; por defecto INDEX_VERSION (v1)")
    recompute.set_defaults(func=cmd_recompute)
    gemini = sub.add_parser("gemini-check", help="Verifica la clave y los modelos de Gemini")
    gemini.set_defaults(func=cmd_gemini_check)
    reports = sub.add_parser("reports", help="Genera los reportes HTML que falten")
    reports.set_defaults(func=cmd_reports)
    audit = sub.add_parser("audit")
    audit.add_argument("project_id", type=int)
    audit.add_argument(
        "--start", help="AAAA-MM-DD; por defecto, 3 años antes de T0 (mínimo el año 2000)"
    )
    audit.add_argument("--index-version", help="v0 o v1; por defecto INDEX_VERSION (v1)")
    audit.set_defaults(func=cmd_audit)
    evaluate = sub.add_parser("eval")
    evaluate.add_argument("--output", default="data/eval/report.json")
    evaluate.set_defaults(func=cmd_eval)
    return parser


def main(argv: list[str] | None = None) -> int:
    # La consola de Windows usa cp1252: un carácter que no exista allí no debe tumbar el comando.
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure:
            reconfigure(errors="replace")
    configure_logging()
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
