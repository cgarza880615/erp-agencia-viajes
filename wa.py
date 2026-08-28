import os
import logging
import re
from datetime import datetime

import httpx
from fastapi import APIRouter, Form, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from database import ejecutar_comando, now_local, obtener_datos

# ── Credenciales Meta WhatsApp Cloud API ──────────────────────────────────────
# Todas las credenciales vienen de variables de entorno — nunca las escribas
# aquí directamente. Copia .env.example a .env y llena tus propios valores
# (Meta Business Settings → Usuarios del sistema → Generar token).
WA_PHONE_NUMBER_ID = os.environ.get("WA_PHONE_NUMBER_ID", "")
WA_ACCESS_TOKEN    = os.environ.get("WA_ACCESS_TOKEN", "")
WA_VERIFY_TOKEN    = os.environ.get("WA_VERIFY_TOKEN", "")
WA_API_VERSION     = "v20.0"
WA_API_BASE        = f"https://graph.facebook.com/{WA_API_VERSION}"

wa_router = APIRouter()

# ── Helpers internos ──────────────────────────────────────────────────────────

def _templates():
    import main as _m
    return _m.templates

def _ctx(request):
    import main as _m
    return _m.ctx(request)

def _usuario(request):
    import main as _m
    return _m.usuario_activo(request)


async def _wa_send_text(to: str, text: str) -> bool:
    url = f"{WA_API_BASE}/{WA_PHONE_NUMBER_ID}/messages"
    headers = {"Authorization": f"Bearer {WA_ACCESS_TOKEN}", "Content-Type": "application/json"}
    payload = {"messaging_product": "whatsapp", "to": to, "type": "text", "text": {"body": text}}
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.post(url, json=payload, headers=headers)
        if r.status_code != 200:
            logging.error(f"WA send error {r.status_code}: {r.text}")
        return r.status_code == 200
    except Exception as e:
        logging.error(f"WA send exception: {e}")
        return False


def _upsert_conversacion(wa_id: str, nombre: str, ultimo_msg: str, ts: str) -> int:
    # No normalizar el wa_id (ej. quitarle el "1" legacy a móviles mexicanos):
    # Meta exige exactamente ese mismo formato al ENVIAR, o rechaza con
    # "(#131030) Recipient phone number not in allowed list" aunque el número
    # sea el correcto — hay que guardarlo tal cual lo entrega el webhook.
    existing = obtener_datos("SELECT id FROM wa_conversaciones WHERE wa_id = ?", (wa_id,))
    if existing.empty:
        ejecutar_comando(
            "INSERT INTO wa_conversaciones (wa_id, nombre, telefono, estado, ultimo_mensaje, ultimo_mensaje_ts) VALUES (?, ?, ?, 'NUEVO', ?, ?)",
            (wa_id, nombre, wa_id, ultimo_msg[:100] if ultimo_msg else "", ts),
        )
        row = obtener_datos("SELECT id FROM wa_conversaciones WHERE wa_id = ?", (wa_id,))
        return int(row.iloc[0]["id"])
    else:
        conv_id = int(existing.iloc[0]["id"])
        ejecutar_comando(
            "UPDATE wa_conversaciones SET nombre=COALESCE(NULLIF(?,?), nombre), ultimo_mensaje=?, ultimo_mensaje_ts=?, estado=CASE WHEN estado='RESUELTO' THEN 'NUEVO' ELSE estado END WHERE id=?",
            (nombre, wa_id, ultimo_msg[:100] if ultimo_msg else "", ts, conv_id),
        )
        return conv_id


def _insert_mensaje(conv_id: int, wa_msg_id: str | None, direccion: str, tipo: str,
                    contenido: str, ts: str, usuario: str = None, media_id: str = None):
    try:
        ejecutar_comando(
            "INSERT OR IGNORE INTO wa_mensajes (id_conversacion, wa_message_id, direccion, tipo, contenido, media_id, timestamp, usuario_envio) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (conv_id, wa_msg_id, direccion, tipo, contenido, media_id, ts, usuario),
        )
    except Exception as e:
        logging.error(f"WA insert_mensaje: {e}")


