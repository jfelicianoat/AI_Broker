# Informe Red Team — AI Broker

**Fecha:** 3 de octubre de 2026. **Horizonte del premortem:** 3 de abril de 2027.

**Dictamen:** el proyecto tiene controles de estilo y tipos sólidos y una suite extensa, pero no supera sus requisitos actuales de entrega. Hay cuatro asuntos bloqueantes con el alcance indicado debajo: CI, recuperación de documentos, recuperación de herramientas mutables y privacidad de la conversión cuando se admite visión cloud. Esto no demuestra que se hayan producido daños en el servicio actual.

## Alcance y método

Se revisó el checkout de `C:\Procesos\AI_Broker`, sobre HEAD `66127eb8`, incluyendo su configuración y cambios locales existentes. El entorno permitió tres subagentes: realizaron cinco misiones por tandas —calidad, seguridad, arquitectura, premortem y abogado del diablo—. El revisor principal contrastó evidencias, reprodujo el problema del backup y ejecutó la suite completa.

Las comprobaciones usaron lectura de código, SQLite temporal, dobles de herramientas, transporte HTTP simulado y una construcción del paquete sobre una copia temporal. No se contactaron proveedores reales ni se alteró la base de producción. Docker real quedó desactivado en las pruebas. No se exponen valores secretos. No se aplicaron correcciones; el entregable de esta revisión es este informe.

**Semáforo:** 🔴 bloquea el requisito o uso señalado; 🟠 merece atención por un defecto o riesgo concreto; 🟡 aviso sin fallo ni riesgo demostrado. Los escenarios del premortem y las consecuencias de las objeciones de diseño son hipótesis, no incidentes observados.

## Comprobaciones

| Comprobación | Resultado |
|---|---|
| Ruff sobre el proyecto | Pasa |
| Mypy | Pasa; 54 archivos |
| Primera selección de pruebas | 163 pasan |
| Suite completa con cobertura | **979 pasan, 4 se omiten, 2 subtests pasan; 65,24 segundos** |
| Cobertura completa local | **87,95 %; inferior al 90 % exigido; salida 1** |
| Wheel construido en copia temporal | 58 miembros; **ningún archivo de `app/templates` ni de `app/static`** |
| Recuperación de documentos | La resubida reutiliza la referencia rota y leerla produce `FileNotFoundError` |
| Redirección de `fetch_url` | Un 302 simulado permite llegar a una dirección loopback |
| Recuperación de herramienta MCP mutable | Un efecto simulado se aplica dos veces tras una interrupción |

La ejecución completa utilizó `pytest -q -p no:cacheprovider --cov=app --cov-report=term`, con directorios temporales de pruebas y cobertura y `AI_BROKER_SANDBOX_DOCKER=0`. El fallo de cobertura es local y demostrado; no se ejecutó el CI remoto. Las advertencias incluyen conexiones SQLite sin cerrar en pruebas y una deprecación de Starlette/httpx: no se interpretan como prueba de una fuga de recursos en producción.

## 🔴 Bloqueantes

### R1. Los controles de entrega no pasan

**Dos problemas independientes.** La suite completa mide 87,95 % de cobertura frente al mínimo de 90 % definido en [pyproject.toml](C:/Procesos/AI_Broker/pyproject.toml:57). Además, [test_contract.py](C:/Procesos/AI_Broker/tests/test_contract.py:4) importa `jsonschema`, declarado en el extra de desarrollo, pero ausente de [requirements.lock](C:/Procesos/AI_Broker/requirements.lock). El [CI](C:/Procesos/AI_Broker/.github/workflows/ci.yml:33) instala ese lock y después el proyecto editable con `--no-deps`.

**Evidencia:** el umbral falla en esta ejecución. La ausencia de la dependencia y la ruta de instalación se verificaron por inspección; el fallo de recogida en un entorno limpio se deduce de esas entradas. El entorno local sí tiene `jsonschema`.

