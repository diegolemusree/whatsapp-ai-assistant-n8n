"""Cliente mínimo de la API pública de n8n (tu instancia de n8n Cloud o self-host).

La llave se lee al momento desde la configuración de n8n-mcp en ~/.claude.json
(o, si existen, de N8N_API_KEY / N8N_API_URL en .env). Nunca se imprime ni se guarda.

Uso por línea de comandos:
  python3 tools/n8n_api.py respaldar <id> [<id> ...]   -> guarda el JSON en .tmp/respaldos/<fecha>/
  python3 tools/n8n_api.py ver <id>                     -> resumen (nombre, activo, versión, nodos)
"""
import json
import os
import sys
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent


def _credenciales():
    env = RAIZ / ".env"
    datos = {}
    if env.exists():
        for linea in env.read_text().splitlines():
            if "=" in linea and not linea.strip().startswith("#"):
                k, v = linea.split("=", 1)
                datos[k.strip()] = v.strip().strip('"').strip("'")
    if datos.get("N8N_API_KEY") and datos.get("N8N_API_URL"):
        return datos["N8N_API_URL"], datos["N8N_API_KEY"]
    cfg = json.loads((Path.home() / ".claude.json").read_text())
    e = cfg["mcpServers"]["n8n-mcp"]["env"]
    return e["N8N_API_URL"], e["N8N_API_KEY"]


def _base():
    url, _ = _credenciales()
    url = url.rstrip("/")
    return url if url.endswith("/api/v1") else url + "/api/v1"


def pedir(metodo, ruta, cuerpo=None):
    """Hace una petición a la API y regresa el JSON. Lanza RuntimeError con el mensaje de n8n si falla."""
    _, llave = _credenciales()
    datos = json.dumps(cuerpo).encode() if cuerpo is not None else None
    req = urllib.request.Request(_base() + ruta, data=datos, method=metodo)
    req.add_header("X-N8N-API-KEY", llave)
    req.add_header("Accept", "application/json")
    if datos is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            texto = r.read().decode()
            return json.loads(texto) if texto else {}
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"{metodo} {ruta} -> {e.code}: {e.read().decode()[:800]}") from None


def obtener(wid):
    return pedir("GET", f"/workflows/{wid}")


def crear(workflow):
    return pedir("POST", "/workflows", workflow)


def actualizar(wid, workflow):
    return pedir("PUT", f"/workflows/{wid}", workflow)


def activar(wid):
    return pedir("POST", f"/workflows/{wid}/activate")


def desactivar(wid):
    return pedir("POST", f"/workflows/{wid}/deactivate")


def para_guardar(wf):
    """Deja solo los campos que la API acepta al crear o actualizar un workflow."""
    permitidos = {"executionOrder", "errorWorkflow", "callerPolicy", "availableInMCP", "saveDataErrorExecution",
                  "saveDataSuccessExecution", "saveManualExecutions", "saveExecutionProgress", "timezone",
                  "executionTimeout", "binaryMode", "timeSavedMode"}
    cuerpo = {
        "name": wf["name"],
        "nodes": wf["nodes"],
        "connections": wf["connections"],
        "settings": {k: v for k, v in (wf.get("settings") or {}).items() if k in permitidos},
    }
    if wf.get("staticData"):
        cuerpo["staticData"] = wf["staticData"]
    return cuerpo


def respaldar(ids):
    carpeta = RAIZ / ".tmp" / "respaldos" / datetime.now().strftime("%Y-%m-%d_%H%M")
    carpeta.mkdir(parents=True, exist_ok=True)
    for wid in ids:
        wf = obtener(wid)
        nombre = wf["name"].replace("/", "-").replace("·", "-")
        (carpeta / f"{wid} {nombre}.json").write_text(json.dumps(wf, ensure_ascii=False, indent=2))
        print(f"{wid}  versión {wf.get('versionId')}  activo={wf.get('active')}  {len(wf['nodes'])} nodos  {wf['name']}")
    print(f"Respaldos en: {carpeta}")


if __name__ == "__main__":
    if len(sys.argv) < 3:
        print(__doc__)
        sys.exit(1)
    accion, args = sys.argv[1], sys.argv[2:]
    if accion == "respaldar":
        respaldar(args)
    elif accion == "ver":
        wf = obtener(args[0])
        print(json.dumps({k: wf.get(k) for k in ("id", "name", "active", "versionId", "activeVersionId")}, ensure_ascii=False))
        print(len(wf["nodes"]), "nodos; grupos:", [g.get("name") for g in wf.get("nodeGroups") or []])
    else:
        print(__doc__)
        sys.exit(1)
