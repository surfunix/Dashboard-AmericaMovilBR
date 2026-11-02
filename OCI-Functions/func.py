"""
OCI Function: dashboard-publish
Recebe o estado do Dashboard FY27 (POST via API Gateway) e grava no Object Storage:
  - data/dashboard-fy27.json                      (versão corrente lida pela view)
  - data/history/dashboard-fy27-<UTC>.json        (histórico de cada publicação)

Configuração da Function (fn config / Console > Configuration):
  PUBLISH_TOKENS  lista "nome:token" separada por vírgula, ex.: marcus:abc123,backup:def456
                  (cada pessoa com seu token; revogar = remover da lista, sem redeploy)
  PUBLISH_TOKEN   alternativa legada: um único token (nome registrado: "gestao")
  BUCKET_NAME     default: dashboard-claro
  OBJECT_NAME     default: data/dashboard-fy27.json
  HISTORY_PREFIX  default: data/history/
  NAMESPACE       opcional (descoberto automaticamente se vazio)

Autenticação no Object Storage: Resource Principal (dynamic group + policy).
"""
import io
import json
import hmac
import logging
from datetime import datetime, timezone

import oci
from fdk import response

REQUIRED_ARRAYS = ("increments", "decrements", "actuals", "commitments")
MAX_BODY_BYTES = 2 * 1024 * 1024  # 2 MB, folga grande para o JSON do dashboard

_client = None
_namespace = None


def _object_storage(cfg):
    global _client, _namespace
    if _client is None:
        signer = oci.auth.signers.get_resource_principals_signer()
        _client = oci.object_storage.ObjectStorageClient(config={}, signer=signer)
    if _namespace is None:
        _namespace = cfg.get("NAMESPACE") or _client.get_namespace().data
    return _client, _namespace


def _header(ctx, name):
    """Busca header de forma tolerante (API Gateway pode prefixar com Fn-Http-H-)."""
    wanted = {name.lower(), ("fn-http-h-" + name).lower()}
    for key, value in (ctx.Headers() or {}).items():
        if key.lower() in wanted:
            return value[0] if isinstance(value, list) else value
    return None


def _reply(ctx, status, body):
    return response.Response(
        ctx,
        response_data=json.dumps(body, ensure_ascii=False),
        headers={"Content-Type": "application/json"},
        status_code=status,
    )


def _authorize(cfg, received):
    """Retorna o nome associado ao token recebido, ou None."""
    tokens = {}
    for item in (cfg.get("PUBLISH_TOKENS") or "").split(","):
        if ":" in item:
            name, token = item.split(":", 1)
            if name.strip() and token.strip():
                tokens[name.strip()] = token.strip()
    if cfg.get("PUBLISH_TOKEN"):
        tokens.setdefault("gestao", cfg["PUBLISH_TOKEN"].strip())
    match = None
    for name, token in tokens.items():
        if received and hmac.compare_digest(token, received):
            match = name
    return match


def _valid_state(state):
    return (
        isinstance(state, dict)
        and all(isinstance(state.get(k), list) for k in REQUIRED_ARRAYS)
        and isinstance(state.get("manual"), dict)
    )


def handler(ctx, data: io.BytesIO = None):
    cfg = dict(ctx.Config() or {})

    # 1) Autorização
    publisher = _authorize(cfg, _header(ctx, "X-Publish-Token") or "")
    if not publisher:
        return _reply(ctx, 401, {"message": "Token de publicação inválido."})

    # 2) Payload
    raw = data.getvalue() if data else b""
    if not raw or len(raw) > MAX_BODY_BYTES:
        return _reply(ctx, 400, {"message": "Corpo vazio ou maior que o permitido."})
    try:
        body = json.loads(raw)
    except ValueError:
        return _reply(ctx, 400, {"message": "JSON inválido."})

    state = body.get("state") if isinstance(body, dict) else None
    force = bool(body.get("force")) if isinstance(body, dict) else False
    if not _valid_state(state):
        return _reply(ctx, 400, {"message": "Formato de dados do dashboard inválido."})

    bucket = cfg.get("BUCKET_NAME", "dashboard-claro")
    object_name = cfg.get("OBJECT_NAME", "data/dashboard-fy27.json")
    history_prefix = cfg.get("HISTORY_PREFIX", "data/history/")

    try:
        client, namespace = _object_storage(cfg)

        # 3) Controle de concorrência otimista: o rascunho precisa partir da versão central atual
        if not force:
            try:
                current = json.loads(client.get_object(namespace, bucket, object_name).data.content)
                current_version = current.get("publishedAt")
                if current_version and state.get("basePublishedAt") != current_version:
                    return _reply(ctx, 409, {
                        "message": "Alguém publicou uma versão depois da que você abriu.",
                        "currentPublishedAt": current_version,
                        "currentPublishedBy": current.get("publishedBy"),
                    })
            except oci.exceptions.ServiceError as err:
                if err.status != 404:
                    raise

        # 4) Gravação: histórico primeiro, depois a versão corrente
        now = datetime.now(timezone.utc)
        state["publishedAt"] = now.isoformat(timespec="milliseconds").replace("+00:00", "Z")
        state["publishedBy"] = publisher
        state.pop("basePublishedAt", None)
        payload = json.dumps(state, ensure_ascii=False).encode("utf-8")
        history_object = f"{history_prefix}dashboard-fy27-{now.strftime('%Y%m%dT%H%M%SZ')}.json"

        client.put_object(namespace, bucket, history_object, payload,
                          content_type="application/json; charset=utf-8")
        client.put_object(namespace, bucket, object_name, payload,
                          content_type="application/json; charset=utf-8",
                          cache_control="no-cache, no-store, must-revalidate")

        logging.getLogger().info("Dashboard publicado por %s: %s (%d bytes)", publisher, object_name, len(payload))
        return _reply(ctx, 200, {
            "message": "Publicado com sucesso.",
            "publishedAt": state["publishedAt"],
            "publishedBy": publisher,
            "object": object_name,
            "historyObject": history_object,
        })
    except Exception as exc:  # noqa: BLE001
        logging.getLogger().exception("Falha na publicação")
        return _reply(ctx, 500, {"message": f"Falha ao gravar no Object Storage: {exc}"})