**Bloquea:** declarar superados los estándares de entrega del repositorio. **Corrección:** completar y validar el lock de desarrollo en un entorno limpio; cubrir comportamientos relevantes hasta superar el umbral acordado. No rebajar el mínimo solo para ocultar el resultado.

### R2. El backup no recupera los documentos y la resubida no los repara

[create_state_backup](C:/Procesos/AI_Broker/app/maintenance.py:38) copia SQLite y el árbol de artefactos, pero omite `state/files`. Las filas de documentos sobreviven en la base restaurada. La [deduplicación](C:/Procesos/AI_Broker/app/ingestion/service.py:406) devuelve una fila existente sin comprobar que sus archivos continúen en disco.

**Reproducción:** subir y preparar un texto en un entorno temporal; crear backup; simular ausencia del original y su conversión; restaurar la base; resubir el mismo contenido. Resultado: mismo `file_id`, `created=False`, original ausente y `FileNotFoundError` al leer Markdown. La [documentación operativa](C:/Procesos/AI_Broker/docs/Phase_6_Operations.md:39) reconoce la omisión, pero afirma incorrectamente que resubir regenera el documento.

**Bloquea:** utilizar este backup como garantía de recuperación de tareas con adjuntos después de perder o trasladar su almacenamiento. **Corrección:** incluir originales y conversiones, definir una instantánea coherente, comprobar referencias al restaurar y reparar filas huérfanas en la deduplicación. Criterio de cierre: restaurar en otro directorio y ejecutar una tarea con sus adjuntos sin depender del almacén original.

### R3. Un reinicio puede repetir cambios realizados por una herramienta MCP

El [coordinador](C:/Procesos/AI_Broker/app/coordinator.py:1431) llama a MCP antes de registrar el resultado. La [recuperación](C:/Procesos/AI_Broker/app/repository.py:1280) trata la ambigüedad de invocaciones de modelos, pero carece de un registro equivalente para efectos de herramientas. Puede reencolar el agente y repetir la operación.

**Reproducción:** un doble MCP aplica un efecto y simula una interrupción antes de persistir su resultado. Recuperar y reintentar eleva el contador de efectos de uno a dos. No se modificó ningún sistema externo.

**Bloquea:** habilitar herramientas que modifican datos bajo una garantía de recuperación segura. Con herramientas exclusivamente de lectura, el impacto es menor. **Corrección:** identidad estable de operación, registro durable `started/completed/ambiguous`, checkpoints de conversación e idempotencia del servidor cuando exista. Un efecto ambiguo sin idempotencia exige reconciliación antes de reintentar.

### R4. La privacidad de una tarea llega después de la conversión del documento

La [subida de archivos](C:/Procesos/AI_Broker/app/main.py:699) no recibe clasificación de datos ni permiso de salida. La conversión [elige visión del catálogo completo](C:/Procesos/AI_Broker/app/ingestion/service.py:293), y [select_vision_target](C:/Procesos/AI_Broker/app/ingestion/vision.py:87) admite destinos cloud. El [motor](C:/Procesos/AI_Broker/app/ingestion/engines.py:324) puede enviar la figura y hasta 1.200 caracteres de contexto antes de crear una tarea `local_only`.

**Validación:** una configuración sintética con visión cloud habilitada selecciona ese destino sin política de datos. **La configuración actual fija un modelo local; no se ha demostrado una fuga actual.**

**Bloquea:** prometer confidencialidad de documentos cuando su conversión pueda seleccionar cloud. **Corrección:** clasificar y persistir la política en la subida; filtrar y volver a comprobar el destino antes de cada envío. Conversión local por defecto y autorización explícita de salida para cada documento.

## 🟠 Hallazgos que merecen atención

