#!/usr/bin/env python3
"""
Radar de Consultorias -- SEACE data pipeline.

Fetches "contratos menores" (Consultoria de Obra + Servicio, estado Vigente) from
the public SEACE buscador API, scores them against Jhon Ribbeck's profile
(structural/BIM civil engineer, RNP Consultor de Obras + Bienes y Servicios),
fetches the requerimiento PDF file id for each match, and writes:
  - seace_dashboard_data.json  (compact data consumed by the dashboard template)
  - seace_dashboard.html       (template + data + curated analysis, ready to publish)

Uses only the Python standard library so it runs anywhere (local Windows/Linux,
cloud sandbox) without extra pip installs.
"""
import json
import re
import time
import urllib.request
from pathlib import Path
from datetime import datetime, timezone

HERE = Path(__file__).resolve().parent
BASE = "https://prod6.seace.gob.pe/v1/s8uit-services/buscadorpublico/contrataciones/buscador"
DETAIL_FILES = "https://prod6.seace.gob.pe/v1/s8uit-services/archivo/archivos-publico/listar-archivos-contrato"

ANIO = 2026
OBJETOS = "4,2"       # Consultoria de Obra (4) + Servicio (2)
ESTADO = "2"           # Vigente
PAGE_SIZE = 200

# Keyword weights against Jhon Ribbeck's profile: structural/BIM civil engineer,
# RNP registered as Consultor de Obras + Bienes y Servicios (no ejecutor de obra).
# Team includes electrical/sanitary engineers and an architect.
WEIGHTS = {
    # Alta relevancia: estructuras, expedientes tecnicos, supervision/consultoria de obra, BIM
    "expediente tecnico": 3,
    "estructura": 3,
    "estructural": 3,
    "sismorresistente": 3,
    "sismico": 3,
    "supervision de obra": 3,
    "supervision tecnica": 3,
    "residente de obra": 3,
    "consultoria de obra": 3,
    "liquidacion de obra": 3,
    "compatibilizacion": 3,
    "edificacion": 3,
    "edificaciones": 3,
    "bim": 3,
    "diseno estructural": 3,
    "evaluacion estructural": 3,
    "inspeccion tecnica de edificacion": 3,
    "expediente de saneamiento": 2,
    "estudio de preinversion": 2,
    "perfil tecnico": 2,
    "ficha tecnica": 2,
    "valorizacion de obra": 2,
    # Media relevancia: especialidades del equipo (electrico, sanitario, arquitectura)
    "instalaciones electricas": 2,
    "electrificacion": 2,
    "instalaciones sanitarias": 2,
    "agua potable": 2,
    "alcantarillado": 2,
    "aguas residuales": 2,
    "agua y desague": 2,
    "arquitectura": 2,
    "diseno arquitectonico": 2,
    "habilitacion urbana": 2,
    # Baja relevancia: contexto general de obras/infraestructura
    "obra publica": 1,
    "infraestructura": 1,
    "construccion": 1,
    "proyecto de inversion": 1,
    "mantenimiento de infraestructura": 1,
}

ACCENTS = str.maketrans("áéíóúñÁÉÍÓÚÑ", "aeiounAEIOUN")


def strip_accents(s):
    return (s or "").translate(ACCENTS).lower()


def http_get_json(url, retries=3):
    last_err = None
    for _ in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.loads(r.read().decode("utf-8"))
        except Exception as e:  # noqa: BLE001
            last_err = e
            time.sleep(1)
    raise last_err