def _get_conv_context(conv_id: int) -> dict:
    conv = obtener_datos(
        """SELECT c.*, cl.nombre AS nombre_cliente,
                  ef.nombre AS etapa_nombre, ef.emoji AS etapa_emoji, ef.color AS etapa_color
           FROM wa_conversaciones c
           LEFT JOIN clientes cl ON c.id_cliente = cl.id_cliente
           LEFT JOIN wa_etapas_funnel ef ON c.id_etapa_funnel = ef.id
           WHERE c.id = ?""",
        (conv_id,),
    )
    if conv.empty:
        return {}
    mensajes  = obtener_datos(
        "SELECT * FROM wa_mensajes WHERE id_conversacion = ? ORDER BY timestamp ASC, id ASC LIMIT 300",
        (conv_id,),
    )
    clientes  = obtener_datos("SELECT id_cliente, nombre FROM clientes ORDER BY nombre")
    usuarios  = obtener_datos("SELECT usuario FROM usuarios ORDER BY usuario")
    etapas    = obtener_datos("SELECT * FROM wa_etapas_funnel WHERE activo=1 ORDER BY orden")
    etiquetas = obtener_datos("SELECT * FROM wa_etiquetas WHERE activo=1 ORDER BY orden")
    etiq_conv = obtener_datos(
        "SELECT id_etiqueta FROM wa_conv_etiquetas WHERE id_conversacion=?", (conv_id,)
    )
    etiq_ids  = set(etiq_conv["id_etiqueta"].tolist()) if not etiq_conv.empty else set()
    return {
        "conv":       conv.iloc[0].to_dict(),
        "mensajes":   mensajes.to_dict("records"),
        "clientes":   clientes.to_dict("records"),
        "usuarios":   usuarios.to_dict("records"),
        "etapas":     etapas.to_dict("records"),
        "etiquetas":  etiquetas.to_dict("records"),
        "etiq_ids":   etiq_ids,
    }


def _contar_wa_nuevas() -> int:
    df = obtener_datos("SELECT COUNT(*) AS n FROM wa_conversaciones WHERE estado='NUEVO'")
    return int(df.iloc[0]["n"]) if not df.empty else 0


# ── Webhook Meta ──────────────────────────────────────────────────────────────

@wa_router.get("/wa/webhook")
async def wa_webhook_verify(
    hub_mode: str = Query(None, alias="hub.mode"),
    hub_verify_token: str = Query(None, alias="hub.verify_token"),
    hub_challenge: str = Query(None, alias="hub.challenge"),
):
    if hub_mode == "subscribe" and hub_verify_token == WA_VERIFY_TOKEN:
        return Response(content=hub_challenge, media_type="text/plain")
    return Response(status_code=403)


@wa_router.post("/wa/webhook")
async def wa_webhook_receive(request: Request):
    try:
        body = await request.json()
        for entry in body.get("entry", []):
            for change in entry.get("changes", []):
                value = change.get("value", {})
                contacts = {
                    c["wa_id"]: c.get("profile", {}).get("name", "")
                    for c in value.get("contacts", [])
                }

                for msg in value.get("messages", []):
                    wa_id   = msg["from"]
                    msg_id  = msg.get("id", "")
                    ts_unix = msg.get("timestamp", "")
                    ts = (datetime.fromtimestamp(int(ts_unix)).strftime("%Y-%m-%d %H:%M:%S")
                          if ts_unix else str(now_local()))
                    tipo    = msg.get("type", "texto")
                    nombre  = contacts.get(wa_id, wa_id)

                    if tipo == "text":
                        contenido = msg.get("text", {}).get("body", "")
                        media_id  = None
                    elif tipo == "image":
                        contenido = "[📷 Imagen]"
                        media_id  = msg.get("image", {}).get("id")
                    elif tipo == "document":
                        fname     = msg.get("document", {}).get("filename", "Documento")
                        contenido = f"[📎 {fname}]"
                        media_id  = msg.get("document", {}).get("id")
                    elif tipo == "audio":
                        contenido = "[🎤 Audio de voz]"
                        media_id  = msg.get("audio", {}).get("id")
                    elif tipo == "video":
                        contenido = "[🎬 Video]"
                        media_id  = msg.get("video", {}).get("id")
                    elif tipo == "location":
                        loc       = msg.get("location", {})
                        contenido = f"[📍 Ubicación: {loc.get('name', '')}]"
                        media_id  = None
                    elif tipo == "sticker":
                        contenido = "[🎉 Sticker]"
                        media_id  = msg.get("sticker", {}).get("id")
                    else:
                        contenido = f"[{tipo}]"
                        media_id  = None

                    conv_id = _upsert_conversacion(wa_id, nombre, contenido, ts)
                    _insert_mensaje(conv_id, msg_id, "ENTRANTE", tipo, contenido, ts, media_id=media_id)

                for status in value.get("statuses", []):
                    estado_map = {"sent": "enviado", "delivered": "entregado", "read": "leido", "failed": "fallido"}
                    ejecutar_comando(
                        "UPDATE wa_mensajes SET estado=? WHERE wa_message_id=?",
                        (estado_map.get(status.get("status", ""), "enviado"), status.get("id")),
                    )
    except Exception as e:
        logging.error(f"WA webhook error: {e}")
    return {"status": "ok"}


