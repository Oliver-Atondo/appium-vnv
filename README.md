# Appium MCP Studio

Automatizacion **Model Driven**: Copilot CLI es el agente, `application_graph.json`
es el modelo de la aplicacion y la web es un dashboard. No hay chat, modelo local,
VLM, proveedor de inferencia ni API key de modelos en Studio.

## Arquitectura

```text
Copilot CLI -- MCP HTTP --> Studio -- MCP stdio --> appium-mcp --> iPhone
                              |
                        application_graph.json
                              |
Dashboard Streamlit -- HTTP --> actividad, sesion y pasos compartidos
```

Studio mantiene una sola conexion a `appium-mcp`, iniciada con
`npx -y appium-mcp@latest`. Las herramientas que ejecuta Copilot y los pasos
que muestra el dashboard pertenecen al mismo proceso y sesion, no a dos
servidores Appium independientes. Abrir o cerrar el dashboard no detiene la sesion.

## Requisitos

- Python 3.10+ y Node.js 22+ con `npx` disponible.
- Copilot CLI instalado y autenticado.
- `application_graph.json` en la raiz del proyecto; es el unico mapa.
- Xcode y un runtime de simulador iOS para iPhone.
- Solo se admiten simuladores de iPhone locales de esta Mac; no Android,
  iPhone fisicos ni servidores Appium remotos.

El SDK MCP requerido es 2.3+. No se necesita instalar SGLang, Qwen ni otro modelo.

## Arranque

En una terminal inicia el servicio compartido:

```bash
./run.sh --mcp
```

El MCP queda en `http://127.0.0.1:8765/mcp`. El primer descubrimiento de
herramientas inicia Appium MCP y puede descargar sus dependencias mediante `npx`.

En otra terminal abre el dashboard:

```bash
./run.sh
```

La web queda en `http://127.0.0.1:8501`. Ambos comandos preparan `.venv/` e
instalan las dependencias declaradas. Para otro puerto del servicio:

```bash
./run.sh --mcp --port 8766
```

Cambia tambien la URL del servicio en el dashboard y en la configuracion de Copilot.

## Copilot CLI

Registra el servicio una vez:

```bash
copilot mcp add --transport http --timeout 300000 appium-studio http://127.0.0.1:8765/mcp
```

El endpoint admite el transporte MCP de Copilot CLI con protocolo `2026-07-28`,
incluido su `ping` de comprobacion de vida. Se conserva tambien la inicializacion
de versiones anteriores. La compatibilidad del ping solo se aplica despues de
validar la peticion; los metodos desconocidos y sobres invalidos siguen rechazados.

Despues abre `copilot` en la terminal. Las herramientas de Studio incluyen:

- `get_application_graph_summary`: resumen del mapa canonico.
- `search_application_graph`: nodos y relaciones cercanas por nombre, ID o texto.
- `get_application_node`: un nodo exacto y sus relaciones entrantes y salientes.
- `get_studio_state`: actividad, contexto extra, ruta o URL de build, dispositivo,
  ID de sesion y ultimos pasos.
- `inspect_ios_environment`: Xcode, simuladores y herramientas de conectividad.
- `show_ios_simulator`: abre la ventana del iPhone elegido por el agente.
- `check_ios_connection`: verifica `/status` de WDA en un puerto local.
- Las herramientas originales de Appium MCP, salvo `appium_ai`.

Recursos MCP disponibles:

- `application://graph/summary`: resumen JSON.
- `application://graph/map`: el mapa completo, bajo peticion explicita.
- `studio://activity`: actividad y estado compartido.

El servidor entrega instrucciones Model Driven durante la inicializacion MCP:
consultar el mapa, verificar sus evidencias en el XML real de Appium y priorizar
accessibility IDs. El grafo describe codigo extraido, no el estado actual de la
build. Las plantillas `%@` no son IDs literales.

Evita conectar otro `appium-mcp` independiente para esta actividad: usa las
herramientas de `appium-studio` para que la sesion y la grabacion sean compartidas.

## Flujo

1. En el dashboard guarda el nombre de actividad, instrucciones, ruta o URL de build de simulador,
   ruta o URL del repositorio opcional y archivo extra `.txt`, `.md` o `.json` UTF-8.
2. En Copilot pide, por ejemplo:

   > Usa appium-studio. Lee get_studio_state y el mapa canonico, selecciona el
   > iPhone de simulador que te indique, prepara WDA, crea la sesion con la build
   > de la actividad y ejecuta el flujo solicitado consultando el mapa antes de actuar.