| ID | Hallazgo y evidencia | Impacto, validación y corrección |
|---|---|---|
| O1 | **La protección SSRF se salta mediante redirecciones.** [skills.py](C:/Procesos/AI_Broker/app/skills.py:214) valida la URL inicial y sigue redirecciones automáticamente. | Reproducido sin red: una URL pública redirige a loopback y devuelve contenido simulado. Requiere un agente autorizado a usar `fetch_url` que visite un sitio hostil. No demuestra saltarse la autenticación del broker. Validar cada salto y conectar a la dirección validada; considerar control de salida de red. |
| O2 | **Un token de una sesión aparece en un log versionado.** [run_broker.py](C:/Procesos/AI_Broker/scripts/run_broker.py:48) lo imprime y [el lanzador desatendido](C:/Procesos/AI_Broker/arrancar_ai_broker_desatendido.bat:6) redirige la consola. | Se verificó presencia de credencial en `logs/restart-console.log`, línea 8, tanto en checkout como en HEAD, sin mostrar su valor. Vigencia no comprobada; los tokens generados cambian al reiniciar. Evitar imprimirlos en modo desatendido, usar keyring, retirar logs del índice y revisar credenciales expuestas antes de compartir el repositorio. |
| O3 | **Los servidores MCP heredan todos los secretos del entorno.** [mcp.py](C:/Procesos/AI_Broker/app/mcp.py:298) copia íntegramente `os.environ`. | Mock de arranque: un servidor sin variables declaradas recibe token admin y clave ajena sintéticos. El riesgo depende de comprometer o confiar en exceso en ese servidor. Construir un entorno mínimo y autorizar expresamente los secretos necesarios por servidor. |
| O4 | **El límite de salida del sandbox llega después de acumularla en memoria.** [sandbox.py](C:/Procesos/AI_Broker/app/sandbox.py:93) recibe stdout/stderr completos y [los trunca después](C:/Procesos/AI_Broker/app/sandbox.py:263). | Mock: 100.000 bytes recibidos con límite de 500 caracteres. Código que imprime continuamente puede agotar memoria del broker aunque Docker tenga límites. Leer por bloques, limitar bytes acumulados y terminar/limpiar el contenedor al superar el límite. No se ejecutó una carga abusiva real. |
| O5 | **La distribución normal omite el panel.** [pyproject.toml](C:/Procesos/AI_Broker/pyproject.toml:41) no declara sus recursos; [main.py](C:/Procesos/AI_Broker/app/main.py:444) exige `static` al arrancar. | Wheel temporal: cero plantillas y recursos estáticos. La instalación editable utiliza el árbol de fuentes y oculta el problema. Declarar `package-data` y comprobar el arranque desde un wheel instalado. Bloquea esa forma de distribución, no la ejecución actual desde el checkout. |
| O6 | **Una finalización puede sobrescribir una cancelación.** [repository.py](C:/Procesos/AI_Broker/app/repository.py:582) no condiciona la transición al estado anterior; [la ingesta](C:/Procesos/AI_Broker/app/ingestion/service.py:572) finaliza sin comprobar cancelación. | Reproducción aislada: una conversión solicita cancelación antes de retornar y acaba `completed` con `cancel_requested=True`. Usar transiciones atómicas condicionadas; preservar estados terminales y comprobar cancelación en la finalización. |
| O7 | **El límite de cola no limita todo el trabajo pendiente.** [repository.py](C:/Procesos/AI_Broker/app/repository.py:124) cuenta solo `queued`; [las ingestas](C:/Procesos/AI_Broker/app/repository.py:445) se crean sin ese límite. | Con límite uno, una tarea esperando memoria permite admitir otra; tres ingestas adicionales elevan los `queued` a cuatro. Aplicar cupos por carril a todos los pendientes y reservar capacidad/espacio antes de persistir subidas. |
| O8 | **Dos instancias pueden reclamar la misma tarea.** [workers=1](C:/Procesos/AI_Broker/app/config.py:111) no impide otra instancia; [el arranque](C:/Procesos/AI_Broker/app/main.py:236) recupera tareas sin propietario de proceso. | Dos repositorios sobre SQLite temporal: el segundo recupera y reclama una tarea que el primero mantiene activa. Depende de un segundo arranque sobre la misma base; no se observó en producción. Añadir bloqueo exclusivo por base o leases con propietario y caducidad. |
| O9 | **La retención solo se ejecuta al arrancar.** [main.py](C:/Procesos/AI_Broker/app/main.py:246) realiza las podas sin programar mantenimiento periódico. | Confirmado por inspección. Un proceso activo durante meses no hace cumplir el plazo de retención durante ese tiempo. Archivos y artefactos además tienen retención cero en la configuración actual. Programar poda en lotes y alertas/cupos de disco, preservando referencias necesarias. La fecha de saturación depende del uso y no se estimó. |

