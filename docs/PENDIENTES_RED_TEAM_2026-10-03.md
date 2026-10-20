# Mejoras pendientes después del informe Red Team

Fecha: 3 de octubre de 2026.

Este documento deja una guía para retomar los asuntos del informe
`INFORME_RED_TEAM_2026-10-03.md`. Se ha autorizado corregir R2 y R4. Los demás
asuntos quedan pendientes salvo el bloqueo compartido que necesita R2 y que
también cubre la exclusión básica descrita en O8.

## Qué cambia con R2: recuperar de verdad los documentos

Antes, la copia guardaba la base de datos y las respuestas del broker, pero
dejaba fuera los archivos subidos. Era como copiar el catálogo de una biblioteca
sin copiar sus libros: al recuperar el catálogo, las entradas estaban allí,
pero los documentos no.

La nueva copia incluye originales, documentos convertidos y respuestas
guardadas. Comprueba que los archivos que menciona la base están dentro de la
copia y que su contenido coincide con las comprobaciones guardadas. Si falta
un documento, lo comunica en vez de entregar una copia aparentemente completa.

La copia se hace con el broker detenido. Esto evita que, mientras se copia un
documento, el servicio cambie su estado o termine una conversión. El servicio y
la herramienta de copias usan el mismo bloqueo del sistema operativo: si uno
está usando el estado, el otro espera a una nueva ejecución y muestra un mensaje
para detener el broker. No hay que borrar el archivo del bloqueo; queda
liberado al terminar el proceso.

Al restaurar en otra carpeta, también cambian las direcciones guardadas en la
base. Así, el broker busca el archivo recuperado en la nueva carpeta, sin
depender del disco anterior. Primero se comprueba y prepara el contenido;
después se publican los archivos y, al final, la base. Si una escritura falla,
se intenta volver a la versión anterior. Si el proceso se corta abruptamente,
una marca impide arrancar con una restauración a medias: hay que repetirla.

Volver a subir un documento cuyos archivos se perdieron o dañaron repara su
registro y conserva su identificador. Esto es importante porque las tareas
ya creadas lo siguen usando. Si hace falta convertirlo de nuevo, se crea otro
trabajo de conversión aunque el documento ya existiera. Una fotografía que no
necesita conversión se recupera directamente.

Las copias antiguas no pueden recuperar documentos que nunca guardaron. Si
contienen referencias a esos documentos, la restauración se rechaza con una
explicación. Las copias antiguas sin documentos siguen admitiéndose.

## Qué cambia con R4: decidir la privacidad al subir el archivo

Antes, un documento podía enviarse a un servicio de visión en la nube durante
su preparación, antes de que existiera la tarea donde el usuario había elegido
«solo local». Decidirlo después no podía deshacer ese envío.

Ahora la decisión se toma al subirlo. La opción inicial es «Solo en este
equipo». Para permitir servicios en la nube hay que elegir «Uso interno» o
«Público» y marcar expresamente la autorización. «Confidencial» y «Solo en este
equipo» no admiten esa autorización.

La decisión queda guardada con el documento. Se comprueba al elegir el modelo
de visión y antes de enviar cada figura. El envío incluye también el texto que
rodea la figura, por lo que ambos están sujetos al mismo permiso. Si solo hay
visión en la nube y no se ha autorizado, se conserva el texto convertido y se
indica que las figuras no se han descrito.

El permiso también acompaña al archivo cuando se adjunta a una tarea. Un
archivo solo local obliga a procesar esa tarea localmente. Si la tarea pide
expresamente una herramienta o un modelo que enviaría los datos fuera, se
rechaza con una explicación. Con varios adjuntos se respeta el más restrictivo.
La restricción se comprueba otra vez al ejecutar la tarea.

Dos subidas del mismo contenido con permisos distintos no comparten un registro
que pueda mezclar esas autorizaciones. Los documentos anteriores al cambio se
consideran locales para usos futuros, pero se indica que no quedó registrada
su política anterior. No se afirma que nunca salieran a la nube. Si una fila
antigua está rota, una resubida compatible puede reconstruirla con una nueva
conversión local y mantener su identificador.