# ── Inbox principal ───────────────────────────────────────────────────────────

@wa_router.get("/wa", response_class=HTMLResponse)
async def wa_inbox(request: Request):
    u = _usuario(request)
    if not u:
        return RedirectResponse("/login")
    c = _ctx(request)
    c["active"] = "wa"
    c["etapas"] = obtener_datos("SELECT * FROM wa_etapas_funnel WHERE activo=1 ORDER BY orden").to_dict("records")
    c["etiquetas"] = obtener_datos("SELECT * FROM wa_etiquetas WHERE activo=1 ORDER BY orden").to_dict("records")
    return _templates().TemplateResponse(request, "wa_inbox.html", c)


def _query_conversaciones(estado: str = "", q: str = "", etapa: str = "", etiqueta: str = ""):
    filtros, params = ["1=1"], []
    if estado:
        filtros.append("c.estado = ?")
        params.append(estado)
    if q:
        filtros.append("(c.nombre LIKE ? OR c.wa_id LIKE ? OR c.ultimo_mensaje LIKE ?)")
        params.extend([f"%{q}%", f"%{q}%", f"%{q}%"])
    if etapa:
        filtros.append("c.id_etapa_funnel = ?")
        params.append(int(etapa))
    if etiqueta:
        filtros.append("EXISTS (SELECT 1 FROM wa_conv_etiquetas ce2 WHERE ce2.id_conversacion = c.id AND ce2.id_etiqueta = ?)")
        params.append(int(etiqueta))

    where = " AND ".join(filtros)
    return obtener_datos(
        f"""SELECT c.*, cl.nombre AS nombre_cliente,
                   ef.nombre AS etapa_nombre, ef.emoji AS etapa_emoji, ef.color AS etapa_color,
                   GROUP_CONCAT(et.nombre || '::' || et.color, '||') AS etiquetas
            FROM wa_conversaciones c
            LEFT JOIN clientes cl ON c.id_cliente = cl.id_cliente
            LEFT JOIN wa_etapas_funnel ef ON c.id_etapa_funnel = ef.id
            LEFT JOIN wa_conv_etiquetas ce ON c.id = ce.id_conversacion
            LEFT JOIN wa_etiquetas et ON ce.id_etiqueta = et.id
            WHERE {where}
            GROUP BY c.id
            ORDER BY c.ultimo_mensaje_ts DESC NULLS LAST
            LIMIT 100""",
        tuple(params),
    )


@wa_router.get("/wa/rows", response_class=HTMLResponse)
async def wa_rows(request: Request, estado: str = "", q: str = "", etapa: str = "", etiqueta: str = ""):
    u = _usuario(request)
    if not u:
        return HTMLResponse("", status_code=401)
    convs = _query_conversaciones(estado, q, etapa, etiqueta)
    return _templates().TemplateResponse(request, "wa_conv_rows.html",
                                         {"request": request, "convs": convs.to_dict("records")})


