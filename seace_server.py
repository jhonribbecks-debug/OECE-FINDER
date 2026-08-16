#!/usr/bin/env python3
"""
OECE Finder -- servidor local para vista previa de PDFs y postulacion.

Uso:
    python seace_server.py [puerto]      (por defecto 8000)

Endpoints:
    GET  /                      -> seace_dashboard.html
    GET  /pdf/<archivoId>       -> PDF del requerimiento en linea (vista previa, sin descargar)
    GET  /formato/<tipo>.docx   -> genera un formato (.docx) para llenar (declaracion RTM, carta CCI, oferta, TDR/EETT)
    GET  /formato/<tipo>.html   -> vista previa en pantalla del formato (mismo contenido, con RUC/nombre rellenados)
    GET  /docs                  -> lista los archivos de mis_documentos/ (RNP.pdf, Ficha_RUC.pdf, etc.)
    GET  /doc/<archivo>         -> sirve un archivo de mis_documentos/ en linea (para previsualizar RNP / ficha RUC)
    GET  /analizar/<id>         -> descarga el PDF del proceso, aplica OCR y genera/guarda el checklist de requisitos
    GET  /import?num=<codigo>   -> agrega un proceso a la data por su codigo (ej: CM-373-2026-SBS)
    GET  /config                -> lee config_local.json (ruc, razon social, correo, celular)
    POST /config                -> guarda config_local.json (NUNCA la clave SUNAT)
    POST /refresh               -> re-ejecuta seace_pipeline.py (descarga y puntua procesos)
    GET  /refresh/status        -> estado de la actualizacion en curso

La clave de SUNAT/SEACE se ingresa SOLO en el navegador al momento de enviar
la cotizacion en la pagina oficial. Este servidor jamas la almacena.
"""
import io
import json
import re
import subprocess
import sys
import threading
import urllib.parse
import urllib.request
import zipfile
from datetime import datetime, timezone
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from xml.sax.saxutils import escape

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import seace_pipeline as pipe  # noqa: E402  (reusa WEIGHTS, score_item, http_get_json)
import seace_ocr as ocr  # noqa: E402  (extrae texto de PDFs + detecta requisitos)

MIS_DOCS_DIR = HERE / "mis_documentos"

SEACE_DOWNLOAD = (
    "https://prod6.seace.gob.pe/v1/s8uit-services/archivo/"
    "archivos-publico/descargar-archivo-contrato/{archivo_id}"
)
CONFIG_PATH = HERE / "config_local.json"
DATA_PATH = HERE / "seace_dashboard_data.json"
TEMPLATE_PATH = HERE / "seace_template.html"
ANALISIS_PATH = HERE / "seace_analisis.json"
OUT_PATH = HERE / "seace_dashboard.html"

_refresh_lock = threading.Lock()
_refresh_state = {"running": False, "done": False, "error": None, "started": None}

DEFAULT_CONFIG = {
    "ruc": "",
    "rnp": "",
    "razonSocial": "",
    "correo": "",
    "celular": "",
}