## R1 — Hacer que la instalación y las comprobaciones de entrega pasen

**Qué ocurre.** Las pruebas necesitan `jsonschema`, pero el archivo que instala
las dependencias en CI no lo incluye. La máquina del desarrollador puede
tenerlo y ocultar la falta. Además, el informe midió una cobertura del 87,95 %:
el proyecto exige un mínimo del 90 %. Esa cifra pertenece al informe; hay que
volver a medirla al retomar este punto.

**Qué cambiar.** Regenerar la lista de dependencias de desarrollo desde un
entorno vacío, incluyendo las que necesita `jsonschema`. Comprobar que otro
entorno vacío puede instalar y recoger las pruebas. Después identificar
comportamientos importantes que no estén probados y añadir casos que los
ejerciten. Mantener el mínimo del 90 %.

**Cómo saber que está resuelto.** La instalación limpia, Ruff, Mypy y todas las
pruebas pasan, y la cobertura alcanza el mínimo. Los nuevos casos deben detectar
errores reales: alcanzar un porcentaje por sí solo no demuestra fiabilidad.

**Dónde empezar.** `requirements.lock`, `pyproject.toml`,
`.github/workflows/ci.yml` y `tests/test_contract.py`.

## R3 — Evitar repetir una modificación externa al reiniciar

**Qué ocurre.** Imagina que una herramienta crea un registro en otra aplicación.
El broker se apaga justo después de crearlo, antes de guardar la confirmación.
Al arrancar, puede repetir el paso y crear un segundo registro.

**Qué cambiar.** Antes de enviar una operación, guardar qué va a hacerse y
asignarle un identificador que se conserve al reintentar. Después guardar la
respuesta y el punto desde el que debe seguir la conversación. Si el broker
reinicia y no tiene confirmación, registrar «no sabemos si se hizo».

Cuando el servidor admita reconocer operaciones repetidas, enviar el mismo
identificador para que devuelva el resultado anterior. Cuando permita consultar
el resultado, comprobarlo primero. Si no ofrece ninguna de esas posibilidades,
detener el reintento y pedir que se revise lo ocurrido. Una operación sin
confirmación no debe confundirse con una operación que seguro no sucedió.

**Cómo saber que está resuelto.** Simular apagados antes del envío y después de
aplicar el cambio. Un servidor que admite operaciones identificadas produce
un único efecto. Uno que no las admite deja el caso pendiente de revisión y
no repite el cambio por su cuenta. Dos acciones distintas con los mismos
argumentos deben seguir pudiendo ejecutarse.

**Dónde empezar.** `app/coordinator.py`, `app/repository.py`, `app/mcp.py` y
`app/db.py`. No basta con añadir un mensaje al log.

## O1 — Comprobar también los destinos de una redirección web

**Qué ocurre.** Se revisa una dirección pública, pero esa página puede responder
«el contenido está en otra dirección». Si la nueva dirección es un servicio
privado del equipo o de la red, el broker puede terminar accediendo a él.

**Qué cambiar.** Seguir las redirecciones una a una, revisar cada nueva dirección
y poner un máximo de saltos. La conexión debe usar la dirección de red ya
comprobada; resolver el nombre otra vez podría obtener un destino diferente.
Revisar tanto la descarga de páginas como la búsqueda web.

**Cómo saber que está resuelto.** Una página pública que redirige al propio equipo
o a la red privada se rechaza antes de conectar. Probar también direcciones
IPv6 y nombres que cambian de dirección entre la comprobación y la conexión.

**Dónde empezar.** `app/skills.py`.

## O2 — Sacar las credenciales de los registros y del repositorio

**Qué ocurre.** El arranque imprime el token de administración. El lanzador
desatendido guarda esa consola en un archivo que se está siguiendo con Git.
Quien reciba ese archivo podría recibir también una credencial.

