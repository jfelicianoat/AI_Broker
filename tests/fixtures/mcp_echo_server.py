"""Servidor MCP mínimo sobre stdio, para los tests del cliente.

Habla JSON-RPC 2.0 por líneas: `initialize`, `tools/list` y `tools/call`.
Publica dos herramientas, una que responde y otra que devuelve un error de
herramienta (`isError`), que es un caso distinto de un error de protocolo.

Escribe además una línea que NO es JSON antes de la primera respuesta: muchos
servidores reales ensucian stdout con avisos y el cliente tiene que sobrevivir
a eso en vez de romper el handshake.
"""
import json
import sys

TOOLS = [
    {
        "name": "echo",
        "description": "Devuelve el texto recibido",
        "inputSchema": {
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "required": ["text"],
        },
    },
    {
        "name": "explota",
        "description": "Devuelve siempre un error de herramienta",
        "inputSchema": {"type": "object", "properties": {}},
    },
]


def send(message):
    sys.stdout.write(json.dumps(message, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def main() -> None:
    noisy = False
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            request = json.loads(line)
        except ValueError:
            continue
        method = request.get("method")
        request_id = request.get("id")
        if request_id is None:
            continue  # notificación: no se responde
        if not noisy:
            # Ruido deliberado en stdout antes de la primera respuesta.
            sys.stdout.write("cargando servidor de prueba...\n")
            sys.stdout.flush()
            noisy = True
        if method == "initialize":
            send({"jsonrpc": "2.0", "id": request_id, "result": {
                "protocolVersion": "2024-11-05",
                "serverInfo": {"name": "echo", "version": "1.0"},
                "capabilities": {"tools": {}},
            }})
        elif method == "tools/list":
            send({"jsonrpc": "2.0", "id": request_id, "result": {"tools": TOOLS}})
        elif method == "tools/call":
            params = request.get("params") or {}
            name = params.get("name")
            arguments = params.get("arguments") or {}
            if name == "echo":
                send({"jsonrpc": "2.0", "id": request_id, "result": {
                    "content": [{"type": "text", "text": f"eco: {arguments.get('text', '')}"}],
                }})
            elif name == "explota":
                send({"jsonrpc": "2.0", "id": request_id, "result": {
                    "content": [{"type": "text", "text": "la herramienta falló"}],
                    "isError": True,
                }})
            else:
                send({"jsonrpc": "2.0", "id": request_id, "error": {
                    "code": -32602, "message": f"herramienta desconocida: {name}",
                }})
        else:
            send({"jsonrpc": "2.0", "id": request_id, "error": {
                "code": -32601, "message": f"método no soportado: {method}",
            }})


if __name__ == "__main__":
    main()
