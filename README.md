# OECE Finder — Radar de Consultorías SEACE

Pipeline que consulta el buscador público de contrataciones menores del SEACE
(prod6.seace.gob.pe), filtra los procesos de objeto "Consultoría de Obra" y
"Servicio" en estado Vigente, los puntúa contra el perfil profesional de
Jhon Brian Ribbeck Soto (Ingeniero Civil Estructural/BIM, RNP Consultor de
Obras + Bienes y Servicios), y genera un dashboard HTML autocontenido.

## Archivos

- `seace_pipeline.py` — script principal (solo librería estándar de Python).
  Descarga los procesos vigentes, calcula el puntaje de afinidad, obtiene el
  archivo de requerimiento y la ubicación de cada coincidencia, y arma
  `seace_dashboard.html` a partir de la plantilla.
- `seace_template.html` — plantilla del dashboard (HTML/CSS/JS), con
  marcadores `__DATA_JSON__`, `__ANALISIS_JSON__` y `__GENERATED_AT__`.
- `seace_analisis.json` — checklist de requisitos ya analizados manualmente
  para procesos puntuales (comparados contra el CV), indexado por
  `idContrato`. Se actualiza a mano cuando se revisa un nuevo proceso a
  fondo.
- `seace_dashboard_data.json` / `seace_dashboard.html` — salida generada por
  el pipeline (no editar a mano).

## Uso

```bash
python3 seace_pipeline.py
```

Genera `seace_dashboard.html`, listo para publicarse como Artifact.
