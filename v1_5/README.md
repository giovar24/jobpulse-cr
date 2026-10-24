# JobPulse CR 1.5 (en desarrollo)

Esta carpeta inicia el desarrollo aislado de la version 1.5. La aplicacion
actual, sus datos y `data/jobs.db` no se modifican ni se migran.

## Alcance de esta fase

- Paquete Python independiente en `src/jobpulse_v15`.
- Persistencia SQLite independiente en `data/jobpulse_v15.db`.
- Migraciones SQL versionadas.
- Pruebas con bases temporales; no requieren red, perfil ni credenciales.

La base de v1.5 se crea al inicializar `Database` por primera vez. Las
migraciones solo modifican la ruta de base configurada para esa instancia.

## Preparar y ejecutar

Crear un entorno virtual independiente e instalar el paquete:

```powershell
cd v1_5
py -3.11 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e .
```

Inicializar la DB exclusiva de v1.5:

```powershell
.\.venv\Scripts\python.exe -m jobpulse_v15 init-db
```

La DB se crea en `v1_5/data/jobpulse_v15.db`. Para probar otro archivo, se
puede indicar `--db C:\ruta\de\prueba.db` antes del comando.

Para respaldar la base activa sin detener la aplicación:

```powershell
.\.venv\Scripts\python.exe -m jobpulse_v15 backup `
  --output .\backups\jobpulse-v15-2026-10-23.db