**Qué cambiar.** El arranque desatendido no debe imprimir el token. Obtenerlo del
almacén de credenciales del sistema, cuyo mecanismo ya existe. Mostrarlo en
consola solo por una petición explícita e interactiva. Corregir la entrada de
`logs` en `.gitignore`, que lleva un espacio inicial, y sacar los logs del
seguimiento de Git. Comprobar e invalidar las credenciales expuestas que sigan
siendo utilizables.

**Cómo saber que está resuelto.** Un token de prueba no aparece en los logs ni en
la salida desatendida, pero se puede obtener por el mecanismo autorizado.
Antes de compartir el repositorio hay que revisar las versiones anteriores:
dejar de seguir el archivo no borra su historial.

**Dónde empezar.** `scripts/run_broker.py`, `arrancar_ai_broker_desatendido.bat`,
`.gitignore` y `app/admin_auth.py`.

## O3 — Dar a cada servidor MCP solo las credenciales necesarias

**Qué ocurre.** Al iniciar una herramienta externa, se le entrega todo el entorno
del broker. Puede recibir el token de administración y claves de servicios que
no necesita.

**Qué cambiar.** Crear un entorno reducido con las variables necesarias para
arrancar el proceso. Añadir únicamente los secretos declarados para ese servidor.
Por ejemplo, una herramienta de documentos recibiría su credencial de documentos,
sin recibir las claves de los modelos de IA.

**Cómo saber que está resuelto.** Dos servidores de prueba reciben credenciales
distintas y ninguno recibe secretos ajenos. Ambos deben seguir arrancando en
Windows y Linux, con texto UTF-8 correcto.

**Dónde empezar.** `app/mcp.py` y `MCPServerConfig` en `app/config.py`.

## O4 — Detener a tiempo un programa que imprime sin parar

**Qué ocurre.** El sandbox limita lo que devuelve al usuario, pero el broker
recoge primero toda la salida del programa. Un programa que imprime sin parar
puede llenar la memoria antes de que se recorte su respuesta.

**Qué cambiar.** Leer la salida normal y los errores en pequeños bloques, a la
vez. Mantener un límite de bytes acumulados. Si se supera, detener la ejecución
y limpiar el contenedor y sus volúmenes. Aplicarlo tanto con adjuntos como sin
ellos, y también cuando haya cancelación o se agote el tiempo.

**Cómo saber que está resuelto.** Un programa que imprime continuamente se detiene
al alcanzar el límite, no hace crecer la memoria sin control y no deja recursos
de Docker abandonados. Comprobarlo también con Docker real.

**Dónde empezar.** `app/sandbox.py`.

## O5 — Incluir el panel dentro del paquete instalable

**Qué ocurre.** Ejecutar desde la carpeta del proyecto encuentra las plantillas,
los estilos y el JavaScript. El paquete distribuible no los incluye, así que
instalarlo en otro sitio puede dejar el panel sin recursos o impedir el arranque.

**Qué cambiar.** Declarar los archivos HTML, CSS y JavaScript como contenido del
paquete. Construir el paquete e instalarlo en un entorno nuevo.

**Cómo saber que está resuelto.** Desde una carpeta que no contiene el código
fuente, el servicio arranca y el panel autenticado carga sus páginas, estilos
y botones. Comprobar que los archivos están realmente dentro del paquete.

**Dónde empezar.** `pyproject.toml` y una prueba de instalación en CI.

## O6 — Hacer que una cancelación no se convierta después en completada

**Qué ocurre.** El usuario cancela mientras una conversión está terminando. La
finalización puede escribir después «completada» y sobrescribir la cancelación.

**Qué cambiar.** Al guardar el resultado, actualizar el estado únicamente si la
tarea sigue activa y no se ha cancelado. Comprobar y actualizar en la misma
operación de base de datos. Registrar un cambio de estado solo cuando ocurrió.
Revisar todas las vías de finalización, no únicamente la conversión de documentos.

**Cómo saber que está resuelto.** Si la cancelación se confirma primero, el estado
queda cancelado. Si la finalización se confirma primero, cancelar no altera un
resultado ya terminado. Probar ambos órdenes de forma controlada.

**Dónde empezar.** `app/repository.py` y `app/ingestion/service.py`.

## O7 — Limitar todo el trabajo que sigue pendiente

