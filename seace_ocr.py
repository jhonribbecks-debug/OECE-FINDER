#!/usr/bin/env python3
"""
seace_ocr.py -- extraccion de texto y deteccion heuristica de requisitos.

Uso desde seace_server.py:
    text = extract_pdf_text(pdf_bytes)      # texto nativo o OCR si es escaneado
    anal = analyze_text(text)               # dict con estructura de seace_analisis.json

Perfil de referencia (Jhon Brian Ribbeck Soto):
    - Ing. Civil, CIP 289452, titulo 2022, colegiado set. 2022 (~4 anios)
    - RNP: Consultor de Obras + Bienes y Servicios (no ejecutor)
    - Experiencia: consorcios (DIMSA, Integral Sierra y Selva), PRONIED,
      Residente de Obras (Monobamba, Manitea), Especialista en Estructuras, BIM
    - Sin equipos propios: GPS, taladro diamantina, escaner de acero, camioneta
"""
import os
import re
from pathlib import Path

TESSERACT_CMD = r"C:\Program Files\Tesseract-OCR\tesseract.exe"
TESSDATA = Path(r"C:\Users\aintc\AppData\Local\Temp\opencode\tessdata")

MIN_NATIVE_CHARS = 1200  # si el texto nativo es menor, se asume PDF escaneado -> OCR


def extract_pdf_text(pdf_bytes):
    """Devuelve el texto del PDF. Usa el texto nativo si hay; si no, OCR por pagina."""
    native = ""
    try:
        import fitz  # PyMuPDF
        doc = fitz.open(stream=pdf_bytes, filetype="pdf")
        native = "\n".join(page.get_text() for page in doc)
        doc.close()
    except Exception:
        native = ""
    if len(native.strip()) >= MIN_NATIVE_CHARS:
        return native
    ocr = _ocr_pdf(pdf_bytes)
    if len(ocr.strip()) > len(native.strip()):
        return ocr
    return native or ocr


def _ocr_pdf(pdf_bytes):
    import io

    import fitz
    from PIL import Image

    os.environ.setdefault("TESSDATA_PREFIX", str(TESSDATA))
    try:
        import pytesseract
        pytesseract.pytesseract.tesseract_cmd = TESSERACT_CMD
    except Exception:
        return ""
    try:
        doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    except Exception:
        return ""
    parts = []
    try:
        for page in doc:
            pix = page.get_pixmap(matrix=fitz.Matrix(2.2, 2.2))
            img = Image.open(io.BytesIO(pix.tobytes("png"))).convert("L")
            try:
                parts.append(pytesseract.image_to_string(img, lang="spa", config="--psm 6"))
            except Exception:
                continue
    finally:
        doc.close()
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Heuristicas de deteccion
# ---------------------------------------------------------------------------

def _n(s):
    return (s or "").lower()


def _has(t, *kws):
    tl = _n(t)
    return any(kw.lower() in tl for kw in kws)