## 🟡 Aviso sin fallo demostrado

**Y1. Python 3.10 es la versión mínima declarada, pero CI ejecuta solo 3.14.** [pyproject.toml](C:/Procesos/AI_Broker/pyproject.toml:5) y [ci.yml](C:/Procesos/AI_Broker/.github/workflows/ci.yml:27). Mypy comprueba compatibilidad de tipos con 3.10, pero eso no sustituye una ejecución. No se ha demostrado incompatibilidad. Incluir la versión mínima en CI si se mantiene esa promesa.

## Premortem: qué podría fallar en seis meses

Estos escenarios reutilizan causas verificadas; no se cuentan como nuevos defectos ni como incidentes actuales. La probabilidad es cualitativa y condicionada; no se midió el volumen real de uso.

| Escenario al 3 de abril de 2027 | Desencadenante y probabilidad | Señal temprana | Prevención |
|---|---|---|---|
| 🔴 **Restauración válida, documentos ausentes.** | Avería de disco o traslado; fallo previsible si se depende únicamente del backup actual. Causa R2. | Filas `ready` sin original/conversión; backup verificado sin ensayo de tarea con adjuntos. | Backup completo y simulacro de recuperación en otro directorio antes de confiarle continuidad. |
| 🟠 **Disco lleno mientras se siguen admitiendo subidas.** | Uso sostenido de documentos/vídeos; probabilidad alta si crece sin cuotas. [Configuración](C:/Procesos/AI_Broker/broker_config.yaml:39): 8.192 MB por archivo, 5.000 páginas y retención desactivada. | Menor espacio libre, crecimiento sostenido de originales y temporales, backups más lentos. | Cuotas globales y por cliente, reserva de espacio y retención acordada. No borrar adjuntos de tareas pendientes. |
| 🟠 **Colas que cumplen el cupo aparente, pero incumplen los plazos.** | Ráfagas y esperas por recursos; probabilidad alta al ampliar clientes sin medir capacidad. O7; [conversiones](C:/Procesos/AI_Broker/broker_config.yaml:45) de hasta 7.200 segundos con dos trabajadores. | Mayor antigüedad del pendiente más viejo; ritmo de entrada superior al de terminación; puestos ocupados durante horas. | Control de admisión por carril/cliente, medir todos los estados de espera y ensayar carga representativa. |
| 🟠 **Cambios de proveedores degradan respuestas y distorsionan costes.** | Actualizaciones de modelos/API/tarifas; probabilidad media, creciente con servicios externos. [Cuarentena](C:/Procesos/AI_Broker/app/providers/routing.py:850) permite reintentar cuando todos están apartados; [costes](C:/Procesos/AI_Broker/app/providers/openai_compatible.py:232) dependen de tarifas configuradas. | Más fallbacks, `routing.quarantine_bypassed` y divergencias con la facturación del proveedor. | Regresión previa a cambios, conciliación de consumo, precios con vigencia y modo conservador para tareas sensibles. No se compararon tarifas ni facturas reales. |

## Abogado del diablo: decisiones que conviene someter a prueba

Las cuatro objeciones son 🟠. Sus mecanismos existen en código; su efecto sobre el acierto real requiere evaluación. Ninguna demuestra por sí sola un bloqueo.