@wa_router.post("/wa/nueva", response_class=HTMLResponse)
async def wa_nueva_conversacion(request: Request, numero: str = Form(...), nombre: str = Form("")):
    u = _usuario(request)
    if not u:
        return HTMLResponse("", status_code=401)

    wa_id = re.sub(r"\D", "", numero)
    if len(wa_id) < 10:
        return HTMLResponse(
            "<div class='wa-placeholder'><span>⚠️</span><p>Número inválido — escribe el número completo con código de país (ej. 528112345678).</p></div>",
            status_code=400,
        )

    existing = obtener_datos("SELECT id FROM wa_conversaciones WHERE wa_id = ?", (wa_id,))
    if not existing.empty:
        conv_id = int(existing.iloc[0]["id"])
    else:
        ejecutar_comando(
            "INSERT INTO wa_conversaciones (wa_id, nombre, telefono, estado, ultimo_mensaje, ultimo_mensaje_ts, agente_asignado) VALUES (?, ?, ?, 'ABIERTO', '', ?, ?)",
            (wa_id, nombre.strip() or wa_id, wa_id, str(now_local()), u),
        )
        row = obtener_datos("SELECT id FROM wa_conversaciones WHERE wa_id = ?", (wa_id,))
        conv_id = int(row.iloc[0]["id"])

    datos = _get_conv_context(conv_id)
    datos["request"] = request
    datos["usuario_actual"] = u
    datos["wa_error_msg"] = ""
    chat_html = _templates().env.get_template("wa_chat.html").render(datos)

    convs = _query_conversaciones()
    lista_html = _templates().env.get_template("wa_conv_rows.html").render(
        {"request": request, "convs": convs.to_dict("records")}
    )
    # Debe replicar EXACTOS los atributos hx-* del contenedor original en wa_inbox.html —
    # un swap OOB reemplaza el elemento completo (outerHTML), así que si se omiten aquí
    # se pierde el polling automático de cada 12s tras iniciar una conversación nueva.
    oob_lista = (
        '<div id="wa-conv-list" class="wa-conv-list" hx-swap-oob="true" '
        'hx-get="/wa/rows" hx-trigger="load, every 12s" hx-include="#wa-filtros">'
        f'{lista_html}</div>'
    )

    return HTMLResponse(chat_html + "\n" + oob_lista)


@wa_router.get("/wa/{conv_id}/chat", response_class=HTMLResponse)
async def wa_chat_get(request: Request, conv_id: int):
    u = _usuario(request)
    if not u:
        return HTMLResponse("", status_code=401)
    datos = _get_conv_context(conv_id)
    if not datos:
        return HTMLResponse("<p style='padding:16px'>Conversación no encontrada</p>", status_code=404)
    datos["request"]        = request
    datos["usuario_actual"] = u
    datos["wa_error_msg"] = ""
    return _templates().TemplateResponse(request, "wa_chat.html", datos)


@wa_router.get("/wa/{conv_id}/mensajes", response_class=HTMLResponse)
async def wa_mensajes_poll(request: Request, conv_id: int):
    """Polling ligero — solo los mensajes, sin tocar el form ni el panel lateral."""
    u = _usuario(request)
    if not u:
        return HTMLResponse("", status_code=401)
    mensajes = obtener_datos(
        "SELECT * FROM wa_mensajes WHERE id_conversacion=? ORDER BY timestamp ASC, id ASC LIMIT 300",
        (conv_id,),
    )
    return _templates().TemplateResponse(request, "wa_mensajes_partial.html",
                                         {"request": request, "mensajes": mensajes.to_dict("records"),
                                          "conv_id": conv_id})


@wa_router.post("/wa/{conv_id}/enviar", response_class=HTMLResponse)
async def wa_enviar(request: Request, conv_id: int, mensaje: str = Form("")):
    u = _usuario(request)
    if not u or not mensaje.strip():
        return HTMLResponse("", status_code=400)

    conv = obtener_datos("SELECT * FROM wa_conversaciones WHERE id = ?", (conv_id,))
    if conv.empty:
        return HTMLResponse("", status_code=404)

    wa_id = conv.iloc[0]["wa_id"]
    ts    = str(now_local())
    ok    = await _wa_send_text(wa_id, mensaje.strip())

    if ok:
        ejecutar_comando(
            "UPDATE wa_conversaciones SET ultimo_mensaje=?, ultimo_mensaje_ts=?, estado='ABIERTO' WHERE id=?",
            (mensaje.strip()[:100], ts, conv_id),
        )
        _insert_mensaje(conv_id, None, "SALIENTE", "texto", mensaje.strip(), ts, usuario=u)

    datos = _get_conv_context(conv_id)
    datos["request"]        = request
    datos["usuario_actual"] = u
    datos["wa_error_msg"] = "" if ok else "Error al enviar — verifica que el token de Meta siga vigente"
    return _templates().TemplateResponse(request, "wa_chat.html", datos)