```

El respaldo usa la API de copia de SQLite, se valida con `integrity_check` y
no sobrescribe archivos existentes ni la base activa. La UI ofrece la misma
accion con `Respaldar DB`; restaurar consiste en cerrar JobPulse, conservar
una copia de la base actual y reemplazar el archivo configurado por un respaldo
verificado.
Para diagnosticar una base despues de una restauracion o copia manual:

```powershell
.\.venv\Scripts\python.exe -m jobpulse_v15 db check
```

El comando es de solo lectura, ejecuta `integrity_check` y `foreign_key_check`,
devuelve JSON y usa codigo de salida distinto de cero ante problemas.

## Interfaz de escritorio

La interfaz Tkinter muestra las vacantes guardadas, permite filtrarlas por
texto, ubicacion, modalidad e idioma, y revisar la evidencia del analisis.
El boton `Calidad de datos` muestra un resumen global de vacantes que requieren
revision, faltantes de empresa/pais/ubicacion, estado de analisis y campos
desconocidos o ambiguos, ademas de grupos candidatos a duplicados. El conteo
cubre toda la base, no solo la pagina visible de resultados. `Revisar
duplicados` muestra los primeros grupos candidatos e identificadores para
validacion manual.
Tambien permite ejecutar el extractor local sobre la vacante seleccionada o,
con `Analizar pendientes`, procesar tras confirmacion las vacantes visibles que
aun no tienen analisis. El procesamiento es local, no vuelve a analizar
registros ya analizados y muestra los errores por vacante. Permite abrir el
enlace de origen y registrar el estado manual de la vacante
(`reviewed`, `applied`, `archived` o `dismissed`), guardar notas y excluir
vacantes de la busqueda normal; se pueden volver a mostrar con el filtro
correspondiente. Esta informacion manual se conserva al actualizar la oferta.
La lista puede ordenarse por actividad reciente o por fecha de publicacion;
en este ultimo caso las ofertas sin fecha aparecen al final.
Los campos `Publicada desde` y `Publicada hasta` filtran por fechas inclusivas
en formato `AAAA-MM-DD`; se pueden dejar vacios para no limitar el rango.
Los campos `Tipo de empleo` y `Horario` aceptan texto tal como aparece en los
datos guardados (por ejemplo, `Full-time` o `Night shift`) y filtran por
coincidencia exacta; dejarlos vacios no aplica esos filtros.
El rango salarial compara ofertas solo con la misma moneda y periodo. Para
usar `Salario desde` o `Salario hasta` se requieren ambos valores de unidad;
una oferta coincide cuando su rango conocido se superpone con el solicitado.
Un extremo salarial ausente se interpreta como abierto, pero se exige al menos
un extremo conocido. No se convierten monedas ni periodos. `Moneda` y `Periodo
salarial` tambien se pueden usar por separado para filtrar por coincidencia
exacta.
`Exportar CSV` guarda los resultados de la ultima busqueda con sus filtros
actuales, incluidos los codigos de motivos de revision. Exporta los campos
normalizados basicos, fechas, campos salariales estructurados cuando estan
disponibles, compatibilidad del perfil y un enlace a una fuente; no incluye
descripcion, requisitos originales ni payloads de la fuente.
Neutraliza celdas cuyo contenido podria interpretarse como formula al abrirlo
en una hoja de calculo. Se limita a los resultados cargados (segun `Max.
resultados`) y visibles despues del filtro de compatibilidad.
`Editar datos` permite corregir titulo, empresa, pais, provincia/region, ciudad
y modalidad normalizados. Las correcciones prevalecen sobre futuras
actualizaciones de la fuente; `Restaurar datos de fuente` las elimina y recupera
los valores fuente mas recientes. El campo `location_raw` y el payload original
no se editan. Los cambios se guardan en la base v1.5 y no alteran la aplicacion
ni la base legacy.
El boton `Seguimiento` abre el historial opcional de postulacion: permite
anotar cambios de estado, fecha de aplicacion, proxima accion y notas. Cada
registro es un evento separado y sobrevive a la purga del contenido de la
vacante.
El boton `Mi perfil` permite registrar manualmente datos estructurados tomados
del CV o importar un PDF, DOCX o TXT local; `Comparar perfil` contrasta los
datos aprobados con los requisitos explicitos del analisis vigente.
La importacion solo lee el archivo seleccionado, trabaja localmente y no
conserva el texto extraido. Muestra sugerencias y fragmentos detectados para
revision; solo las filas seleccionadas se guardan. Reemplazar un dato existente
requiere confirmacion.

**Limites de importacion del CV:** PDF hasta **15 MB y 100 paginas**; DOCX y
TXT hasta **15 MB**; TXT codificado en UTF-8. No se ejecuta OCR en PDF
escaneados y los documentos protegidos con contrasena no se admiten.

Los datos muestran su origen (manual o archivo importado). `Revertir ultima
importacion` deshace la importacion mas reciente: elimina datos nuevos y
restaura valores previos que fueron reemplazados. Si un dato importado se
edito manualmente despues, ese cambio manual se conserva. El historial guarda
solo el nombre del archivo y fecha, nunca la ruta ni el contenido del CV.
La comparacion usa categoria y codigo exactos (o texto exacto si el requisito
no trae codigo), valida el nivel cuando ambos datos lo especifican, el estado
de estudios concluidos y el minimo numerico de experiencia. Los requisitos
inferidos o ambiguos requieren revision. Un dato
ausente del perfil se muestra como `No registrado`; no significa que la persona
no tenga esa competencia ni descarta automaticamente la vacante. Los codigos
del perfil deben coincidir con los del requisito (por ejemplo `en`, `bachelor`,
`years_experience`, `python` o `sql`). El extractor local identifica idioma,
educacion, licencia, experiencia y un conjunto inicial de competencias
tecnicas comunes (Python, SQL, Excel, Power BI, Tableau, AWS, Azure, Google
Cloud, Java, JavaScript, TypeScript, C++, C#, Linux, Git, AutoCAD, SolidWorks,
MATLAB, SAP, PLC y Six Sigma) solo cuando aparecen en contexto de habilidades,
herramientas, experiencia o con una señal explicita de requisito/preferencia.
La importacion de CV reutiliza esas reglas como sugerencias, no como afirmaciones
verificadas: revisa y, si es necesario, corrige categoria, codigo y dato usando
`Editar sugerencia`; el fragmento original se mantiene como referencia antes de
guardar.
Las competencias fuera de ese vocabulario se marcan como `unknown` si el texto
indica una seccion/contexto de habilidades, y requieren revision manual; una
mencion casual por si sola no se trata como requisito. La tabla de resultados
muestra el estado del perfil y el filtro
`Compatibilidad` permite ver coincidencias de requisitos obligatorios, datos
no registrados, brechas conocidas, revision manual, vacantes sin analisis o
sin requisitos obligatorios. Un analisis parcial se clasifica para revision,
no como incompatibilidad. El filtro se aplica a los resultados cargados hasta
el maximo indicado; aumenta `Max. resultados` para revisar un conjunto mayor.
La nueva version del extractor no reanaliza historicos automaticamente; usa
`Analizar seleccionada` para actualizar el analisis de una vacante ya guardada.
La interfaz solo consulta la DB v1.5 y no inicia scraping automaticamente.
La administracion de fuentes permite activar o
pausar sitios, agregar tableros Workday, Greenhouse, Ashby, Lever y
Recruitee, y consultar manualmente un tablero tras confirmacion. El boton
`Historial` muestra las ultimas corridas de la fuente seleccionada, con su
estado, conteos y errores:

```powershell
.\.venv\Scripts\python.exe -m pip install -e .
.\.venv\Scripts\python.exe -m jobpulse_v15.ui
```

Tambien se instala el lanzador `jobpulse-v15-ui`.

## Ingesta controlada de ofertas

El comando `ingest import-json` importa resultados ya obtenidos desde un
archivo JSON local, sin hacer solicitudes web. Cada importacion crea una
corrida acotada al sitio elegido, guarda la oferta y su identidad de origen,
detecta reimportaciones y registra los contadores de salud del sitio. El sitio
debe estar habilitado.

Formato: una lista de objetos, o un objeto con la propiedad `offers`. Cada
registro requiere `source_job_id` y `job.title`; se puede indicar un `job_id`
global opcional. Si se omite, se genera como `<site_id>:<source_job_id>`.
`job` acepta los campos normalizados del esquema, como `country`, `region`,
`city`, `work_mode`, `posting_language`, `description_raw` y
`requirements_raw`.

```json
{
  "offers": [
    {
      "source_job_id": "REQ-1001",
      "source_url": "https://careers.example.test/jobs/REQ-1001",
      "job": {
        "title": "Software Engineer",
        "company": "Example",
        "country": "CR",
        "city": "San Jose",
        "work_mode": "hybrid",
        "posting_language": "en",
        "description_raw": "Fluent English required."
      },
      "raw_payload": {
        "source_id": "REQ-1001"
      }
    }
  ]
}
```

Importar y luego analizar una oferta:

```powershell
.\.venv\Scripts\python.exe -m jobpulse_v15 ingest import-json `
  --site intel-cr --input .\offers.json
.\.venv\Scripts\python.exe -m jobpulse_v15 analysis extract --job intel-cr:REQ-1001
```