def detect_requisitos(text):
    t = text or ""
    tl = _n(t)
    docs = []
    add = lambda item, estado, nota: docs.append({"item": item, "estado": estado, "nota": nota})

    # --- Perfil del proveedor ---
    if _has(t, "persona natural") or _has(t, "persona juridica"):
        add("Persona natural o jurídica", "ok", "Aplicas como persona natural.")
    if _has(t, "registro nacional de proveedores", "rnp") or _has(t, "rn p"):
        add("RNP vigente", "ok", "Tu RNP incluye \"Consultor de Obras\" y \"Bienes y servicios\".")
    if _has(t, "ficha ruc", "ruc", "habido"):
        add("Ficha RUC habido y habilitado", "verificar", "Confirmar estado actual en SUNAT antes de postular.")
    if _has(t, "redam", "deudores alimentarios"):
        add("No REDAM / no impedido ni inhabilitado", "verificar", "Verificación estándar, normalmente no es un problema.")

    # --- Titulo y colegiatura ---
    if _has(t, "titulo profesional", "ingeniero civil", "ing. civil", "ingenieria civil"):
        add("Título profesional de Ingeniero Civil", "ok", "CIP 289452, título Universidad Continental (2022).")
    elif _has(t, "titulo profesional", "titulado"):
        add("Título profesional requerido", "verificar", "El TDR pide título profesional; revisa si admite otra carrera afín.")
    if _has(t, "colegiad", "habilitad"):
        add("Colegiatura y habilitación vigente", "ok", "Colegiado desde set. 2022, habilitado al día.")

    # --- Experiencia general (anios desde colegiatura) ---
    m = re.search(r"([0-9]{1,2})\s*a[nñ]os\s*de experiencia\s*en\s*general", tl)
    if not m:
        m = re.search(r"experiencia\s*en\s*general[^\d]{0,40}([0-9]{1,2})\s*a[nñ]os", tl)
    if m:
        yrs = int(m.group(1))
        if yrs <= 4:
            add(f"Experiencia general ≥ {yrs} años desde colegiatura", "ok",
                "Colegiado desde set. 2022 (~4 años).")
        else:
            add(f"Experiencia general ≥ {yrs} años desde colegiatura", "riesgo",
                f"Colegiado desde set. 2022 (~4 años): el TDR pide {yrs}. Verifica el cómputo según la fecha de ofertas.")
    else:
        m = re.search(r"([0-9]{1,2})\s*a[nñ]os", tl)
        if m and int(m.group(1)) >= 4:
            add("Experiencia general (años)", "verificar", f"Se mencionan ~{m.group(1)} años; confirma el cómputo desde colegiatura.")

    # --- Experiencia especifica / especialidad ---
    if _has(t, "experiencia especifica", "experiencia en la especialidad", "especialidad"):
        add("Experiencia específica en la especialidad", "verificar",
            "Acreditable con consorcios (DIMSA, Integral Sierra y Selva), PRONIED y residencias; confirma contratos y conformidades.")
    if _has(t, "monto facturado", "facturado", "s/ 60,000", "s/ 20,000", "soles"):
        add("Monto facturado acumulado en la especialidad", "verificar",
            "Revisa tus comprobantes/conformidades para sumar el monto exigido en el rubro exacto.")

    # --- Personal clave ---
    if _has(t, "personal clave", "jefe de supervisi", "residente de obra", "supervisor de obra"):
        add("Personal clave (cargo/profesión/experiencia)", "verificar",
            "Revisa el cargo, profesión y años exigidos; tu rol como Residente/Especialista en Estructuras puede servir según el puesto.")

    # --- Equipos exigidos ---
    equipos = [
        ("GPS", "GPS", "No figura en tu CV; necesitarías alquilarlo o conseguir uno reciente."),
        ("taladro diamantina", "diamantina", "Equipo especializado de ensayo — normalmente se subcontrata a un laboratorio."),
        ("escáner de acero", "escaner de acero", "Mismo caso — alquiler o alianza con laboratorio de ensayo de materiales."),
        ("camioneta pick up", "camioneta", "No figura en tu CV como recurso propio."),
        ("estación total", "estacion total", "Equipo topográfico que no figura en tu perfil."),
        ("nivel topográfico", "nivel topografico", "Equipo topográfico que no figura en tu perfil."),
    ]
    for label, kw, nota in equipos:
        if kw.lower() in tl:
            add(f"Equipo: {label}", "falta", nota)

    # --- Seguros / otros ---
    if _has(t, "sctr", "seguro complementario de trabajo de riesgo"):
        add("Seguro Complementario de Trabajo de Riesgo (SCTR)", "pendiente",
            "Se gestiona tras la firma del contrato/orden de servicio; no es previo a la oferta, pero su omisión acarrea penalidades.")
    if _has(t, "garantía", "garantia de fiel cumplimiento"):
        add("Garantía de fiel cumplimiento", "pendiente",
            "Suele no exigirse en montos ≤ 50 UIT; confirma según el monto del proceso.")

    # --- Monto y plazo ---
    monto = None
    m = re.search(r"(cuant[ií]a|monto total|presupuesto|suma de)[^\d]{0,90}(s/\.?)?\s*([\d][\d.,\s]*)", tl)
    if m and m.group(3):
        raw = m.group(3).strip()
        if raw and any(c.isdigit() for c in raw):
            monto = "S/ " + raw.replace(" ", "").rstrip(". ")
    plazo = None
    m = re.search(r"(?:plazo estimado|plazo de ejecuci[oó]n|durante el plazo|el plazo de la prestaci[oó]n)[^\d]{0,70}([0-9]{1,3})\)?\s*d[ií]as", tl)
    if not m:
        m = re.search(r"([0-9]{1,3})\)?\s*d[ií]as\s*calendario", tl)
    if m:
        plazo = f"{m.group(1)} días calendario"

    # --- Resumen ---
    titulo = ""
    m = re.search(r"denominaci[oó]n de la contrataci[oó]n[:\s]*", tl)
    if m:
        seg = tl[m.end():m.end() + 220]
        seg = re.split(r"[\n•]|cl[aá]usula|finalidad|objetivo", seg)[0].strip()
        titulo = seg[:200].strip(" .")
    if not titulo:
        m = re.search(r"(mejoramiento|construcci[oó]n|rehabilitaci[oó]n|servicio de|evaluaci[oó]n|mantenimiento)[^\n]{0,160}", tl)
        if m:
            titulo = m.group(0).strip()
    resumen = (f"Requisitos detectados automáticamente por OCR del PDF de requerimiento. {titulo}."
               if titulo else
               "Requisitos detectados automáticamente por OCR del PDF de requerimiento. Revisa el PDF original para confirmar.")

    alertas = [
        "Este análisis fue generado automáticamente por OCR y heurísticas; puede omitir requisitos o tener errores de lectura. Verifica siempre el documento original antes de presentar tu oferta."
    ]
    recomendaciones = [
        "Revisa el PDF original para confirmar cada requisito detectado.",
        "Los ítems marcados 'verificar' requieren documentación previa: junta contratos, conformidades y comprobantes antes de postular.",
        "Los ítems 'falta' (equipos) resuélvelos con alquiler o alianza con laboratorio/proveedor antes de la oferta."
    ]

    if not docs:
        docs.append({"item": "No se detectaron requisitos en el texto extraído", "estado": "verificar",
                     "nota": "El PDF pudo ser escaneado con OCR deficiente. Revisa el documento original o pide un análisis manual."})

    return {
        "resumen": resumen,
        "entidad": "",
        "monto": monto,
        "plazo": plazo,
        "docs": docs,
        "alertas": alertas,
        "recomendaciones": recomendaciones,
        "origen": "ocr-auto",
    }