@wa_router.post("/wa/{conv_id}/asignar", response_class=HTMLResponse)
async def wa_asignar(request: Request, conv_id: int, agente: str = Form("")):
    u = _usuario(request)
    if not u:
        return HTMLResponse("", status_code=401)
    ejecutar_comando(
        "UPDATE wa_conversaciones SET agente_asignado=?, estado=CASE WHEN estado='NUEVO' THEN 'ABIERTO' ELSE estado END WHERE id=?",
        (agente or None, conv_id),
    )
    datos = _get_conv_context(conv_id)
    datos["request"] = request
    datos["usuario_actual"] = u
    datos["wa_error_msg"] = ""
    return _templates().TemplateResponse(request, "wa_chat.html", datos)


@wa_router.post("/wa/{conv_id}/vincular-cliente", response_class=HTMLResponse)
async def wa_vincular_cliente(request: Request, conv_id: int, id_cliente: str = Form("")):
    u = _usuario(request)
    if not u:
        return HTMLResponse("", status_code=401)
    ejecutar_comando("UPDATE wa_conversaciones SET id_cliente=? WHERE id=?", (id_cliente or None, conv_id))
    datos = _get_conv_context(conv_id)
    datos["request"] = request
    datos["usuario_actual"] = u
    datos["wa_error_msg"] = ""
    return _templates().TemplateResponse(request, "wa_chat.html", datos)


@wa_router.post("/wa/{conv_id}/cerrar", response_class=HTMLResponse)
async def wa_cerrar(request: Request, conv_id: int):
    u = _usuario(request)
    if not u:
        return HTMLResponse("", status_code=401)
    ejecutar_comando("UPDATE wa_conversaciones SET estado='RESUELTO' WHERE id=?", (conv_id,))
    datos = _get_conv_context(conv_id)
    datos["request"] = request
    datos["usuario_actual"] = u
    datos["wa_error_msg"] = ""
    return _templates().TemplateResponse(request, "wa_chat.html", datos)


@wa_router.post("/wa/{conv_id}/reabrir", response_class=HTMLResponse)
async def wa_reabrir(request: Request, conv_id: int):
    u = _usuario(request)
    if not u:
        return HTMLResponse("", status_code=401)
    ejecutar_comando("UPDATE wa_conversaciones SET estado='ABIERTO' WHERE id=?", (conv_id,))
    datos = _get_conv_context(conv_id)
    datos["request"] = request
    datos["usuario_actual"] = u
    datos["wa_error_msg"] = ""
    return _templates().TemplateResponse(request, "wa_chat.html", datos)


# ── Etapa del funnel ──────────────────────────────────────────────────────────

@wa_router.post("/wa/{conv_id}/etapa", response_class=HTMLResponse)
async def wa_set_etapa(request: Request, conv_id: int, id_etapa: str = Form("")):
    u = _usuario(request)
    if not u:
        return HTMLResponse("", status_code=401)
    ejecutar_comando(
        "UPDATE wa_conversaciones SET id_etapa_funnel=? WHERE id=?",
        (int(id_etapa) if id_etapa else None, conv_id),
    )
    datos = _get_conv_context(conv_id)
    datos["request"] = request
    datos["usuario_actual"] = u
    datos["wa_error_msg"] = ""
    return _templates().TemplateResponse(request, "wa_chat.html", datos)


# ── Etiquetas ─────────────────────────────────────────────────────────────────