# --------------------------------------------------------------------------
# Formatos .docx (minimo OOXML)
# --------------------------------------------------------------------------
FORMATOS = {
    "declaracion_rtm": {
        "titulo": "DECLARACIÓN JURADA DE CUMPLIMIENTO DE REQUISITOS TÉCNICOS MÍNIMOS (RTM)",
        "parrafos": [
            "Yo, [NOMBRE DEL PROVEEDOR / REPRESENTANTE], identificado(a) con [DNI/RUC N°], con RUC N° [RUC], en mi calidad de [CARGO], DECLARO BAJO JURAMENTO que cumplo y me comprometo a cumplir todos los Requisitos Técnicos Mínimos (RTM) establecidos en el requerimiento del proceso [N° DE PROCESO], de la entidad [ENTIDAD], conforme a los Términos de Referencia.",
            "Asimismo, declaro conocer las condiciones, plazos y obligaciones señaladas en el requerimiento, así como la veracidad de la información presentada.",
            "Lugar y fecha: [LUGAR], [FECHA]",
            "_____________________________",
            "Firma y post firma del proveedor",
        ],
    },
    "carta_cci": {
        "titulo": "CARTA CCI",
        "parrafos": [
            "Señores:",
            "[ENTIDAD]",
            "De mi consideración:",
            "Yo, [NOMBRE DEL PROVEEDOR / REPRESENTANTE], con RUC N° [RUC], en relación al proceso [N° DE PROCESO] correspondiente a [OBJETO DEL PROCESO], presento la presente carta en cumplimiento de lo solicitado en el requerimiento.",
            "Sin otro particular, me despido.",
            "Atentamente,",
            "_____________________________",
            "Firma y post firma del proveedor",
        ],
    },
    "oferta_economica": {
        "titulo": "OFERTA ECONÓMICA / PROPUESTA ECONÓMICA",
        "parrafos": [
            "Proceso: [N° DE PROCESO]",
            "Entidad: [ENTIDAD]",
            "Proveedor: [NOMBRE DEL PROVEEDOR] — RUC: [RUC]",
            "ÍTEM: [DESCRIPCIÓN DEL ÍTEM]",
            "Precio Unitario (sin IGV): S/ [PRECIO UNITARIO]",
            "Cantidad: [CANTIDAD]",
            "Precio Total (sin IGV): S/ [PRECIO TOTAL]",
            "Vigencia de la oferta: [VIGENCIA] días calendario",
            "Lugar y fecha: [LUGAR], [FECHA]",
            "_____________________________",
            "Firma y post firma del proveedor",
        ],
    },
    "tdr_eett": {
        "titulo": "DOCUMENTOS QUE ACREDITAN EL CUMPLIMIENTO DE TDR O EETT",
        "parrafos": [
            "Proceso: [N° DE PROCESO]",
            "Entidad: [ENTIDAD]",
            "Proveedor: [NOMBRE DEL PROVEEDOR] — RUC: [RUC]",
            "En calidad de [CARGO], declaro que los documentos adjuntos acreditan el cumplimiento de los Términos de Referencia / Especificaciones Técnicas del proceso indicado, y me comprometo a ejecutar el servicio conforme a lo requerido.",
            "Adjuntar: certificados, constancias, fichas técnicas u otros medios de verificación.",
            "_____________________________",
            "Firma y post firma del proveedor",
        ],
    },
}


def make_docx(titulo, parrafos):
    body = []
    body.append(
        '<w:p><w:pPr><w:jc w:val="center"/><w:spacing w:after="240"/></w:pPr>'
        f'<w:r><w:rPr><w:b/><w:sz w:val="32"/></w:rPr><w:t xml:space="preserve">{escape(titulo)}</w:t></w:r></w:p>'
    )
    body.append('<w:p><w:pPr><w:spacing w:after="240"/></w:pPr><w:r><w:t xml:space="preserve"/></w:r></w:p>')
    for p in parrafos:
        body.append(
            '<w:p><w:pPr><w:spacing w:after="160"/></w:pPr>'
            f'<w:r><w:t xml:space="preserve">{escape(p)}</w:t></w:r></w:p>'
        )
    document_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        f'<w:body>{"".join(body)}</w:body></w:document>'
    )
    content_types = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        '<Override PartName="/word/document.xml" '
        'ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
        '</Types>'
    )
    rels = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" '
        'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" '
        'Target="word/document.xml"/></Relationships>'
    )
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", content_types)
        z.writestr("_rels/.rels", rels)
        z.writestr("word/document.xml", document_xml)
    return buf.getvalue()