1. **¿Se optimiza exactitud o simplemente que la llamada termine?** [model_stats.py](C:/Procesos/AI_Broker/app/model_stats.py:94) cuenta `completed` como éxito y [model_timing.py](C:/Procesos/AI_Broker/app/model_timing.py:118) usa esa tasa al estimar el tiempo hasta una respuesta correcta. Una salida rápida y equivocada puede mejorar el ranking. **Alternativa:** separar fiabilidad operativa de exactitud y utilidad; imponer calidad mínima antes de optimizar tiempo. **Justificación actual:** transformaciones verificadas automáticamente o priorizar disponibilidad sin prometer exactitud. **Prueba:** evaluación ciega con respuestas verificables y tiempo total hasta aceptación, incluido el retrabajo.

2. **¿Más parámetros equivale al mejor árbitro?** [routing.py](C:/Procesos/AI_Broker/app/providers/routing.py:1038) reconoce que el tamaño es un indicio pobre y lo convierte en prioridad. **Alternativa:** árbitros evaluados por dominio, contradicciones y fidelidad a fuentes; tamaño como desempate provisional. **Justificación actual:** catálogo nuevo sin evidencia y presupuesto suficiente. **Prueba:** idénticas propuestas con errores conocidos y orden permutado; comparar acierto, errores introducidos, latencia y coste frente a otros árbitros y al mejor modelo único.

3. **¿Varias propuestas aportan evidencia independiente?** [El contrato](C:/Procesos/AI_Broker/app/schemas.py:401) declara `different_families`, pero [la selección](C:/Procesos/AI_Broker/app/providers/routing.py:976) puede repetir modelos y [el coordinador](C:/Procesos/AI_Broker/app/coordinator.py:2706) los agrega sin imponer familias distintas. La aplicación de esa política no se encontró fuera del esquema y una fixture. **Alternativa:** diversidad explícita por identidad/familia y errores observados, o verificadores pertinentes. **Justificación actual:** explorar roles con pocos modelos locales, presentándolo como varias muestras. **Prueba:** comparar modelo único, un modelo con varios roles y familias distintas con el mismo presupuesto; medir correlación de errores y mejora de exactitud.

4. **¿Todas las capas de decisión compensan su complejidad?** La [heurística](C:/Procesos/AI_Broker/app/strategy_router.py:201) combina señales indirectas; [el juez](C:/Procesos/AI_Broker/app/coordinator.py:1233) devuelve confianza `1.0` al fallar; [System‑1](C:/Procesos/AI_Broker/app/system1.py:79) declara confianza sin calibrar. **Alternativa:** perfiles explícitos por caso de uso y activar cada capa cuando demuestre valor. **Justificación actual:** plataforma experimental con necesidades heterogéneas y capacidad de mantenimiento. **Prueba:** ablación sobre las mismas solicitudes, añadiendo capas una a una y midiendo calidad, latencia, coste y mantenimiento. Ante ausencia de juicio, registrar incertidumbre sin presentarla como máxima confianza.

## Orden de actuación y criterios de cierre

1. **Entrega:** lock completo e instalación limpia; suite que supere el 90 %; arranque del wheel con sus recursos.
2. **Recuperación:** incluir documentos y verificar una restauración completa. Limitar herramientas mutables hasta resolver efectos ambiguos y duplicados.
3. **Privacidad y seguridad:** política de salida en la subida/conversión; validar redirecciones; mínimo entorno MCP; sacar credenciales y logs del repositorio; limitar la salida del sandbox durante la lectura.
4. **Operación:** estados terminales protegidos, exclusión de segunda instancia, cupos efectivos por carril, mantenimiento periódico y alertas de capacidad.
5. **Decisiones de producto:** evaluación comparativa que justifique routing, árbitros y consenso con calidad observable, no solo terminación técnica.

**Controles favorables comprobados:** autenticación común del panel y API sensible, CSRF, cookies HttpOnly/SameSite, arranque protegido ante exposición sin token, sandbox sin red ni montajes del host, reclamos de tareas con `BEGIN IMMEDIATE`, cambios de estado y eventos en transacciones, carriles separados y tratamiento conservador de llamadas remotas a modelos cuya finalización es ambigua. Conviene conservar estas protecciones al corregir los hallazgos.