@wa_router.post("/wa/{conv_id}/etiqueta/{id_etiqueta}/toggle", response_class=HTMLResponse)
async def wa_toggle_etiqueta(request: Request, conv_id: int, id_etiqueta: int):
    u = _usuario(request)
    if not u:
        return HTMLResponse("", status_code=401)
    existe = obtener_datos(
        "SELECT 1 FROM wa_conv_etiquetas WHERE id_conversacion=? AND id_etiqueta=?",
        (conv_id, id_etiqueta),
    )
    if existe.empty:
        ejecutar_comando(
            "INSERT OR IGNORE INTO wa_conv_etiquetas (id_conversacion, id_etiqueta) VALUES (?,?)",
            (conv_id, id_etiqueta),
        )
    else:
        ejecutar_comando(
            "DELETE FROM wa_conv_etiquetas WHERE id_conversacion=? AND id_etiqueta=?",
            (conv_id, id_etiqueta),
        )
    datos = _get_conv_context(conv_id)
    datos["request"] = request
    datos["usuario_actual"] = u
    datos["wa_error_msg"] = ""
    return _templates().TemplateResponse(request, "wa_chat.html", datos)


# ── Admin: Etapas y Etiquetas ─────────────────────────────────────────────────

@wa_router.get("/admin/wa", response_class=HTMLResponse)
async def wa_admin(request: Request):
    u = _usuario(request)
    if not u:
        return RedirectResponse("/login")
    c = _ctx(request)
    if c.get("rol") != "admin":
        return RedirectResponse("/dashboard")
    etapas    = obtener_datos("SELECT * FROM wa_etapas_funnel ORDER BY orden")
    etiquetas = obtener_datos("SELECT * FROM wa_etiquetas ORDER BY orden")
    c.update({"active": "admin", "etapas": etapas.to_dict("records"),
               "etiquetas": etiquetas.to_dict("records")})
    return _templates().TemplateResponse(request, "wa_admin.html", c)


@wa_router.post("/admin/wa/etapas/nueva")
async def wa_etapa_nueva(request: Request, nombre: str = Form(""),
                          emoji: str = Form(""), color: str = Form("#6b7280"),
                          orden: int = Form(0)):
    u = _usuario(request)
    if not u:
        return RedirectResponse("/login")
    ejecutar_comando(
        "INSERT INTO wa_etapas_funnel (nombre, emoji, color, orden, activo) VALUES (?,?,?,?,1)",
        (nombre.strip(), emoji.strip(), color, orden),
    )
    return RedirectResponse("/admin/wa", status_code=303)


@wa_router.post("/admin/wa/etapas/{eid}/toggle")
async def wa_etapa_toggle(request: Request, eid: int):
    u = _usuario(request)
    if not u:
        return RedirectResponse("/login")
    ejecutar_comando("UPDATE wa_etapas_funnel SET activo = 1 - activo WHERE id=?", (eid,))
    return RedirectResponse("/admin/wa", status_code=303)


@wa_router.post("/admin/wa/etapas/{eid}/eliminar")
async def wa_etapa_eliminar(request: Request, eid: int):
    u = _usuario(request)
    if not u:
        return RedirectResponse("/login")
    ejecutar_comando("DELETE FROM wa_etapas_funnel WHERE id=?", (eid,))
    return RedirectResponse("/admin/wa", status_code=303)


@wa_router.post("/admin/wa/etiquetas/nueva")
async def wa_etiqueta_nueva(request: Request, nombre: str = Form(""),
                             color: str = Form("#3b82f6"), orden: int = Form(0)):
    u = _usuario(request)
    if not u:
        return RedirectResponse("/login")
    ejecutar_comando(
        "INSERT INTO wa_etiquetas (nombre, color, orden, activo) VALUES (?,?,?,1)",
        (nombre.strip(), color, orden),
    )
    return RedirectResponse("/admin/wa", status_code=303)


@wa_router.post("/admin/wa/etiquetas/{eid}/toggle")
async def wa_etiqueta_toggle(request: Request, eid: int):
    u = _usuario(request)
    if not u:
        return RedirectResponse("/login")
    ejecutar_comando("UPDATE wa_etiquetas SET activo = 1 - activo WHERE id=?", (eid,))
    return RedirectResponse("/admin/wa", status_code=303)


@wa_router.post("/admin/wa/etiquetas/{eid}/eliminar")
async def wa_etiqueta_eliminar(request: Request, eid: int):
    u = _usuario(request)
    if not u:
        return RedirectResponse("/login")
    ejecutar_comando("DELETE FROM wa_etiquetas WHERE id=?", (eid,))
    return RedirectResponse("/admin/wa", status_code=303)