La importacion es idempotente por `(site_id, source_job_id)`: una oferta ya
conocida actualiza sus datos, pero se cuenta como duplicada en la corrida.
Una lista invalida se rechaza antes de crearla; si falla la escritura durante
la corrida, el error queda registrado para revision y la CLI informa el fallo.

### Consulta explicita de tableros Workday

v1.5 incluye un adaptador independiente para consultar tableros Workday. Usa
la URL del sitio para identificar tenant y tablero; por ejemplo:

```powershell
.\.venv\Scripts\python.exe -m jobpulse_v15 platform add `
  --id workday --name Workday --provider-type api --default-language en
.\.venv\Scripts\python.exe -m jobpulse_v15 site add `
  --id intel-cr --platform workday --name "Intel Careers CR" `
  --url https://intel.wd1.myworkdayjobs.com/External `
  --company Intel --country CR
.\.venv\Scripts\python.exe -m jobpulse_v15 scrape workday `
  --site intel-cr --pages 1
.\.venv\Scripts\python.exe -m jobpulse_v15 scrape workday `
  --group directos --pages 1
```

La consulta web solo ocurre al ejecutar `scrape workday`; no se inicia al
abrir la UI ni al importar el paquete. Se puede indicar un sitio (`--site`)
o un grupo (`--group`), nunca ambos. El grupo procesa en secuencia sus sitios
Workday habilitados; cada sitio mantiene su propia corrida y un fallo se
registra sin impedir los siguientes. El resultado JSON separa importaciones y
fallos, y la CLI retorna codigo distinto de cero si hubo fallos. El comando
admite de 1 a 10 paginas
(20 anuncios por pagina), utiliza `Costa Rica` como busqueda por defecto,
consulta detalles con una pausa entre vacantes y registra resultados/error
en una corrida del sitio. Un fallo HTTP o una respuesta inesperada se informa
como error y no se registra como cero ofertas. No hay reintentos automaticos.
Los registros se normalizan y pasan por `IngestionService`; el proveedor no
accede a la DB antigua. En la ventana `Administrar fuentes`, selecciona uno o
varios tableros activos y usa `Consultar seleccionadas` para ejecutar la misma
consulta secuencial con confirmacion y resumen de resultados/errores.

### Consulta explicita de tableros Greenhouse

Greenhouse expone un API publico por tablero. Registra la plataforma y un
tablero con URL `https://job-boards.greenhouse.io/<token>` o
`https://boards.greenhouse.io/<token>`:

```powershell
.\.venv\Scripts\python.exe -m jobpulse_v15 platform add `
  --id greenhouse --name Greenhouse --provider-type api
.\.venv\Scripts\python.exe -m jobpulse_v15 site add `
  --id acme-cr --platform greenhouse --name "Acme Careers CR" `
  --url https://job-boards.greenhouse.io/acme --company Acme `
  --country CR --group directos
.\.venv\Scripts\python.exe -m jobpulse_v15 scrape greenhouse --site acme-cr
.\.venv\Scripts\python.exe -m jobpulse_v15 scrape greenhouse --group directos
```

La consulta es siempre explicita. Cada tablero usa una peticion al endpoint
publico de Greenhouse con descripciones; solo se importan vacantes cuya
ubicacion o alguna oficina contiene el texto `country_query` (por defecto
`Costa Rica`). La URL del tablero se valida contra los dominios oficiales antes
de construir la URL fija de API. Cada fuente tiene su propia corrida; un fallo
se registra y no detiene las demas. La UI permite agregar tableros Greenhouse
y consultarlos individualmente o en seleccion multiple, siempre tras confirmar.

### Consulta explicita de tableros Ashby

Ashby ofrece una API publica por tablero. Registra la plataforma y el tablero
con una URL HTTPS de `jobs.ashbyhq.com`:

```powershell
.\.venv\Scripts\python.exe -m jobpulse_v15 platform add `
  --id ashby --name Ashby --provider-type api
.\.venv\Scripts\python.exe -m jobpulse_v15 site add `
  --id acme-cr --platform ashby --name "Acme Careers CR" `
  --url https://jobs.ashbyhq.com/acme --company Acme `
  --country CR --group directos
.\.venv\Scripts\python.exe -m jobpulse_v15 scrape ashby --site acme-cr
.\.venv\Scripts\python.exe -m jobpulse_v15 scrape ashby --group directos
```

El adaptador consulta el endpoint publico del tablero solo bajo demanda,
conserva el payload original y filtra por `country_query` (por defecto
`Costa Rica`) en el titulo, ubicacion principal o ubicaciones secundarias.
Cuando una oferta coincidente solo indica el pais en una ubicacion secundaria
o en el titulo, guarda `<country_query> (multi-loc)` como ubicacion sin inferir
ciudad o provincia. La modalidad se mapea solo si Ashby especifica modalidad
remota, hibrida o presencial; de lo contrario queda `unknown`. La UI permite
agregar tableros Ashby y consultarlos individualmente o en seleccion multiple,
siempre tras confirmar. Las pruebas usan respuestas simuladas y no consultan
tableros reales.

### Consulta explicita de tableros Lever

Lever expone un API publico por tablero. Registra la plataforma y el tablero
con una URL HTTPS de `jobs.lever.co`:

```powershell
.\.venv\Scripts\python.exe -m jobpulse_v15 platform add `
  --id lever --name Lever --provider-type api
.\.venv\Scripts\python.exe -m jobpulse_v15 site add `
  --id acme-cr --platform lever --name "Acme Careers CR" `
  --url https://jobs.lever.co/acme --company Acme `
  --country CR --group directos