**Qué ocurre.** La cola cuenta solo las tareas que esperan turno. Una tarea que
espera memoria, otra tarea o una herramienta sigue pendiente, pero puede dejar
de contar. Las conversiones también pueden admitir trabajo sin respetar ese cupo.

**Qué cambiar.** Separar los límites de inferencia y conversión, contar todas las
esperas y reservar un puesto al aceptar cada trabajo. Esa reserva sigue existiendo
cuando la tarea cambia de una espera a otra. Para una subida, reservar también
el espacio necesario y liberarlo si se rechaza o falla.

**Cómo saber que está resuelto.** Con un límite de uno, una tarea esperando memoria
impide aceptar otra del mismo carril. Varias subidas simultáneas no superan el
límite de conversión y los rechazos no dejan archivos sueltos.

**Dónde empezar.** `app/repository.py`, la entrada de subidas y `app/config.py`.

## O8 — Impedir dos servicios sobre la misma base

**Qué ocurre.** Dos procesos pueden interpretar el trabajo del otro como trabajo
interrumpido y reclamarlo de nuevo.

**Situación tras R2.** El bloqueo compartido de copias y servicio ya impide que
dos instancias nuevas arranquen sobre la misma base mientras una la utiliza.
Es una protección necesaria para garantizar una copia con el servicio detenido.
Hay que reiniciar la instalación anterior para que use ese bloqueo.

**Qué quedaría para más adelante.** Si se quiere permitir varios procesos a la
vez, habrá que cambiar el reparto de tareas: registrar quién tiene cada tarea,
renovar ese permiso y recuperar solo permisos caducados. Eso es un cambio de
arquitectura; para una sola instancia, el bloqueo basta.

**Cómo comprobar la protección actual.** Un segundo proceso falla antes de
recuperar tareas. Al terminar el primero, otro puede arrancar sin borrar el
archivo de bloqueo, incluso si el primero terminó abruptamente.

**Dónde empezar.** `app/state_lock.py`, el arranque y, para varios procesos,
`app/repository.py`.

## O9 — Cumplir la retención sin tener que reiniciar

**Qué ocurre.** Los archivos antiguos se revisan al arrancar. Si el broker lleva
meses funcionando, puede llevar meses sin aplicar la retención configurada.

**Qué cambiar.** Ejecutar una revisión periódica en lotes pequeños. Conservar
los documentos que todavía necesitan tareas pendientes o en ejecución. Coordinar
la limpieza con copias y reparaciones. Mostrar cuánto espacio queda y cuándo
se hizo la última revisión. El plazo de conservación debe decidirlo el operador;
no activaría un borrado automático de documentos sin esa decisión.

**Cómo saber que está resuelto.** Un archivo antiguo sin uso se elimina sin
reiniciar; otro que necesita una tarea pendiente se conserva. Un fallo parcial
no deja una base que ofrezca documentos inexistentes como si estuvieran disponibles.

**Dónde empezar.** `app/maintenance.py` y el ciclo de vida de `app/main.py`.

## Y1 — Probar la versión mínima de Python que se promete

**Qué ocurre.** El proyecto declara Python 3.10 como mínimo, pero CI solo ejecuta
3.14. Revisar los tipos no demuestra que la aplicación arranque en 3.10.

**Qué cambiar.** Ejecutar la instalación y las pruebas con 3.10 y 3.14 en Windows
y Linux. Ajustar las dependencias si no existe una combinación común compatible.

**Cómo saber que está resuelto.** Todas esas combinaciones instalan y ejecutan
las pruebas. Si se decide abandonar 3.10, cambiar de forma explícita el requisito
y la documentación.

**Dónde empezar.** `.github/workflows/ci.yml`, `pyproject.toml` y los locks.

## Decisiones sobre calidad que necesitan pruebas comparativas

### 1. Terminar una llamada no demuestra que la respuesta sea correcta

El histórico cuenta «completada» como éxito. Un modelo rápido que responde mal
puede quedar bien clasificado. Separar la tasa de llamadas terminadas de la
calidad verificada. Guardar cómo se evaluó cada respuesta y optimizar velocidad
solo entre modelos con calidad suficiente. Corregir la etiqueta de «tiempo hasta
respuesta correcta» mientras no exista esa medición.

