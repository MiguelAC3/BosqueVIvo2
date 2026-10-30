# Bosque Vivo

Proyecto Integrador · Ingeniería Informática · ODS 15 (Vida de ecosistemas terrestres)
Politécnico Colombiano Jaime Isaza Cadavid · Segundo semestre de 2026

Sistema de información que contrasta lo que declaran los proyectos de carbono forestal con lo que se observa desde el satélite, y lo explica en lenguaje sencillo mediante un **asistente con IA** por WhatsApp y un panel web para auditores.

> **Estado:** núcleo implementado (RF01–RF10, RF12, RF13, RF15, RF17). Pendiente: mediciones de NFR, evaluación con Gemini real, validación con polígonos reales, RF11, RF14 y RF16.

## Decisiones del equipo (leer primero)

| Tema | Decisión |
|---|---|
| **Dónde corre** | **Todo en un computador local (Windows 11).** Sin máquinas virtuales, sin servidores, sin nube propia. |
| **Docker** | **No se usa.** En Windows corre sobre WSL2/Hyper-V (una VM). |
| **IA** | **Gemini de Google AI Studio, capa gratuita, por API** (SDK `google-genai`). **No se usa Ollama ni modelos locales.** |
| **Nivel del proyecto** | **Proyecto universitario, no tesis.** Se prioriza que funcione, se pueda demostrar, esté probado y documentado. |
| **Código abierto** | Todo el código propio es abierto. Gemini, Google Earth Engine y WhatsApp Cloud API son servicios gratuitos de terceros (el ERS contempla "servicios gratuitos de terceros"). |
| **Base de datos** | SQLite + shapely (sin PostgreSQL/PostGIS). |
| **Frontend** | Next.js en `web/` ([ADR 0013](docs/architecture/adr/0013-nextjs-para-el-frontend.md), propuesta). El panel Streamlit (`panel/`) queda en desuso hasta que se apruebe. |
| **Alcance** | ERS (RF01–RF08) + agente de IA que razona, analiza imágenes y concluye con evidencia (RF09–RF17, pendiente de aprobación del docente). |

Los nombres de modelo y las cuotas de Gemini cambian: verificar en AI Studio y configurar `GEMINI_MODEL` en el `.env`.

## Documentos

| Documento | Contenido |
|---|---|
| [`docs/planning/PLAN_IMPLEMENTACION.md`](docs/planning/PLAN_IMPLEMENTACION.md) | Plan maestro: arquitectura local, stack, estructura del repo, datos, índice, equipo, pruebas, cronograma, riesgos, primeros pasos |
| [`docs/planning/PLAN_AGENTE_IA.md`](docs/planning/PLAN_AGENTE_IA.md) | Diseño del agente con Gemini: herramientas, análisis de imágenes, conocimiento, voz, cuotas, seguridad, evaluación |
| [`docs/architecture`](docs/architecture/arquitectura.md) | Arquitectura y [ADRs](docs/architecture/adr/README.md) |
| [`docs/methodology/indice-v0.md`](docs/methodology/indice-v0.md) | Definición del índice, confianza y limitaciones |
| [`docs/fuentes-de-datos/datos-abiertos.md`](docs/fuentes-de-datos/datos-abiertos.md) | Pérdida de bosque con datos abiertos de Hansen, sin Earth Engine |
| [`docs/fuentes-de-datos/karnik.md`](docs/fuentes-de-datos/karnik.md) | Fuente de los polígonos (Karnik et al., 2025): licencia, reglas de importación, resultado para Colombia |
| [`docs/testing`](docs/testing/plan-de-pruebas.md) | Plan de pruebas, matriz RF → pruebas y resultados |
| [`docs/privacy`](docs/privacy/aviso-de-privacidad.md) | Aviso de privacidad (Ley 1581) |

## Ejecución local