3. Copilot utiliza `select_device`, `prepare_ios_simulator` y
  `show_ios_simulator`, compone las capabilities con IA a partir del dispositivo,
  la build y el `capabilitiesHint` de WDA, verifica la conexion y llama a
  `appium_session_management`. Studio no genera ni reescribe ese JSON. Usa el
   `session_id` de `get_studio_state` para dirigir las acciones a esa sesion.
4. El dashboard se actualiza cada dos segundos. Muestra contexto, sesion,
   herramienta activa y pasos con argumentos, resultado, estado y duracion.
  La ventana nativa de Simulator la abre Copilot; no es un stream web.
5. Guarda la grabacion desde el dashboard. Los archivos JSON y replay quedan en
   `recordings/`. El replay salta las consultas del mapa, remapea las nuevas
   sesiones y WDA y se detiene en el primer error.
6. Pide a Copilot que elimine la sesion Appium cuando termine. Detener el
   servicio tambien cierra la conexion upstream y limpia sus sesiones propias.

El dashboard no interpreta instrucciones, no elige herramientas y no lanza
sesiones de Appium: esas decisiones y acciones corresponden a Copilot.

## Preparacion autonoma del agente

Copilot se encarga del diagnostico, seleccion del iPhone local, arranque del simulador,
preparacion de WDA, capabilities, conexion, instalacion, ejecucion y limpieza.
El servidor ofrece operaciones verificables, no una receta fija de capabilities.

No hay tuneles USB, iproxy, RSD, perfiles de firma ni preparacion de dispositivos
fisicos. El servicio valida los UDID contra los simuladores disponibles, restringe
`select_device` a iOS/simulator y rechaza sesiones remotas. Las capabilities
compuestas por Copilot deben incluir explicitamente el UDID local del simulador.
El agente no debe pedir contrasenas ni intentar saltarse protecciones del sistema.

## Build de iOS

Una `.ipa` compilada para iPhone fisico **no funciona en el simulador**,
incluso si ambos usan arm64. Necesitas una `.app` compilada para
`iphonesimulator`, como carpeta `.app` o comprimida en `.zip`, compatible con el runtime y arquitectura.
Las URLs terminadas en `.ipa` se rechazan al guardar la actividad.
Studio no convierte ni recompila builds.
Se aceptan rutas locales, por ejemplo `/Users/usuario/Downloads/MyApp.app`
o `~/Downloads/MyApp.zip`. Las rutas relativas se resuelven desde la raiz de
este proyecto; se comprueba que existan y se guardan como rutas absolutas.
El repositorio tambien puede ser una carpeta local. Copilot usa directamente
la ruta de build en `appium:app`, sin subirla a un servidor.
Si se usa una URL HTTP(S), debe ser de descarga directa, no una pagina con inicio de sesion.

## Seguridad

- Servicio de un solo usuario, limitado a `127.0.0.1`. No exponerlo a Internet.
- Se rechazan Host/Origin externos. No hay autenticacion entre procesos locales.
- Solo usar mapas, builds, contexto y endpoints de confianza, con datos de prueba.
- Las variables `AI_VISION_*` se eliminan y `AI_VISION_ENABLED=false` se impone
  tanto en el servicio como en el replay. No se configura un VLM.
- Las grabaciones pueden contener credenciales en argumentos, contexto sensible
  y tokens de URLs firmadas. Revisarlas antes de compartirlas.
- Copilot puede enviar el contexto y los resultados a su modelo: aplicar las
  politicas de datos correspondientes. El dashboard no realiza esa inferencia.
- Mapa limitado a 16 MiB y archivo extra a 2 MiB. El estado incluye los ultimos
  300 pasos; la grabacion guardada conserva todos.

## Pruebas

```bash
.venv/bin/python -m unittest discover -s tests -v
```

Pruebas del protocolo MCP HTTP, recursos del grafo, estado compartido,
seguridad local, dashboard con AppTest y replay. Usan dobles para Appium:
no instalan una build ni arrancan un dispositivo.

## Archivos

```text
application_graph.json  # mapa canonico
src/studio.py           # actividad, grafo, conexion Appium y grabacion compartidos
src/studio_mcp.py       # servidor MCP HTTP y API del dashboard
src/ios_runtime.py      # operaciones de simulador local controladas por Copilot
src/app.py              # dashboard Streamlit, sin agente interno
src/mcp_bridge.py       # conexion upstream Appium por stdio
src/workflow.py         # validacion, consultas del grafo y utilidades iOS
src/recorder.py         # pasos y generacion de replay
tests/test_workflow.py  # pruebas
```