.\.venv\Scripts\python.exe -m jobpulse_v15 scrape lever --site acme-cr
.\.venv\Scripts\python.exe -m jobpulse_v15 scrape lever --group directos
```

El adaptador consulta primero el endpoint europeo de Lever y, si falla,
reintenta con el global. Conserva el payload original, filtra por
`country_query` (por defecto `Costa Rica`) en el titulo, pais o listas del
anuncio y completa la descripcion con las secciones adicionales del anuncio.
Cuando una oferta coincidente no indica el pais en su ubicacion principal,
guarda `<country_query> (multi-loc)` sin inferir ciudad o provincia; la
modalidad no se deduce y queda `unknown` porque Lever no la expone. La UI
permite agregar tableros Lever y consultarlos individualmente o en seleccion
multiple, siempre tras confirmar. Las pruebas usan respuestas simuladas y no
consultan tableros reales.

### Consulta explicita de tableros Recruitee

Recruitee expone un API publico por tablero. Registra la plataforma y el
tablero con una URL HTTPS cuyo subdominio identifica la empresa:

```powershell
.\.venv\Scripts\python.exe -m jobpulse_v15 platform add `
  --id recruitee --name Recruitee --provider-type api
.\.venv\Scripts\python.exe -m jobpulse_v15 site add `
  --id acme-cr --platform recruitee --name "Acme Careers CR" `
  --url https://acme.recruitee.com --company Acme `
  --country CR --group directos
.\.venv\Scripts\python.exe -m jobpulse_v15 scrape recruitee --site acme-cr
.\.venv\Scripts\python.exe -m jobpulse_v15 scrape recruitee --group directos
```

El adaptador consulta `https://<token>.recruitee.com/api/offers/` solo bajo
demanda, conserva el payload original y filtra por `country_query` (por
defecto `Costa Rica`) en el titulo, la ubicacion principal o las ubicaciones
secundarias. Completa la descripcion con la seccion de requisitos del anuncio.
Cuando una oferta coincidente no indica el pais en su ubicacion principal,
guarda `<country_query> (multi-loc)` sin inferir ciudad o provincia; la
modalidad no se deduce y queda `unknown`. La UI permite agregar tableros
Recruitee y consultarlos individualmente o en seleccion multiple, siempre tras
confirmar. Las pruebas usan respuestas simuladas y no consultan tableros
reales.

Workday devuelve una ubicacion original que v1.5 siempre conserva en
`location_raw` y en el payload de origen. Para Costa Rica, el adaptador separa
ciudad y provincia cuando recibe `Ciudad, Provincia, Costa Rica`, o reconoce
una provincia cuando recibe `Provincia, Costa Rica`. Una ubicacion con una
estructura no reconocida permanece sin ciudad/provincia; no se adivina. La
modalidad no se deduce y queda `unknown` hasta que la fuente la especifique.
El adaptador usa solo la biblioteca estandar de Python y sus pruebas simulan
las respuestas; no hacen peticiones al portal.

Para normalizar registros Workday que ya estan guardados, se puede revisar
primero el reporte de simulacion. `--site` limita la operacion a un tablero y
`--apply` aplica solo los campos vacios, sin reemplazar valores de ciudad o
provincia existentes:

```powershell
.\.venv\Scripts\python.exe -m jobpulse_v15 location normalize-workday `
  --site intel-cr
.\.venv\Scripts\python.exe -m jobpulse_v15 location normalize-workday `
  --site intel-cr --apply
```

La operacion usa exclusivamente el payload original persistido y no hace
consultas web. Las ubicaciones no reconocidas y registros con fuentes
conflictivas se omiten y aparecen contados en el reporte.

## Ejecutar las pruebas

Desde `v1_5/`:

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

## Gestionar fuentes y vacantes

Las operaciones de persistencia viven en `jobpulse_v15.repository.Repository`.
Desde la CLI se pueden dar de alta plataformas y sitios, filtrar los sitios
habilitados que una corrida futura debe seleccionar y activar/pausar fuentes:

```powershell
.\.venv\Scripts\python.exe -m jobpulse_v15 platform add `
  --id workday --name Workday --provider-type api --default-language en
.\.venv\Scripts\python.exe -m jobpulse_v15 site add `
  --id intel-cr --platform workday --name "Intel Careers CR" `
  --url https://intel.wd1.myworkdayjobs.com/External --company Intel `
  --country CR --group directos
.\.venv\Scripts\python.exe -m jobpulse_v15 site list --enabled-only --group directos
.\.venv\Scripts\python.exe -m jobpulse_v15 site set-enabled --id intel-cr --disabled
```

Los sitios deben apuntar a una plataforma registrada. La configuracion del
sitio es declarativa y se rechazan claves comunes de credenciales.
`select_sites` selecciona sitios activos, y el comando Workday puede recorrer
secuencialmente los tableros del grupo elegido.
`Repository.save_job` guarda la oferta, su registro de fuente y su memoria de
deduplicacion en una sola transaccion.

El registro de corridas distingue resultados vacios de errores. Tres
corridas exitosas seguidas con cero ofertas marcan el sitio para revision
manual, sin desactivarlo:

```powershell
.\.venv\Scripts\python.exe -m jobpulse_v15 run start --kind full --group directos
.\.venv\Scripts\python.exe -m jobpulse_v15 run site-result `
  --id 1 --site intel-cr --found 0 --valid 0 --new 0 --duplicates 0
.\.venv\Scripts\python.exe -m jobpulse_v15 run finish --id 1
.\.venv\Scripts\python.exe -m jobpulse_v15 run show --id 1
.\.venv\Scripts\python.exe -m jobpulse_v15 run list --limit 10
.\.venv\Scripts\python.exe -m jobpulse_v15 site mark-reviewed --id intel-cr
```

En la interfaz, una fuente que requiere revision habilita `Marcar revisada`.
La accion elimina la bandera de revision y conserva el ultimo estado observado
(`zero_results` o `error`); no activa la fuente ni ejecuta una consulta.

`jobs_found` cuenta ofertas recibidas del portal; si todas ya existian, se
registra un resultado exitoso con `--found` mayor que cero y `--new 0`, no
como un sitio sin resultados. Un error se registra con `--error "mensaje"`
y no incrementa la racha de ceros.

## Esquema inicial

- `jobs`: identidad y campos comunes normalizados; conserva los textos
  originales cuando los proporciona la fuente.
- `source_platforms` y `source_sites`: plataformas, tableros y configuracion
  declarativa.
- `job_source_records` y `source_job_memory`: datos originales por fuente y
  memoria minima independiente de la retencion de la oferta.
- `source_runs` y `source_run_sites`: resultados y salud por corrida y sitio.
- `job_analysis`, `job_requirements` y `job_field_status`: extraccion
  versionada, requisitos con evidencia y estado de campos ausentes.
- `user_job_state` y `applications`: decisiones manuales y seguimiento que
  sobreviven a la purga del contenido de una oferta.

Los requisitos son filas relacionadas y repetibles, no columnas especificas
por portal. `posting_language` describe el idioma del anuncio y no implica
que ese idioma sea requisito del puesto. Los estados `not_mentioned`,
`unknown`, `ambiguous` e `inferred` son distintos y no deben tratarse como
hechos equivalentes.

## Analisis estructurado y busqueda

`Repository.save_analysis` guarda una revision de analisis con requisitos
repetibles, evidencia, nivel (`required`, `preferred`, `inferred` o `ambiguous`),
origen, confianza y estados de campos (`found`, `not_mentioned`, `unknown` o
`ambiguous`). Cada revision se asocia a la huella del texto y version del
extractor; una nueva revision pasa a ser la actual sin borrar el historial.
Si se marca una categoria como `found`, debe incluir al menos un requisito con
evidencia. `complete` no puede ocultar campos ambiguos o desconocidos.