def fetch_all():
    url = f"{BASE}?anio={ANIO}&lista_codigo_objeto={OBJETOS}&lista_estado_contrato={ESTADO}&orden=2&page=1&page_size={PAGE_SIZE}"
    first = http_get_json(url)
    total = first["pageable"]["totalElements"]
    pages = -(-total // PAGE_SIZE)
    print(f"Total elementos: {total} en {pages} paginas")
    items = list(first["data"])
    for p in range(2, pages + 1):
        url = f"{BASE}?anio={ANIO}&lista_codigo_objeto={OBJETOS}&lista_estado_contrato={ESTADO}&orden=2&page={p}&page_size={PAGE_SIZE}"
        r = http_get_json(url)
        items.extend(r["data"])
        time.sleep(0.15)
    print(f"Descargados: {len(items)}")
    return items


def score_item(item):
    text = strip_accents(
        f"{item.get('desContratacion','')} {item.get('desObjetoContrato','')} {item.get('nomEntidad','')}"
    )
    score = 0
    matched = []
    for kw, w in WEIGHTS.items():
        if re.search(r"\b" + re.escape(kw) + r"\b", text):
            score += w
            matched.append(kw)
    return score, matched


def clean(s):
    if not s:
        return ""
    s = s.replace("¿", "-")
    return re.sub(r"\s+", " ", s).strip()


def main():
    raw = fetch_all()
    scored = []
    for it in raw:
        score, matched = score_item(it)
        if score > 0:
            scored.append({
                "id": it["idContrato"],
                "num": clean(it["desContratacion"]),
                "ent": clean(it["nomEntidad"]),
                "obj": it["nomObjetoContrato"],
                "desc": clean(it["desObjetoContrato"]),
                "fin": it["fecFinCotizacion"],
                "pub": it["fecPublica"],
                "score": score,
                "kw": matched,
            })
    scored.sort(key=lambda x: -x["score"])
    print(f"Coincidencias con score > 0: {len(scored)}")

    # Fetch requerimiento file id + area usuaria / lugar for every match
    # (cheap JSON calls, no PDF download).
    for d in scored:
        try:
            files = http_get_json(f"{DETAIL_FILES}/{d['id']}/1")
            if files:
                d["archivoId"] = files[0]["idContratoArchivo"]
        except Exception:  # noqa: BLE001
            pass
        try:
            detail = http_get_json(
                f"https://prod6.seace.gob.pe/v1/s8uit-services/buscadorpublico/contrataciones/listar-completo?id_contrato={d['id']}"
            )
            comp = detail.get("uitContratoCompletoProjection", {})
            d["area"] = clean(comp.get("nomAreaUsuaria", ""))
            d["tipo"] = clean(comp.get("nomTipoInvitacion", ""))
            items = detail.get("uitContratoItemProjectionList", [])
            lugares = []
            for it in items:
                nd = it.get("nomDistritoExt")
                if nd and nd not in lugares:
                    lugares.append(nd)
            d["lugar"] = clean(" | ".join(lugares))
        except Exception:  # noqa: BLE001
            d["area"] = ""
            d["tipo"] = ""
            d["lugar"] = ""
        time.sleep(0.1)

    data_path = HERE / "seace_dashboard_data.json"
    data_path.write_text(json.dumps(scored, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    print(f"Guardado: {data_path} ({len(json.dumps(scored, ensure_ascii=False))} bytes)")

    # Build the final HTML by merging with the template + curated analysis.
    template_path = HERE / "seace_template.html"
    analisis_path = HERE / "seace_analisis.json"
    out_path = HERE / "seace_dashboard.html"

    data_text = json.dumps(scored, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")
    analisis_text = analisis_path.read_text(encoding="utf-8").replace("</", "<\\/") if analisis_path.exists() else "{}"
    template = template_path.read_text(encoding="utf-8")

    now_lima = datetime.now(timezone.utc).astimezone().strftime("%d/%m/%Y %H:%M")
    html = template.replace("__DATA_JSON__", data_text)
    html = html.replace("__ANALISIS_JSON__", analisis_text)
    html = html.replace("__GENERATED_AT__", f"{now_lima} (UTC)")

    out_path.write_text(html, encoding="utf-8")
    print(f"Dashboard generado: {out_path} ({len(html)} bytes)")


if __name__ == "__main__":
    main()
