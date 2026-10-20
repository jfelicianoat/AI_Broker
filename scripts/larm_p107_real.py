"""Prueba real P107: cliente → Broker → LARM → runtime local → artefacto.

Monta una instancia AISLADA del broker (su propio directorio, base de datos,
puerto 8766 y token de administración; no publica nada en el keyring del
broker de producción) y un larm-daemon con el puente Broker–LARM sobre el
runtime local adoptado (Ollama en loopback). Luego hace de cliente:

1. tarea con target exacto `larm/local/<variant_id>`, `fallback_allowed:
   false`, semilla 42 y temperatura 0: debe completarse, `served_by` debe ser
   exactamente esa variante, debe existir un único artefacto final y su
   SHA-256 debe casar con los bytes descargados; la lease debe haberse
   renovado durante la inferencia y quedar liberada;
2. tarea cancelada a mitad de inferencia: la lease debe quedar liberada;
3. variante desconocida con fallback prohibido: falla sin tocar otro modelo.

Toda la evidencia (peticiones, respuestas, estado de leases en LARM, hashes)
se escribe en --out. El token del principal se genera aquí, viaja a LARM
solo por stdin (`enroll-principal`) y al broker solo por el entorno del
proceso hijo; al terminar se borra del Credential Manager.

Uso:
    python scripts/larm_p107_real.py --larm-repo "<ruta>/larm" --model gemma4:12b --out <dir>
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import secrets
import shutil
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx

BROKER_REPO = Path(__file__).resolve().parent.parent
LARM_PORT = 8790
BROKER_PORT = 8766
PRINCIPAL = "ai-broker"
TOKEN_ENV = "LARM_BROKER_TOKEN"
ADMIN_ENV = "AI_BROKER_P107_ADMIN_TOKEN"


def now() -> str:
    moment = datetime.now(timezone.utc)
    return moment.strftime("%Y-%m-%dT%H:%M:%S.") + f"{moment.microsecond // 1000:03d}Z"


def uuid7() -> str:
    maker = getattr(uuid, "uuid7", None)
    if maker is not None:
        return str(maker())
    millis = int(time.time() * 1000)
    rand = secrets.randbits(74)
    value = (millis << 80) | (0x7 << 76) | (((rand >> 62) & 0xFFF) << 64) | (0b10 << 62) | (rand & ((1 << 62) - 1))
    text = f"{value:032x}"
    return f"{text[:8]}-{text[8:12]}-{text[12:16]}-{text[16:20]}-{text[20:]}"


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class Evidence:
    def __init__(self, out: Path) -> None:
        self.out = out
        self.log: list[dict[str, Any]] = []
        self.checks: list[dict[str, Any]] = []

    def save(self, name: str, payload: Any) -> Path:
        path = self.out / name
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        return path

    def record(self, step: str, **data: Any) -> None:
        self.log.append({"at": now(), "step": step, **data})

    def check(self, check_id: str, expected: str, observed: str, ok: bool, *paths: str) -> bool:
        self.checks.append({
            "id": check_id, "expected": expected, "observed": observed,
            "result": "PASS" if ok else "FAIL", "evidence_paths": list(paths),
        })
        print(f"[{'PASS' if ok else 'FAIL'}] {check_id}: {observed}", flush=True)
        return ok


def wait_http(url: str, timeout: float, headers: dict[str, str] | None = None) -> None:
    deadline = time.monotonic() + timeout
    last: Exception | None = None
    while time.monotonic() < deadline:
        try:
            if httpx.get(url, headers=headers, timeout=2).status_code < 500:
                return
        except httpx.HTTPError as error:
            last = error
        time.sleep(0.5)
    raise RuntimeError(f"{url} no respondió en {timeout:.0f} s: {last}")


def larm_daemon(larm_repo: Path) -> Path:
    exe = larm_repo / "target" / "debug" / ("larm-daemon.exe" if os.name == "nt" else "larm-daemon")
    if not exe.exists():
        raise RuntimeError(f"No existe {exe}: compila con `cargo build -p larm-daemon`")
    return exe


def enroll(exe: Path, token: str) -> None:
    result = subprocess.run(
        [str(exe), "enroll-principal", "--name", PRINCIPAL],
        input=token + "\n", capture_output=True, text=True, timeout=60,
    )
    if result.returncode != 0:
        raise RuntimeError(f"enroll-principal falló: {result.stderr.strip()}")


def unenroll() -> None:
    """Borra la credencial de prueba del principal en LARM (formato keyring Rust:
    target `principal:ai-broker.larm`)."""
    if os.name != "nt":
        return
    subprocess.run(["cmdkey", f"/delete:principal:{PRINCIPAL}.larm"], capture_output=True, timeout=30)


def bridge_config(out: Path, model: str, variant_id: str, budget_gib: int) -> Path:
    logical = os.cpu_count() or 2
    config = {
        "adoption": {
            "host_id": uuid7(),
            "runtime": "Ollama",
            "base_url": "http://127.0.0.1:11434",
            "accept_external_local_trusted": True,
            "runtime_version_id": uuid7(),
            "memory_budget_bytes": str(budget_gib * 1024**3),
            "gateway_base_url": f"http://127.0.0.1:{LARM_PORT}/api/v1/gateway",
            "launch_profile": {
                "id": uuid7(),
                "created_at": now(),
                "updated_at": now(),
                "row_version": 1,
                "metadata": {"source": "external_adoption", "applied_by_runtime": False},
                "name": "BALANCED",
                "parameters": {
                    "threads": max(1, logical - 1), "context": 4096, "batch": 1, "concurrency": 1,
                    "offload_layers": None, "backend_id": uuid7(), "tokenizer_hash": None,
                    "chat_template_hash": None,
                    "generation": {"temperature": 0.0, "top_p": 1.0, "seed": 42, "max_output_tokens": 512},
                    "memory_limit_bytes": None, "runtime_arguments": [],
                    "environment": {"allowlist": [], "variables": []}, "adapter_options": {},
                },
                "profile_hash": "0" * 64,
                "backend_id": None,
            },
            "variants": [{"variant_id": variant_id, "model": model, "capabilities": ["text.generate"]}],
        },
        "principals": [{"name": PRINCIPAL, "scopes": ["read", "leases:manage"]}],
    }
    profile = config["adoption"]["launch_profile"]
    profile["profile_hash"] = hashlib.sha256(
        json.dumps(profile["parameters"], sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    path = out / "larm_bridge_config.json"
    path.write_text(json.dumps(config, indent=2), encoding="utf-8")
    return path


def broker_config(out: Path) -> Path:
    config = f"""\
