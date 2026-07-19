# Imagen del sandbox de código del AI Broker (skill run_code).
#
# python:3.12-slim + librerías de análisis de datos, para que el agente pueda
# procesar los datos de documentos adjuntos (CSV/XLSX convertidos a Markdown)
# sin salir del aislamiento. Sin compiladores ni herramientas de red: la
# imagen no las necesita y su ausencia reduce superficie.
#
# Construcción (desde la raíz del proyecto):
#   docker build -t ai-broker-sandbox:latest sandbox/
# y en broker_config.yaml:
#   sandbox.image: ai-broker-sandbox:latest
FROM python:3.12-slim

RUN pip install --no-cache-dir \
        numpy \
        pandas \
        matplotlib \
        openpyxl \
        python-dateutil \
    && rm -rf /root/.cache

# matplotlib sin display: backend Agg por defecto dentro del contenedor.
ENV MPLBACKEND=Agg

# El ejecutor fuerza --user 65534, --network none, --read-only y tmpfs /work;
# nada de eso se define aquí para que la imagen no dé sensación de seguridad
# que en realidad impone el broker en tiempo de ejecución.
WORKDIR /work
CMD ["python"]