Comprobarlo con preguntas cuya respuesta pueda verificarse y medir el tiempo
total hasta que se acepta la respuesta, incluyendo correcciones. Empezar en
`app/model_stats.py`, `app/model_timing.py` y `app/providers/routing.py`.

### 2. Un modelo más grande no tiene por qué juzgar mejor

El árbitro elige entre propuestas y debe detectar errores y respetar las fuentes.
Evaluarlo para ese trabajo y por tipo de tarea. Usar el tamaño como criterio
provisional cuando no exista evidencia suficiente.

Dar a varios árbitros las mismas propuestas con errores conocidos, cambiar su
orden y medir qué errores detectan o introducen, cuánto tardan y cuánto cuestan.
Comparar también con un único modelo respondiendo directamente. Empezar en
`app/providers/routing.py` y las pruebas de árbitros.

### 3. Tres respuestas del mismo modelo no son tres fuentes independientes

La selección puede repetir modelos aunque el contrato diga «familias distintas».
Hacer cumplir esa opción. Si faltan familias, informar de la limitación o usar
una alternativa que el cliente haya permitido. No presentar varias muestras
del mismo modelo como diversidad de familias.

Comparar un modelo, varias muestras de ese modelo y varias familias con el mismo
presupuesto. Medir cuánto coinciden sus errores y si mejora el resultado final.
Empezar en `app/schemas.py`, `app/providers/routing.py` y el consenso.

### 4. Un juez que falla debe expresar incertidumbre

Actualmente un fallo del juez puede convertirse en confianza máxima. Guardar
«confianza desconocida» con su motivo. Definir cómo continúa cada caso de uso:
por ejemplo, una tarea sensible necesitaría otra verificación. Ajustar las
puntuaciones de System-1 con ejemplos de los que se conoce la respuesta.

Comparar el flujo sencillo con cada capa añadida usando las mismas solicitudes.
Mantener cada capa cuando su mejora de calidad compense su tiempo y coste.
Empezar en `app/coordinator.py`, `app/system1.py` y `app/strategy_router.py`.

## Medidas para los escenarios de los próximos seis meses

- **Quedarse sin disco:** combinar límites de almacenamiento, espacio reservado
  para subidas y margen mínimo libre. Contar originales, conversiones y temporales,
  incluidos los creados antes de entrar al endpoint. Alertar con tiempo.
- **Demasiado trabajo para la capacidad disponible:** aplicar O7, mostrar la edad
  del trabajo más antiguo y comprobar si entra más trabajo del que termina.
  Probar carga representativa antes de ampliar clientes. Los cupos por cliente
  requieren identificar al cliente de forma autenticada.
- **Cambios de modelos o precios:** guardar la versión/configuración del modelo
  evaluado y la tarifa usada para calcular costes, con su fecha de vigencia.
  Mostrar «coste desconocido» cuando falten datos. Comparar consumo y facturación
  cuando el proveedor permita consultarlos. En tareas sensibles, no usar por
  defecto modelos apartados por fallos solo porque no quede otra opción.
- **Una copia que parece buena pero no permite trabajar:** repetir periódicamente
  el ensayo de R2 en otra carpeta, sin acceso al almacén original, y ejecutar una
  tarea con adjuntos. Guardar la copia en el mismo disco no protege de perderlo.

## Orden práctico para retomar los pendientes

1. R1 y O5: poder instalar y verificar una entrega completa.
2. R3, O1, O2, O3 y O4: herramientas externas, credenciales y límites de ejecución.
3. O6, O7 y O9: cancelaciones, admisión y conservación de datos.
4. Y1: respaldar la compatibilidad declarada.
5. Evaluaciones de calidad y cambios de selección basados en sus resultados.

Las hipótesis del informe no prueban que estos daños hayan ocurrido. Esta lista
explica cómo evitar los fallos observados y qué comprobar antes de dar cada
mejora por terminada.