server:
  host: 127.0.0.1
  port: {BROKER_PORT}
  admin_token_env: {ADMIN_ENV}
  admin_keyring_service: ai-broker-p107-real-test
  publish_session_token: none
persistence:
  database: {(out / 'broker' / 'broker.db').as_posix()}
processing:
  auto_dispatch: true
ingestion:
  enabled: false
sandbox:
  enabled: false
mcp:
  enabled: false
system1:
  enabled: false
shadow_probe:
  enabled: false
strategy_router:
  enabled: false
providers:
  ollama:
    enabled: false
larm:
  enabled: true
  endpoint: http://127.0.0.1:{LARM_PORT}
  api_key_env: {TOKEN_ENV}
  keyring_service: larm-p107-real-test-unused
  lease_ttl_seconds: 30
  job_poll_seconds: 1
"""
    path = out / "broker_p107.yaml"
    path.write_text(config, encoding="utf-8")
    return path


def start_broker(out: Path, config: Path, env: dict[str, str]) -> subprocess.Popen[bytes]:
    workdir = out / "broker"
    workdir.mkdir(parents=True, exist_ok=True)
    launcher = (
        "import sys, uvicorn\n"
        f"sys.path.insert(0, {str(BROKER_REPO)!r})\n"
        "from app.config import load_config\n"
        "from app.main import create_app\n"
        f"config = load_config({str(config)!r})\n"
        f"uvicorn.run(create_app(config, config_path={str(config)!r}), host='127.0.0.1', port={BROKER_PORT}, log_level='warning')\n"
    )
    log = (workdir / "broker_stdout.log").open("wb")
    return subprocess.Popen(
        [sys.executable, "-c", launcher], cwd=workdir, env=env, stdout=log, stderr=subprocess.STDOUT,
    )


def task(variant_id: str, key: str, prompt: str, max_tokens: int) -> dict[str, Any]:
    return {
        "idempotency_key": key,
        "content": {"prompt": prompt},
        "model_requirements": {
            "target_model": {"provider": "larm", "deployment": "local", "model": variant_id},
            "fallback_allowed": False,
        },
        "generation": {"temperature": 0, "seed": 42, "max_output_tokens": max_tokens},
        "execution": {"strategy": "single", "preset": "fast"},
        "risk": {"data_classification": "local_only"},
        "exclude_from_model_learning": True,
        "auxiliary_invocations": False,
        "prompt_compression": "off",
    }


def poll(client: httpx.Client, task_id: str, timeout: float) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        state = client.get(f"/api/v1/tasks/{task_id}").json()
        if state["status"] in {"completed", "failed", "cancelled"}:
            return state
        time.sleep(2)
    raise RuntimeError(f"La tarea {task_id} no terminó en {timeout:.0f} s")


def larm_leases(token: str) -> list[dict[str, Any]]:
    response = httpx.get(f"http://127.0.0.1:{LARM_PORT}/api/v1/leases", headers={"Authorization": f"Bearer {token}"}, timeout=10)
    response.raise_for_status()
    return response.json()["items"]


def run(args: argparse.Namespace) -> int:
    out: Path = args.out.resolve()
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)
    evidence = Evidence(out)
    exe = larm_daemon(args.larm_repo)
    token = secrets.token_hex(32)
    admin = secrets.token_urlsafe(24)
    variant_id = uuid7()
    unknown_variant = uuid7()
    evidence.record("environment", host=platform.node(), os=platform.platform(), python=sys.version.split()[0],
                    larm_daemon=str(exe), larm_daemon_sha256=sha256_file(exe), model=args.model,
                    variant_id=variant_id, ollama_version=httpx.get("http://127.0.0.1:11434/api/version", timeout=5).json())
    enroll(exe, token)
    evidence.record("principal_enrolled", principal=PRINCIPAL, channel="stdin -> Windows Credential Manager")
    larm_config = bridge_config(out, args.model, variant_id, args.budget_gib)
    broker_cfg = broker_config(out)
    env = dict(os.environ)
    env[TOKEN_ENV] = token
    env[ADMIN_ENV] = admin
    env["PYTHONIOENCODING"] = "utf-8"
    larm_log = (out / "larm_stdout.log").open("wb")
    larm_proc = subprocess.Popen(
        [str(exe), "--bridge-config", str(larm_config), "--data-root", str(out / "larm-data"),
         "--port", str(LARM_PORT), "--log-format", "json", "--log-level", "info"],
        stdout=larm_log, stderr=subprocess.STDOUT,
        creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
    )
    broker_proc: subprocess.Popen[bytes] | None = None
    try:
        wait_http(f"http://127.0.0.1:{LARM_PORT}/api/v1/health/live", 30)
        broker_proc = start_broker(out, broker_cfg, env)
        wait_http(f"http://127.0.0.1:{BROKER_PORT}/health", 60)
        client = httpx.Client(base_url=f"http://127.0.0.1:{BROKER_PORT}", headers={"X-Admin-Token": admin}, timeout=30)

        capabilities = client.get("/api/v1/capabilities").json()
        cap_path = evidence.save("broker_capabilities.json", capabilities)
        evidence.check(
            "P107-REAL-01-capabilities",
            "local_runtime_resolver=true, larm_contract_version=1.0, contract_version=2.11",
            f"local_runtime_resolver={capabilities.get('local_runtime_resolver')}, "
            f"larm_contract_version={capabilities.get('larm_contract_version')}, contract_version={capabilities.get('contract_version')}",
            capabilities.get("local_runtime_resolver") is True and capabilities.get("larm_contract_version") == "1.0"
            and capabilities.get("contract_version") == "2.11",
            cap_path.name,
        )
        catalog = httpx.get(f"http://127.0.0.1:{LARM_PORT}/api/v1/broker/catalog",
                            headers={"Authorization": f"Bearer {token}"}, timeout=30).json()
        catalog_path = evidence.save("larm_catalog.json", catalog)
        evidence.check(
            "P107-REAL-02-catalog",
            "la variante adoptada aparece como BROKER_ELIGIBLE con huella verificada",
            f"items={[item['variant_id'] for item in catalog['items']]}",
            [item["variant_id"] for item in catalog["items"]] == [variant_id],
            catalog_path.name,
        )

        # 1. Inferencia real bajo lease, larga para observar renovaciones (TTL 30 s → 10 s).
        prompt = ("Escribe una explicación detallada, en español y en unas 600 palabras, de cómo funciona "
                  "una caché LRU, con un ejemplo paso a paso.")
        accepted = client.post("/api/v1/tasks", json=task(variant_id, f"p107-real-{uuid7()}", prompt, args.max_tokens))
        evidence.record("task_accepted", status=accepted.status_code, body=accepted.json())
        task_id = accepted.json()["task_id"]
        state = poll(client, task_id, args.timeout)
        state_path = evidence.save("task_completed_state.json", state)
        invocations = client.get(f"/api/v1/tasks/{task_id}/invocations").json()
        inv_path = evidence.save("task_invocations.json", invocations)
        artifacts = client.get(f"/api/v1/tasks/{task_id}/artifacts").json()
        art_path = evidence.save("task_artifacts.json", artifacts)
        leases_after = larm_leases(token)
        leases_path = evidence.save("larm_leases_after_task.json", leases_after)

        summary = state.get("execution_summary") or {}
        served = summary.get("served_by") or {}
        evidence.check(
            "P107-REAL-03-served-by-exact",
            f"status=completed y served_by=larm/local/{variant_id} sin fallback",
            f"status={state.get('status')}, served_by={served.get('provider')}/{served.get('deployment')}/{served.get('model')}, "
            f"fallback_used={summary.get('fallback_used')}, error={state.get('error')}",
            state.get("status") == "completed" and (served.get("provider"), served.get("deployment"), served.get("model"))
            == ("larm", "local", variant_id) and summary.get("fallback_used") is False,
            state_path.name,
        )
        finals = [item for item in artifacts.get("items", []) if item.get("final")]
        artifact_ok = False
        observed_artifact = f"finales={len(finals)}"
        if len(finals) == 1 and finals[0].get("available"):
            data = client.get(finals[0]["download_url"]).content
            (out / "final_artifact.bin").write_bytes(data)
            digest = hashlib.sha256(data).hexdigest()
            artifact_ok = digest == finals[0].get("sha256")
            observed_artifact += f", sha256 descargado={digest}, declarado={finals[0].get('sha256')}, bytes={len(data)}"
        evidence.check(
            "P107-REAL-04-canonical-artifact",
            "exactamente un artefacto final disponible y su SHA-256 casa con los bytes descargados",
            observed_artifact, artifact_ok, art_path.name, "final_artifact.bin",
        )
        contractual = [item for item in invocations.get("items", []) if item.get("contractual")]
        fp = (contractual[0].get("execution_fingerprint") or {}) if contractual else {}
        extension = (fp.get("extensions") or {}).get("larm.v1") or {}
        evidence.check(
            "P107-REAL-05-fingerprint-extension",
            "invocación contractual con extensión larm.v1 source=lease, variante exacta y backend efectivo verificado",
            f"invocaciones contractuales={len(contractual)}, source={extension.get('source')}, variant={extension.get('variant_id')}, "
            f"backend={((extension.get('components') or {}).get('backend') or {})}, generation={contractual[0].get('generation') if contractual else None}",
            len(contractual) == 1 and extension.get("source") == "lease" and extension.get("variant_id") == variant_id
            and ((extension.get("components") or {}).get("backend") or {}).get("status") == "VERIFIED",
            inv_path.name,
        )
        lease = next((item for item in leases_after if contractual and item.get("request_id", "").endswith(contractual[0]["invocation_id"])), None)
        evidence.check(
            "P107-REAL-06-lease-renewed-and-released",
            "la lease de la invocación se renovó (row_version > 3) y quedó RELEASED",
            f"lease={lease and lease['id']}, state={lease and lease['state']}, row_version={lease and lease['row_version']}, "
            f"request_id={lease and lease['request_id']}",
            bool(lease) and lease["state"] == "RELEASED" and lease["row_version"] > 3
            and lease["request_id"] == f"{task_id}:{contractual[0]['invocation_id']}",
            leases_path.name,
        )

        # 2. Cancelación a mitad de inferencia.
        cancel_prompt = "Enumera del 1 al 2000, un número por línea, sin comentarios."
        accepted = client.post("/api/v1/tasks", json=task(variant_id, f"p107-cancel-{uuid7()}", cancel_prompt, 4000))
        cancel_id = accepted.json()["task_id"]
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline:
            if any(item["state"] == "ACTIVE" for item in larm_leases(token)):
                break
            time.sleep(0.5)
        time.sleep(3)
        client.delete(f"/api/v1/tasks/{cancel_id}")
        cancelled = poll(client, cancel_id, 120)
        time.sleep(2)
        leases_cancel = larm_leases(token)
        cancel_path = evidence.save("larm_leases_after_cancel.json", leases_cancel)
        evidence.save("task_cancelled_state.json", cancelled)
        cancel_leases = [item for item in leases_cancel if item["request_id"].startswith(f"{cancel_id}:")]
        evidence.check(
            "P107-REAL-07-cancel-releases",
            "tarea cancelada y su lease RELEASED",
            f"status={cancelled.get('status')}, leases={[(i['id'], i['state']) for i in cancel_leases]}",
            cancelled.get("status") == "cancelled" and bool(cancel_leases)
            and all(item["state"] == "RELEASED" for item in cancel_leases),
            cancel_path.name, "task_cancelled_state.json",
        )

        # 3. Variante desconocida sin fallback.
        accepted = client.post("/api/v1/tasks", json=task(unknown_variant, f"p107-unknown-{uuid7()}", "Hola", 16))
        unknown = poll(client, accepted.json()["task_id"], 60)
        unknown_path = evidence.save("task_unknown_variant_state.json", unknown)
        evidence.check(
            "P107-REAL-08-unknown-variant-no-fallback",
            "failed con MODEL_UNAVAILABLE y sin invocar ningún modelo",
            f"status={unknown.get('status')}, code={(unknown.get('error') or {}).get('code')}",
            unknown.get("status") == "failed" and (unknown.get("error") or {}).get("code") == "MODEL_UNAVAILABLE",
            unknown_path.name,
        )
    finally:
        for proc in (broker_proc, larm_proc):
            if proc is not None and proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    proc.kill()
        larm_log.close()
        unenroll()
        evidence.record("cleanup", principal_credential_deleted=True)
        evidence.save("run_log.json", evidence.log)
        evidence.save("checks.json", evidence.checks)
        hashes = {path.name: sha256_file(path) for path in sorted(out.iterdir()) if path.is_file()}
        (out / "SHA256SUMS.json").write_text(json.dumps(hashes, indent=2), encoding="utf-8")
    passed = all(item["result"] == "PASS" for item in evidence.checks)
    print(f"\n{sum(c['result'] == 'PASS' for c in evidence.checks)}/{len(evidence.checks)} comprobaciones PASS -> {out}")
    return 0 if passed and evidence.checks else 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--larm-repo", type=Path, required=True)
    parser.add_argument("--model", required=True, help="Modelo YA presente en el runtime local (no se descarga nada)")
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--budget-gib", type=int, default=48)
    parser.add_argument("--max-tokens", type=int, default=1200)
    parser.add_argument("--timeout", type=float, default=900)
    return run(parser.parse_args())


if __name__ == "__main__":
    raise SystemExit(main())