```python
repository.save_analysis(
    "job-123",
    text_hash="sha256-of-description",
    extractor_version="rules-1",
    analysis_status="complete",
    source_site_id="intel-cr",
    source_config_hash="sha256-of-site-config",
    field_statuses={
        "language": "found",
        "education": "found",
        "license": "not_mentioned",
    },
    requirements=[
        {
            "category": "language",
            "value_code": "en",
            "value_text": "English",
            "requirement_level": "required",
            "evidence": "Advanced English required.",
            "origin": "rule",
            "confidence": 0.95,
        },
        {
            "category": "education",
            "value_code": "bachelor",
            "value_text": "Bachelor's degree",
            "education_status": "completed",
            "requirement_level": "required",
            "evidence": "Bachelor's degree required.",
            "origin": "rule",
            "confidence": 0.98,
        },
    ],
)

matches = repository.search_jobs(
    requirements=[
        {"category": "language", "value_code": "en"},
        {
            "category": "education",
            "value_code": "bachelor",
            "education_status": "completed",
        },
    ],
    filters={"country": "CR", "work_mode": "hybrid"},
)
```

Cada criterio solicitado debe coincidir (AND), pero una vacante aparece una
sola vez aunque coincida con varios requisitos. Por defecto la busqueda exige
requisitos `required`; se pueden incluir preferencias/inferencias pasando
`requirement_levels`. Los filtros y criterios se validan y parametrizan en SQL.
`get_current_analysis` permite recuperar el analisis vigente con sus estados y
evidencias. El esquema migra automaticamente a la version 2.

La busqueda tambien esta disponible desde la CLI; `--text` busca en titulo,
empresa, ubicacion y descripcion, y se pueden repetir criterios de requisitos:

```powershell
.\.venv\Scripts\python.exe -m jobpulse_v15 search `
  --text "quality engineer" --country CR --work-mode hybrid `
  --employment-type "Full-time" --schedule "Night shift" `
  --salary-currency USD --salary-period annual `
  --salary-at-least 5000 --salary-at-most 8000 `
  --requirement language=en `
  --requirement education=bachelor `
  --include-preferred --limit 25
```

La CLI devuelve JSON. Sin `--include-preferred`, `--include-inferred` o
`--include-ambiguous`, los criterios solo coinciden con requisitos marcados
como `required`. La busqueda de texto trata `%`, `_` y `!` como caracteres
literales, no como comodines SQL.
La salida JSON se mantiene por defecto. Para producir el mismo CSV controlado
que ofrece la UI (incluidos compatibilidad, motivos de revision y enlace, sin
descripcion ni payload), usa `--format csv`:

```powershell
.\.venv\Scripts\python.exe -m jobpulse_v15 search `
  --needs-review --format csv --sort-by date_published
```
El orden predeterminado `last_seen` conserva la actividad reciente de la fuente.
`--sort-by date_published` ordena por fecha de publicacion descendente y deja al
final las ofertas sin fecha conocida. `--published-from` y `--published-to`
aceptan fechas inclusivas en formato `AAAA-MM-DD`:

```powershell
.\.venv\Scripts\python.exe -m jobpulse_v15 search `
  --published-from 2026-10-01 --published-to 2026-10-31
```
La casilla de revision de datos en la UI y `--needs-review` en la CLI muestran
vacantes sin empresa o pais, con ubicacion original pero sin ciudad/provincia
normalizada, sin analisis registrado, con analisis pendiente/parcial/con error
o con campos `unknown` o `ambiguous`. No se marca por si sola como error una
modalidad desconocida, un idioma no determinado o un campo `not_mentioned`:
estos estados pueden significar que la fuente no los proporciona o que las
reglas no encontraron una mencion. Esta vista prioriza la revision, pero no
modifica los datos ni convierte incertidumbre en un hecho.
Los resultados incluyen `review_reasons` en el JSON de busqueda, con codigos
estables como `missing_company`, `location_not_normalized`,
`analysis_partial` o `unknown_field:license`. La UI muestra el conteo en la
columna `Datos` y detalla los motivos al seleccionar la vacante. Cuando no hay
motivos detectados se muestra `OK`; esto no garantiza que todos los datos sean
correctos, solo que las reglas actuales no detectaron faltantes.
El filtro `Motivo de revision` permite limitar los resultados a un codigo; por
ejemplo, `missing_country`, `analysis_partial` o `unknown_field:license`.
La CLI acepta el mismo filtro con `--review-reason`:

```powershell
.\.venv\Scripts\python.exe -m jobpulse_v15 search `
  --review-reason location_not_normalized
```
La CLI tambien permite usar `--profile-compatibility` con los estados
`matched`, `not_registered`, `below_requirement`, `needs_review`,
`not_analyzed` o `no_required`. Por ejemplo:

```powershell
.\.venv\Scripts\python.exe -m jobpulse_v15 search `
  --profile-compatibility matched
```
Cuando se solicita este filtro, la salida JSON incluye la etiqueta
`profile_compatibility`; el CSV conserva la misma etiqueta y aplica el filtro
antes de exportar. La comparacion usa solo datos guardados y aprobados del
perfil; no significa que un requisito no registrado sea una carencia real.
Las vacantes con `skip_automatic = 1` se excluyen por defecto de la busqueda;
usa `--include-manually-skipped` para volver a incluirlas. Este filtro de
busqueda no elimina los anuncios del listado resumido solicitado a Workday;
cuando el ID de origen es reconocible, el adaptador evita descargar su detalle
y cuenta el anuncio como omitido, no como cero resultados. Si no se puede
reconocer un ID estable en el listado, Workday puede requerir descargar el
detalle para identificarlo.

## Extractor inicial de requisitos

La version `rules-1` reconoce frases explicitas en ingles y espanol para
idiomas, estudios universitarios/secundarios, licencia de conducir y años de
experiencia. No usa red ni IA. Los hallazgos guardan como evidencia la frase
que activó la regla; los indicadores de preferencia y obligatoriedad se
clasifican por separado. Cuando el texto alude a un requisito que las reglas no
pueden interpretar, el campo queda `unknown`, no se presenta como ausente.
Un campo `not_mentioned` significa que no se detectaron menciones de esa
categoria en las reglas actuales, por lo que los resultados deben revisarse
antes de utilizarlos para excluir candidatos u ofertas.

Para extraer y persistir el analisis de una oferta ya guardada:

```powershell
.\.venv\Scripts\python.exe -m jobpulse_v15 analysis extract --job job-123
```

El comando analiza `description_raw` y `requirements_raw`, registra la huella
del texto y la configuracion de la fuente, y conserva las revisiones anteriores.
`get_current_analysis` permite revisar los requisitos y la evidencia devueltos.
Para procesar en lote solo vacantes que no tengan analisis vigente, usa:

```powershell
.\.venv\Scripts\python.exe -m jobpulse_v15 analysis extract-pending `
  --limit 25 --dry-run
.\.venv\Scripts\python.exe -m jobpulse_v15 analysis extract-pending `
  --limit 25
```
El limite permitido es de 1 a 500. El modo normal es local y no consulta sitios
web; no reanaliza vacantes que ya tienen analisis, devuelve resumen y errores
por vacante, y retorna codigo de salida distinto de cero si algun registro
fallo. Las vacantes sin texto quedan reportadas como error. Para incluir
vacantes excluidas manualmente de las busquedas normales, agrega
`--include-manually-skipped`.
El informe global de calidad tambien esta disponible como JSON:

```powershell
.\.venv\Scripts\python.exe -m jobpulse_v15 quality report
.\.venv\Scripts\python.exe -m jobpulse_v15 quality duplicates --limit 100
```

La deteccion de posibles duplicados agrupa por titulo, empresa, pais y ciudad
normalizados (sin distinguir mayusculas, acentos ni puntuacion). Solo incluye
registros con esos cuatro campos disponibles. Son candidatos para revision
manual, no confirmaciones: el sistema nunca fusiona ni elimina vacantes
automaticamente. La salida lista los identificadores de cada grupo.
Los totales incluyen todas las vacantes de la base y no estan limitados por
los filtros de busqueda ni por el maximo de resultados de la UI.

No se guardan credenciales en la configuracion de sitios. El JSON de
configuracion es declarativo y no se ejecuta como codigo.