def make_format_html(tipo, cfg=None):
    """Vista previa en pantalla de un formato (.docx) renderizado como HTML.

    Mismo contenido que make_docx() pero legible en el iframe del dashboard,
    con el RUC / razon social ya rellenados desde la config local.
    """
    cfg = cfg or {}
    data = FORMATOS.get(tipo)
    if not data:
        return None
    parrafos = []
    for p in data["parrafos"]:
        p = p.replace("[RUC]", cfg.get("ruc") or "[RUC]")
        p = p.replace("[NOMBRE DEL PROVEEDOR / REPRESENTANTE]", cfg.get("razonSocial") or "[NOMBRE]")
        p = p.replace("[NOMBRE DEL PROVEEDOR]", cfg.get("razonSocial") or "[NOMBRE]")
        parrafos.append(p)
    body = "".join(
        f'<p>{escape(p)}</p>' if not p.startswith("____") else f'<p style="margin-top:28px;white-space:pre;">{escape(p)}</p>'
        for p in parrafos
    )
    html = (
        "<!DOCTYPE html><html lang=\"es\"><head><meta charset=\"utf-8\"/>"
        "<title>Formato</title><style>"
        "body{font-family:Calibri,'Segoe UI',Arial,sans-serif;color:#1a1a1a;margin:0;padding:48px 64px;"
        "line-height:1.55;font-size:15px;background:#fff;} "
        "h1{font-size:19px;text-align:center;line-height:1.35;margin:0 0 32px;}"
        "p{margin:0 0 14px;text-align:justify;} "
        "footer{margin-top:36px;color:#777;font-size:12px;border-top:1px solid #e2e2e2;padding-top:10px;}"
        "</style></head><body>"
        f"<h1>{escape(data['titulo'])}</h1>{body}"
        "<footer>Documento generado automáticamente por OECE Finder. Revisa y completa los campos en corchetes antes de presentar.</footer>"
        "</body></html>"
    )
    return html


# --------------------------------------------------------------------------
# Config local (NUNCA guarda la clave)
# --------------------------------------------------------------------------
def load_config():
    if CONFIG_PATH.exists():
        try:
            data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            merged = dict(DEFAULT_CONFIG)
            merged.update({k: v for k, v in data.items() if k in DEFAULT_CONFIG})
            return merged
        except Exception:  # noqa: BLE001
            pass
    return dict(DEFAULT_CONFIG)


def save_config(cfg):
    safe = {k: str(cfg.get(k, "")) for k in DEFAULT_CONFIG}
    CONFIG_PATH.write_text(json.dumps(safe, ensure_ascii=False, indent=2), encoding="utf-8")