Requisitos en cada PC: Python 3.12, [`uv`](https://docs.astral.sh/uv/), Node 22 o superior, Git, una clave de Google AI Studio, acceso a Google Earth Engine y (solo para WhatsApp) cloudflared o ngrok.

```
cp .env.example backend/.env        # completar claves (en Windows: Copy-Item)
cd backend && uv sync
uv run bosquevivo seed-demo          # tres proyectos de demostración
cd .. && scripts/dev.sh              # en Windows: scripts\dev.ps1
```

`scripts/dev` levanta la API y el sitio (`web/`), instala las dependencias de `web/` la primera vez y crea `web/.env.local` copiando el `ADMIN_TOKEN` de `backend/.env`.

- API: http://127.0.0.1:8000 (documentación en `/docs`)
- Sitio: http://localhost:3000
- Panel Streamlit (en desuso, opcional): `scripts\dev.ps1 -Streamlit` o `STREAMLIT=1 scripts/dev.sh` → http://127.0.0.1:8501

Para probar sin credenciales, use `SATELLITE_PROVIDER=fake` y los datos de demostración. El chat y WhatsApp sí requieren `GEMINI_API_KEY` y `GEMINI_MODEL`. El chat y las trazas del sitio están protegidos con `AUDITOR_ACCESS_CODE` y `SESSION_SECRET` en `web/.env.local` (sin definirlos quedan abiertos en desarrollo). Más detalle en [`web/README.md`](web/README.md).

Importar los polígonos reales de Colombia (base de Karnik et al., ver [`docs/fuentes-de-datos/karnik.md`](docs/fuentes-de-datos/karnik.md)) y calcular una auditoría:

```
uv run bosquevivo import-karnik ../data/raw/karnik_south_america/south_america.gpkg --country Colombia
uv run bosquevivo audit 7        # las series empiezan 3 años antes del T0 del proyecto
```

Para calcular **todos** los proyectos de una vez, con datos abiertos y sin cuenta de Google (solo pérdida de bosque y permanencia; ver [`docs/fuentes-de-datos/datos-abiertos.md`](docs/fuentes-de-datos/datos-abiertos.md)):

```
SATELLITE_PROVIDER=hansen uv run bosquevivo audit-batch --country Colombia
```

`audit` con `SATELLITE_PROVIDER=gee` (NDVI completo y miniaturas) necesita `GEE_PROJECT` y haber ejecutado `earthengine authenticate`. Para un tipo de archivo distinto use `bosquevivo import <archivo> --source <nombre>`.

En el detalle de un proyecto, el mapa permite elegir un mes y una visualización (NDVI o color natural), o un año para ver píxeles de pérdida forestal Hansen. Cada imagen se solicita únicamente al pulsar el botón: `GET /api/v1/projects/{id}/thumbnail?month=AAAA-MM-01&kind=ndvi` (o `kind=natural`) y `GET /api/v1/projects/{id}/thumbnail?year=AAAA&kind=loss`. El backend solo genera imágenes mensuales para meses Sentinel-2 con observaciones guardadas y capas Hansen para años con pérdidas registradas. Las imágenes se recortan al polígono y se cachean para reutilizarse.

Calidad:

```
cd backend && uv run ruff check . && uv run pyright && uv run pytest
cd web && npm run lint && npm run typecheck && npm test && npm run build
```

Para la demo y el piloto, el computador debe estar encendido, sin suspensión y con Internet estable.

## Estructura

```
backend/   API, módulos de dominio, agente de IA y pruebas
web/       sitio Next.js (inicio, explorador, detalle, chat del auditor, trazas, metodología)
panel/     panel Streamlit de solo lectura (en desuso, ver ADR 0013)
docs/      ERS, planes, arquitectura y ADRs, metodología, pruebas y privacidad
scripts/   arranque local
```

## Reglas del repositorio

- `.env` y `data/` **nunca** se suben a git.
- No se envían a Gemini teléfonos ni nombres; el teléfono solo se guarda como HMAC.
- Lenguaje neutro en todo el producto: "correspondencia observada", nunca "fraude".
- Ramas cortas, PR con revisión de otra persona, commits convencionales.

## Equipo

| Persona | Rol |
|---|---|
| P1 | Plataforma e integración (tech lead) |
| P2 | Datos geoespaciales |
| P3 | Analítica y reportes |
| P4 | Agente y experiencia |

Detalle de responsabilidades por requisito en `docs/planning/PLAN_IMPLEMENTACION.md`.
