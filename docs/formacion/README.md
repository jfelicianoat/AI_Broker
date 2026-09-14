# AI Broker — Material Completo para Formación Audiovisual

Bienvenido al repositorio de material formativo oficial para el **AI Broker**. Este directorio reúne todos los documentos, guiones técnicos, contenidos de diapositivas y pautas de demostración en vivo sobre las pantallas originales de la aplicación web (`http://127.0.0.1:8765/dashboard`).

---

## Estructura del Material Formativo

| Archivo | Contenido Principal | Pantallas Cubiertas |
|---|---|---|
| [**`00_mapa_casos_de_uso.md`**](file:///c:/Procesos/AI_Broker/docs/formacion/00_mapa_casos_de_uso.md) | **Inventario y Clasificación Exhaustiva**<br>• Arquitectura y principios de diseño ("cero métricas simuladas", fail-closed).<br>• 15 casos de uso categorizados en 8 bloques operativos.<br>• Mapeo exacto a componentes, rutas y fragmentos de código. | Todas las pantallas del Broker |
| [**`01_modulo_1_y_2_guion_diapositivas.md`**](file:///c:/Procesos/AI_Broker/docs/formacion/01_modulo_1_y_2_guion_diapositivas.md) | **Módulos 1 y 2 (Fundamentos e Inferencia)**<br>• Módulo 1: Introducción, Filosofía y Arquitectura (Doble carril, VRAM, Login).<br>• Módulo 2: Probador de Prompts, Estrategia Single, Compresión de Prompts, JSON Schema y Meta-Router (`auto`). | • Resumen (`/dashboard`)<br>• Probador (`/dashboard/prompt-tester`)<br>• Enrutamiento (`/dashboard/routing`)<br>• Detalle de tarea |
| [**`02_modulo_3_y_4_guion_diapositivas.md`**](file:///c:/Procesos/AI_Broker/docs/formacion/02_modulo_3_y_4_guion_diapositivas.md) | **Módulos 3 y 4 (Multimodalidad y Consenso)**<br>• Módulo 3: Ingesta Documental, OCR de PDFs con Docling, Transcripción con Faster-Whisper, Imágenes y Map-Reduce.<br>• Módulo 4: Deliberación Multi-Modelo (MoA), Presets Fast y Slow con VRAM por oleadas, Comparador de tiempos reales y resiliencia de árbitro. | • Ficheros (`/dashboard/files`)<br>• Visores Markdown e Imagen<br>• Comparación (`/dashboard/comparison`) |
| [**`03_modulo_5_y_6_guion_diapositivas.md`**](file:///c:/Procesos/AI_Broker/docs/formacion/03_modulo_5_y_6_guion_diapositivas.md) | **Módulos 5 y 6 (Agentes, Hardware y Operación)**<br>• Módulo 5: Agentes con Tools (`web_search`, `fetch_url` con SSRF), Sandbox Docker aislado (`run_code`) y Galería de Artefactos.<br>• Módulo 6: Gestión y reordenación de cola, Histórico forense, Catálogo y sondeo de modelos, Borrado en dos pasos y Edición de Configuración en caliente. | • Tareas y Cola (`/dashboard/tasks`)<br>• Histórico (`/dashboard/history`)<br>• Modelos (`/dashboard/models`)<br>• Configuración (`/dashboard/config`) |

---

## Formato Estándar de Cada Sesión de Vídeo
Cada módulo incluye para cada una de sus diapositivas y tomas de vídeo:
1. **Título y texto proyectado** para las diapositivas.
2. **Pantalla original y ruta exacta** de la app que debe capturarse o proyectarse.
3. **Guion locutado paso a paso** (texto exacto para el presentador o voz en off).
4. **Acciones de demostración en vivo** (movimientos de ratón, selecciones, clics y validaciones en la interfaz).