# --------------------------------------------------------------------------
# Importar proceso por codigo
# --------------------------------------------------------------------------
def build_dashboard():
    scored = json.loads(DATA_PATH.read_text(encoding="utf-8"))
    data_text = json.dumps(scored, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")
    analisis_text = (
        ANALISIS_PATH.read_text(encoding="utf-8").replace("</", "<\\/")
        if ANALISIS_PATH.exists() else "{}"
    )
    template = TEMPLATE_PATH.read_text(encoding="utf-8")
    now_lima = datetime.now(timezone.utc).astimezone().strftime("%d/%m/%Y %H:%M")
    html = template.replace("__DATA_JSON__", data_text)
    html = html.replace("__ANALISIS_JSON__", analisis_text)
    html = html.replace("__GENERATED_AT__", f"{now_lima} (UTC)")
    OUT_PATH.write_text(html, encoding="utf-8")


def merge_analisis(base, ocr):
    """Complementa un analisis existente con los requisitos detectados por OCR.

    Nunca reemplaza: conserva el checklist manual y agrega solo los ítems
    nuevos que el OCR detecta y que no existen ya. Para evitar duplicados
    irrelevantes, compara por similitud de tokens: si el ítem nuevo repite
    >= 60% de sus palabras clave contra algún ítem existente, se descarta.
    Mantiene alertas/recomendaciones del análisis base.
    """
    def toks(s):
        return set(w for w in re.findall(r"[a-z0-9áéíóúñ]{4,}", (s or "").lower()))

    merged = dict(base)
    docs = list(base.get("docs", []))
    have_toks = [toks(d.get("item", "")) for d in docs]

    def is_dup(item):
        t = toks(item)
        if not t:
            return True
        for et in have_toks:
            if not et:
                continue
            inter = len(t & et)
            if inter >= 1 and inter >= 0.6 * len(t):
                return True
        return False

    added = 0
    for d in ocr.get("docs", []):
        item = d.get("item", "")
        if is_dup(item):
            continue
        nd = dict(d)
        nota = nd.get("nota", "")
        nd["nota"] = (nota + " (Detectado automáticamente por OCR.)").strip()
        docs.append(nd)
        have_toks.append(toks(item))
        added += 1
    if added:
        docs.append({
            "item": f"Requisitos adicionales detectados por OCR: +{added}",
            "estado": "verificar",
            "nota": "El OCR del PDF detectó ítems nuevos que no estaban en el checklist previo. Verifícalos en el documento original.",
        })
    merged["docs"] = docs

    alertas = list(base.get("alertas", []))
    if added:
        alertas.append(f"Análisis complementado con OCR: se agregaron {added} requisito(s) nuevo(s) al checklist existente.")
    elif base.get("origen") != "ocr-auto":
        alertas.append("El OCR no detectó requisitos nuevos: el checklist existente ya cubre lo extraído del PDF.")
    merged["alertas"] = alertas
    return merged


def import_process(num):
    num = num.strip()
    if not num:
        return {"ok": False, "error": "Código vacío"}
    base = pipe.BASE
    url = (f"{base}?anio={pipe.ANIO}&lista_codigo_objeto={pipe.OBJETOS}"
           f"&lista_estado_contrato={pipe.ESTADO}&orden=2&page=1&page_size=100"
           f"&palabra={urllib.parse.quote(num)}")
    resp = pipe.http_get_json(url)
    hit = None
    for it in resp.get("data", []):
        if pipe.strip_accents(it.get("desContratacion", "")).strip() == pipe.strip_accents(num).strip():
            hit = it
            break
    if not hit:
        return {"ok": False, "error": f"No se encontró el proceso '{num}' en SEACE. Verifica el código."}
    score, matched = pipe.score_item(hit)
    rec = {
        "id": hit["idContrato"],
        "num": pipe.clean(hit.get("desContratacion", "")),
        "ent": pipe.clean(hit.get("nomEntidad", "")),
        "obj": hit.get("nomObjetoContrato", ""),
        "desc": pipe.clean(hit.get("desObjetoContrato", "")),
        "fin": hit.get("fecFinCotizacion", ""),
        "pub": hit.get("fecPublica", ""),
        "score": score,
        "kw": matched,
        "imp": True,
    }
    try:
        files = pipe.http_get_json(pipe.DETAIL_FILES + f"/{rec['id']}/1")
        if files:
            rec["archivoId"] = files[0]["idContratoArchivo"]
    except Exception:  # noqa: BLE001
        pass
    try:
        detail = pipe.http_get_json(
            f"https://prod6.seace.gob.pe/v1/s8uit-services/buscadorpublico/contrataciones/listar-completo?id_contrato={rec['id']}"
        )
        comp = detail.get("uitContratoCompletoProjection", {})
        rec["area"] = pipe.clean(comp.get("nomAreaUsuaria", ""))
        rec["tipo"] = pipe.clean(comp.get("nomTipoInvitacion", ""))
        items = detail.get("uitContratoItemProjectionList", [])
        lugares = []
        for it in items:
            nd = it.get("nomDistritoExt")
            if nd and nd not in lugares:
                lugares.append(nd)
        rec["lugar"] = pipe.clean(" | ".join(lugares))
    except Exception:  # noqa: BLE001
        rec["area"] = rec["tipo"] = rec["lugar"] = ""
    # guarda en la data (sin duplicar)
    scored = json.loads(DATA_PATH.read_text(encoding="utf-8"))
    scored = [x for x in scored if x.get("id") != rec["id"]]
    scored.append(rec)
    scored.sort(key=lambda x: -x.get("score", 0))
    DATA_PATH.write_text(json.dumps(scored, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    build_dashboard()
    return {"ok": True, "proceso": rec["num"], "id": rec["id"], "score": score}


# --------------------------------------------------------------------------
# Pipeline de refresco
# --------------------------------------------------------------------------
def fetch_pdf(archivo_id):
    url = SEACE_DOWNLOAD.format(archivo_id=archivo_id)
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=45) as resp:
        return resp.read()


def run_pipeline():
    with _refresh_lock:
        _refresh_state.update(
            running=True, done=False, error=None,
            started=datetime.now(timezone.utc).isoformat(),
        )
    try:
        subprocess.run(
            [sys.executable, str(HERE / "seace_pipeline.py")],
            cwd=str(HERE), check=True, capture_output=True, timeout=600,
        )
        with _refresh_lock:
            _refresh_state.update(running=False, done=True, error=None)
    except subprocess.CalledProcessError as exc:
        with _refresh_lock:
            _refresh_state.update(
                running=False, done=True,
                error=(exc.stderr or exc.stdout or str(exc))[-2000:],
            )
    except Exception as exc:  # noqa: BLE001
        with _refresh_lock:
            _refresh_state.update(
                running=False, done=True, error=str(exc)[-2000:]
            )


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(HERE), **kwargs)

    def log_message(self, fmt, *args):
        pass

    def _serve_bytes(self, body, content_type, disposition=None):
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        if disposition:
            self.send_header("Content-Disposition", disposition)
        self.end_headers()
        self.wfile.write(body)

    def _serve_json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        qs = urllib.parse.parse_qs(self.path.split("?", 1)[1]) if "?" in self.path else {}
        m = re.fullmatch(r"/pdf/(\d+)", path)
        if m:
            try:
                body = fetch_pdf(m.group(1))
            except Exception as exc:  # noqa: BLE001
                self.send_error(502, f"No se pudo obtener el PDF: {exc}")
                return
            self._serve_bytes(
                body, "application/pdf",
                'inline; filename="requerimiento.pdf"',
            )
            return
        m = re.fullmatch(r"/formato/([a-z_]+)\.html", path)
        if m:
            tipo = m.group(1)
            if tipo not in FORMATOS:
                self.send_error(404, "Formato desconocido")
                return
            html = make_format_html(tipo, load_config())
            self._serve_bytes(
                html.encode("utf-8"),
                "text/html; charset=utf-8",
                'inline; filename="formato.html"',
            )
            return
        m = re.fullmatch(r"/formato/([a-z_]+)\.docx", path)
        if m:
            tipo = m.group(1)
            if tipo not in FORMATOS:
                self.send_error(404, "Formato desconocido")
                return
            cfg = load_config()
            data = FORMATOS[tipo]
            parrafos = [p for p in data["parrafos"]]
            body = make_docx(data["titulo"], parrafos)
            self._serve_bytes(
                body,
                "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                f'attachment; filename="{tipo}.docx"',
            )
            return
        if path == "/docs":
            files = []
            if MIS_DOCS_DIR.exists():
                for f in sorted(MIS_DOCS_DIR.iterdir()):
                    if f.is_file():
                        files.append({"name": f.name, "size": f.stat().st_size})
            self._serve_json({"ok": True, "files": files})
            return
        m = re.fullmatch(r"/doc/([^/]+)", path)
        if m:
            name = urllib.parse.unquote(m.group(1))
            if ".." in name or "/" in name:
                self.send_error(400, "Nombre de archivo inválido")
                return
            fp = MIS_DOCS_DIR / name
            if not fp.is_file():
                self.send_error(404, "Archivo no encontrado en mis_documentos/")
                return
            ctype = (
                "application/pdf"
                if name.lower().endswith(".pdf")
                else "application/octet-stream"
            )
            self._serve_bytes(
                fp.read_bytes(),
                ctype,
                f'inline; filename="{name}"',
            )
            return
        m = re.fullmatch(r"/analizar/(\d+)", path)
        if m:
            cid = int(m.group(1))
            rec = None
            try:
                data = json.loads(DATA_PATH.read_text(encoding="utf-8"))
                rec = next((x for x in data if x.get("id") == cid), None)
            except Exception:  # noqa: BLE001
                pass
            if not rec or not rec.get("archivoId"):
                self._serve_json({"ok": False, "error": "Proceso sin archivo de requerimiento"}, 404)
                return
            try:
                pdf_bytes = fetch_pdf(rec["archivoId"])
            except Exception as exc:  # noqa: BLE001
                self._serve_json({"ok": False, "error": f"No se pudo descargar el PDF: {exc}"}, 502)
                return
            text = ocr.extract_pdf_text(pdf_bytes)
            if not text.strip():
                self._serve_json({"ok": False, "error": "No se pudo extraer texto del PDF (OCR no disponible)."}, 502)
                return
            anal = ocr.detect_requisitos(text)
            anal["entidad"] = rec.get("ent", "")
            existing = {}
            if ANALISIS_PATH.exists():
                existing = json.loads(ANALISIS_PATH.read_text(encoding="utf-8"))
            if str(cid) in existing:
                merged = merge_analisis(existing[str(cid)], anal)
                existing[str(cid)] = merged
                try:
                    ANALISIS_PATH.write_text(
                        json.dumps(existing, ensure_ascii=False, indent=2), encoding="utf-8"
                    )
                    build_dashboard()
                except Exception as exc:  # noqa: BLE001
                    self._serve_json({"ok": False, "error": f"Error al guardar el análisis: {exc}"}, 500)
                    return
                self._serve_json({"ok": True, "id": cid, "analisis": merged, "complemented": True})
                return
            try:
                existing[str(cid)] = anal
                ANALISIS_PATH.write_text(
                    json.dumps(existing, ensure_ascii=False, indent=2), encoding="utf-8"
                )
                build_dashboard()
            except Exception as exc:  # noqa: BLE001
                self._serve_json({"ok": False, "error": f"Error al guardar el análisis: {exc}"}, 500)
                return
            self._serve_json({"ok": True, "id": cid, "analisis": anal})
            return
        if path == "/import":
            num = (qs.get("num") or [""])[0]
            res = import_process(num)
            self._serve_json(res, 200 if res.get("ok") else 404)
            return
        if path == "/items":
            cid = (qs.get("id") or [""])[0]
            if not cid.isdigit():
                self._serve_json({"ok": False, "error": "id inválido"}, 400)
                return
            try:
                detail = pipe.http_get_json(
                    f"https://prod6.seace.gob.pe/v1/s8uit-services/buscadorpublico/contrataciones/listar-completo?id_contrato={cid}"
                )
                items = []
                for it in detail.get("uitContratoItemProjectionList", []):
                    items.append({
                        "desc": pipe.clean(it.get("nomCubso", "")),
                        "um": it.get("nomUnidadMedida", ""),
                        "cant": it.get("cantidad"),
                        "moneda": it.get("nomMoneda", "SOLES"),
                        "lugar": it.get("nomDistritoExt", ""),
                    })
                self._serve_json({"ok": True, "items": items})
            except Exception as exc:  # noqa: BLE001
                self._serve_json({"ok": False, "error": str(exc)}, 502)
            return
        if path == "/config":
            self._serve_json(load_config())
            return
        if path == "/refresh/status":
            self._serve_json(_refresh_state)
            return
        if path == "/":
            if OUT_PATH.exists():
                self._serve_bytes(OUT_PATH.read_bytes(), "text/html; charset=utf-8")
            else:
                self.send_error(404, "Corre primero: python seace_pipeline.py")
            return
        super().do_GET()

    def do_POST(self):
        path = self.path.split("?", 1)[0]
        if path == "/config":
            length = int(self.headers.get("Content-Length", 0))
            try:
                data = json.loads(self.rfile.read(length).decode("utf-8"))
                save_config(data)
                self._serve_json({"ok": True, "config": load_config()})
            except Exception as exc:  # noqa: BLE001
                self._serve_json({"ok": False, "error": str(exc)}, 400)
            return
        if path == "/refresh":
            with _refresh_lock:
                already = _refresh_state["running"]
            if already:
                self._serve_json({"started": False, "running": True})
                return
            threading.Thread(target=run_pipeline, daemon=True).start()
            self._serve_json({"started": True, "running": True})
            return
        self.send_error(404)


def main():
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8000
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    url = f"http://localhost:{port}"
    print(f"OECE Finder sirviendo en {url}")
    print("Presiona Ctrl+C para detener.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nDetenido.")
        server.server_close()


if __name__ == "__main__":
    main()