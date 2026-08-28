"""Asistente_IA — asistente de IA conversacional del CRM (widget flotante), sobre Groq
(modelo gratuito `llama-3.3-70b-versatile`, API estilo OpenAI de chat.completions
con tool-calling estándar).

Se migró aquí desde Gemini (`google-genai`) porque la API gratuita de Gemini
bloquea las solicitudes que vienen de IPs de proveedores de hosting/nube (medida
anti-abuso de Google, no depende del país) — confirmado en producción con el error
"This API is not available in your current location" desde el droplet de
DigitalOcean, pese a funcionar perfecto en pruebas locales (red residencial). Groq
no aplica esa restricción.

Reglas de seguridad de este módulo, no negociables:
- Las únicas tools de escritura expuestas al modelo (`preparar_cotizacion`,
  `previsualizar_conversion`) NUNCA tocan `cotizaciones` ni `reservas`. La escritura
  real (`crear_cotizacion`/`convertir_cotizacion`, en database.py) solo se dispara
  desde los endpoints `/asistente_ia/cotizacion/.../confirmar` y `.../convertir-confirmar`,
  que el modelo no puede invocar por sí mismo — solo un clic humano en la tarjeta
  de confirmación de la UI llega a esas rutas.

Notas sobre la API de Groq usada aquí (chat.completions, formato de mensajes
estándar tipo OpenAI — se reenvía el historial completo en cada llamada):
- `messages`: lista de dicts `{"role": "system"|"user"|"assistant"|"tool", ...}`.
  Un mensaje `assistant` con tool-calls trae `tool_calls` (lista de
  `{"id", "type": "function", "function": {"name", "arguments"}}`) y `content=None`.
  La respuesta a cada tool-call es un mensaje `{"role": "tool", "tool_call_id",
  "name", "content"}`.
- `tools`: lista de dicts `{"type": "function", "function": {"name", "description",
  "parameters"}}` (anidado, a diferencia del formato plano de Gemini).
- A diferencia de Gemini, esta API no tiene un campo `is_error` nativo en el
  resultado de una tool — los errores se comunican como texto dentro del `content`
  del mensaje `tool`, y el propio prompt le pide al modelo que los reconozca así.
"""
import json
import logging
import os

import groq
from groq import AsyncGroq
from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

import asistente_ia_tools as tools
from database import ejecutar_comando, obtener_datos, now_local, crear_cotizacion, convertir_cotizacion, crear_cliente, editar_cliente, reclamar_estado

asistente_ia_router = APIRouter()

MODELO = "openai/gpt-oss-120b"
_TOPE_ITERACIONES = 5
# Límites reales de esta cuenta para este modelo, confirmados en el dashboard
# (console.groq.com/settings/limits) — actualizar aquí si Groq los cambia o si se
# cambia de modelo. También existe un límite de 8K tokens/minuto (200K/día) que no
# se refleja en este contador simple de solicitudes.
RPM_LIMITE = 30
RPD_LIMITE = 1000
TPM_LIMITE = 8000
TPD_LIMITE = 200000

SYSTEM_PROMPT = """Eres Asistente IA, el asistente de IA interno del CRM de Tu Agencia de Viajes (agencia de viajes).
Hablas español, respuestas breves y claras, sin relleno.

Reglas duras:
- Eres de SOLO LECTURA salvo por cuatro tools especiales: `preparar_cliente_nuevo`,
  `preparar_edicion_cliente`, `preparar_cotizacion` y `previsualizar_conversion`. Ninguna guarda
  nada en las tablas reales de negocio — solo arman una propuesta que el humano debe confirmar
  con un clic en la tarjeta que le muestra la interfaz. Nunca digas que "ya guardaste", "ya
  registré al cliente", "ya lo edité" o "ya convertiste" algo — di que dejaste la propuesta lista
  para que la confirmen.
- Para editar un cliente ya existente (ej. agregarle un correo o corregir su teléfono): usa
  `obtener_cliente` para ver sus datos actuales si no los tienes, y luego `preparar_edicion_cliente`
  con solo el/los campos que cambian — nunca reescribas campos que el usuario no pidió modificar.
- Antes de `preparar_cotizacion`, SIEMPRE resuelve primero un `id_cliente` real con `buscar_cliente`.
  Nunca llames `preparar_cotizacion` sin un `id_cliente` que ya exista en el sistema — si el cliente
  no aparece en la búsqueda, pregúntale al usuario nombre, teléfono, email y fecha de nacimiento del
  cliente (nunca los inventes ni los omitas) y usa `preparar_cliente_nuevo`; solo continúa con la
  cotización después de que el humano confirme la creación del cliente y tengas su `id_cliente` real.
- Para crear una cotización: pregunta la información que falte (destino, fechas, hotel,
  vuelo/aerolínea, mayorista, costos y montos de cobro al cliente) antes de llamar a
  `preparar_cotizacion`. No inventes datos que el usuario no te dio.
- Para convertir una cotización ya guardada en itinerario: usa `previsualizar_conversion` para
  mostrar el resumen. La conversión real solo ocurre si el humano confirma en la tarjeta — tú nunca
  la ejecutas.
- Nunca mezcles MXN y USD sin aclarar de qué moneda hablas.
- Si te preguntan CÓMO hacer algo en el sistema (no sobre datos de un cliente/itinerario en
  particular), usa `consultar_manual` primero — nunca inventes pasos, nombres de botones o
  ubicaciones de menú que no vengan del manual. Si el manual no cubre el tema, dilo claramente
  en vez de adivinar.
- Si una búsqueda no encuentra nada, dilo claramente — nunca inventes un resultado.
- Para redactar un mensaje a un cliente (WhatsApp, estado de cuenta), primero usa
  `datos_itinerario_para_mensaje` y basa el texto solo en esos datos reales. Nunca inventes montos
  o fechas. Nunca digas que ya lo enviaste — el usuario lo copia y lo envía él mismo.
- Sé conciso. Usa listas o tablas breves cuando ayuden a leer montos o fechas.
"""

_TOOLS_PLANO = [
    {
        "type": "function", "name": "buscar_cliente",
        "description": "Busca clientes por nombre, teléfono, email o id. Devuelve hasta 15 coincidencias.",
        "parameters": {"type": "object", "properties": {"texto": {"type": "string"}}, "required": ["texto"]},
    },
    {
        "type": "function", "name": "buscar_itinerario",
        "description": "Busca itinerarios (reservas) por nombre de cliente, destino o id. Devuelve hasta 15 coincidencias.",
        "parameters": {"type": "object", "properties": {"texto": {"type": "string"}}, "required": ["texto"]},
    },
    {
        "type": "function", "name": "saldo_itinerario",
        "description": "Saldo real de un itinerario (venta, cobrado, pendiente). Indica si el itinerario existe.",
        "parameters": {"type": "object", "properties": {"id_reserva": {"type": "integer"}}, "required": ["id_reserva"]},
    },
    {
        "type": "function", "name": "cartera_vencida",
        "description": "Parcialidades de plan de pagos ya vencidas, ordenadas por antigüedad.",
        "parameters": {"type": "object", "properties": {"limit": {"type": "integer", "description": "Máximo de filas, default 15"}}},
    },
    {
        "type": "function", "name": "resumen_financiero_mes",
        "description": "Ingresos, egresos y balance del mes, desglosado por moneda (MXN y USD por separado).",
        "parameters": {"type": "object", "properties": {
            "mes": {"type": "integer", "description": "1-12, default mes actual"},
            "anio": {"type": "integer", "description": "default año actual"},
        }},
    },
    {
        "type": "function", "name": "alertas_torre_control",
        "description": "Alertas operativas: cobranza próxima/vencida, pagos a proveedor próximos/vencidos, check-ins pendientes.",
        "parameters": {"type": "object", "properties": {"limit": {"type": "integer", "description": "Máximo por categoría, default 20"}}},
    },
    {
        "type": "function", "name": "cumpleanos_proximos",
        "description": "Clientes que cumplen años en los próximos N días (default 7).",
        "parameters": {"type": "object", "properties": {"dias": {"type": "integer"}}},
    },
    {
        "type": "function", "name": "catalogo_hoteles",
        "description": "Catálogo de hoteles ya usados, opcionalmente filtrado por texto.",
        "parameters": {"type": "object", "properties": {"filtro": {"type": "string"}}},
    },
    {
        "type": "function", "name": "catalogo_aerolineas",
        "description": "Catálogo de aerolíneas ya usadas, opcionalmente filtrado por texto.",
        "parameters": {"type": "object", "properties": {"filtro": {"type": "string"}}},
    },
    {
        "type": "function", "name": "catalogo_mayoristas",
        "description": "Catálogo de mayoristas ya usados, opcionalmente filtrado por texto.",
        "parameters": {"type": "object", "properties": {"filtro": {"type": "string"}}},
    },
    {
        "type": "function", "name": "catalogo_proveedores",
        "description": "Catálogo de proveedores por tipo: 'traslados', 'tours' o 'adicionales'.",
        "parameters": {"type": "object", "properties": {
            "tipo": {"type": "string", "enum": ["traslados", "tours", "adicionales"]},
            "filtro": {"type": "string"},
        }, "required": ["tipo"]},
    },
    {
        "type": "function", "name": "datos_itinerario_para_mensaje",
        "description": "Datos completos de un itinerario (destino, fechas, hotel, saldo, próximas parcialidades) para redactar un mensaje al cliente.",
        "parameters": {"type": "object", "properties": {"id_reserva": {"type": "integer"}}, "required": ["id_reserva"]},
    },
    {
        "type": "function", "name": "preparar_cliente_nuevo",
        "description": (
            "Arma una PROPUESTA de cliente nuevo cuando buscar_cliente no encontró al cliente "
            "que el usuario menciona. NO lo guarda en el sistema — solo la deja lista (con avisos "
            "de posibles duplicados) para que el humano la confirme desde un botón en la interfaz. "
            "Pregunta siempre nombre, teléfono, email y fecha de nacimiento antes de llamar esta "
            "tool — nunca inventes ni omitas estos datos, si el usuario no te los da, pídelos."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "nombre": {"type": "string"}, "telefono": {"type": "string"},
                "email": {"type": "string"}, "fecha_nacimiento": {"type": "string"},
            },
            "required": ["nombre"],
        },
    },
    {
        "type": "function", "name": "obtener_cliente",
        "description": "Ficha completa de un cliente por su id exacto (ej. 'AC-071'). Úsala antes de proponer una edición, para conocer los valores actuales.",
        "parameters": {"type": "object", "properties": {"id_cliente": {"type": "string"}}, "required": ["id_cliente"]},
    },
    {
        "type": "function", "name": "preparar_edicion_cliente",
        "description": (
            "Arma una PROPUESTA para editar datos de un cliente YA existente (ej. agregar o "
            "corregir teléfono, email o fecha de nacimiento). NO lo guarda — solo deja lista una "
            "tarjeta con el cambio (antes → después) para que el humano la confirme. Usa "
            "obtener_cliente primero si no conoces los datos actuales del cliente."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "id_cliente": {"type": "string"},
                "nombre": {"type": "string"}, "telefono": {"type": "string"},
                "email": {"type": "string"}, "fecha_nacimiento": {"type": "string"},
            },
            "required": ["id_cliente"],
        },
    },
    {
        "type": "function", "name": "preparar_cotizacion",
        "description": (
            "Arma una PROPUESTA de cotización a partir de los datos recabados en la conversación. "
            "NO la guarda en el sistema — solo la deja lista con un resumen para que el humano la "
            "confirme desde un botón en la interfaz. Úsala solo cuando ya tengas destino, fechas, "
            "cliente y al menos un monto de cobro."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "id_cliente": {"type": "string"}, "destino": {"type": "string"}, "origen": {"type": "string"},
                "fecha_salida": {"type": "string"}, "fecha_regreso": {"type": "string"}, "moneda": {"type": "string"},
                "num_adultos": {"type": "integer"}, "num_menores": {"type": "integer"},
                "nombre_hotel": {"type": "string"}, "aerolinea": {"type": "string"}, "mayorista": {"type": "string"},
                "cobro_vuelos": {"type": "number"}, "cobro_tua": {"type": "number"}, "cobro_hotel": {"type": "number"},
                "cobro_traslados": {"type": "number"}, "cobro_tours": {"type": "number"}, "cobro_adicionales": {"type": "number"},
                "costo_vuelos": {"type": "number"}, "costo_tua": {"type": "number"}, "costo_hotel": {"type": "number"},
                "costo_traslados": {"type": "number"}, "costo_tours": {"type": "number"}, "costo_adicionales": {"type": "number"},
                "anticipo_requerido": {"type": "number"}, "notas": {"type": "string"},
            },
            "required": ["destino", "fecha_salida", "fecha_regreso"],
        },
    },
    {
        "type": "function", "name": "consultar_manual",
        "description": (
            "Busca en el manual de usuario del CRM cómo hacer algo en el sistema (ej. 'cómo "
            "registro un pago', 'cómo creo una cotización con dos hoteles', 'cómo anulo un "
            "cobro'). Úsala SIEMPRE que te pregunten cómo usar una función del sistema, antes "
            "de responder — nunca inventes pasos o nombres de botones sin consultarla primero."
        ),
        "parameters": {"type": "object", "properties": {"tema": {"type": "string"}}, "required": ["tema"]},
    },
    {
        "type": "function", "name": "previsualizar_conversion",
        "description": "Muestra el resumen (venta/costo/utilidad/opciones de hotel) de una cotización YA guardada, antes de proponer convertirla a itinerario.",
        "parameters": {"type": "object", "properties": {"id_cotizacion": {"type": "integer"}}, "required": ["id_cotizacion"]},
    },
]

# Groq (como OpenAI) espera cada tool anidada bajo "function", no como dict plano.
TOOLS = [
    {"type": "function", "function": {k: v for k, v in t.items() if k != "type"}}
    for t in _TOOLS_PLANO
]

_EJECUTORES = {
    "buscar_cliente": lambda i, **kw: tools.buscar_cliente(i["texto"]),
    "buscar_itinerario": lambda i, **kw: tools.buscar_itinerario(i["texto"]),
    "saldo_itinerario": lambda i, **kw: tools.saldo_itinerario(i["id_reserva"]),
    "cartera_vencida": lambda i, **kw: tools.cartera_vencida(i.get("limit", 15)),
    "resumen_financiero_mes": lambda i, **kw: tools.resumen_financiero_mes(i.get("mes"), i.get("anio")),
    "alertas_torre_control": lambda i, **kw: tools.alertas_torre_control(i.get("limit", 20)),
    "cumpleanos_proximos": lambda i, **kw: tools.cumpleanos_proximos(i.get("dias", 7)),
    "catalogo_hoteles": lambda i, **kw: tools.catalogo_hoteles(i.get("filtro", "")),
    "catalogo_aerolineas": lambda i, **kw: tools.catalogo_aerolineas(i.get("filtro", "")),
    "catalogo_mayoristas": lambda i, **kw: tools.catalogo_mayoristas(i.get("filtro", "")),
    "catalogo_proveedores": lambda i, **kw: tools.catalogo_proveedores(i["tipo"], i.get("filtro", "")),
    "datos_itinerario_para_mensaje": lambda i, **kw: tools.datos_itinerario_para_mensaje(i["id_reserva"]),
    "preparar_cliente_nuevo": lambda i, id_conversacion, **kw: tools.preparar_cliente_nuevo(id_conversacion, i),
    "obtener_cliente": lambda i, **kw: tools.obtener_cliente(i["id_cliente"]),
    "preparar_edicion_cliente": lambda i, id_conversacion, **kw: tools.preparar_edicion_cliente(id_conversacion, i["id_cliente"], i),
    "preparar_cotizacion": lambda i, id_conversacion, **kw: tools.preparar_cotizacion(id_conversacion, i),
    "previsualizar_conversion": lambda i, **kw: tools.previsualizar_conversion(i["id_cotizacion"]),
    "consultar_manual": lambda i, **kw: tools.consultar_manual(i["tema"]),
}


_client_singleton = None


def _cliente():
    """Cliente único reutilizado en todo el proceso — evita pagar el costo de conexión
    (TLS/handshake) en cada intercambio."""
    global _client_singleton
    if _client_singleton is None:
        api_key = os.environ.get("GROQ_API_KEY")
        if not api_key:
            raise RuntimeError("Falta GROQ_API_KEY — Asistente IA no está disponible ahora mismo.")
        _client_singleton = AsyncGroq(api_key=api_key)
    return _client_singleton


def _registrar_uso(completion):
    """Guarda el conteo de tokens que la propia respuesta de Groq ya trae (gratis, no
    hace ninguna llamada extra) — para poder mostrar un contador de uso en el widget."""
    try:
        usage = getattr(completion, "usage", None)
        if not usage:
            return
        ejecutar_comando(
            "INSERT INTO asistente_ia_uso_api (fecha_hora, tokens_entrada, tokens_salida, tokens_total) VALUES (?,?,?,?)",
            (
                now_local().strftime("%Y-%m-%d %H:%M:%S"),
                int(usage.prompt_tokens or 0),
                int(usage.completion_tokens or 0),
                int(usage.total_tokens or 0),
            )
        )
    except Exception as e:
        logging.error(f"Asistente_IA _registrar_uso: {e}")


def _texto_error_api(e: "groq.APIStatusError") -> str:
    code = getattr(e, "status_code", None)
    if code == 429:
        return "Estoy recibiendo muchas solicitudes ahora mismo. Intenta de nuevo en unos segundos."
    if code and code >= 500:
        return "El servicio de IA no está disponible ahora mismo. Intenta de nuevo en un momento."
    return f"Ocurrió un error del servicio de IA ({code}). Intenta de nuevo."


def _tool_call_a_dict(tc) -> dict:
    return {
        "id": tc.id, "type": "function",
        "function": {"name": tc.function.name, "arguments": tc.function.arguments},
    }


async def responder(mensaje_usuario: str, historial: list, usuario: str, id_conversacion: int):
    """Loop de tool-use sobre la API de chat.completions de Groq.
    Retorna (texto_final, pasos_actualizados, id_propuesta_cotizacion|None)."""
    pasos = list(historial) + [{"role": "user", "content": mensaje_usuario}]

    try:
        client = _cliente()
    except RuntimeError as e:
        pasos.append({"role": "assistant", "content": str(e)})
        return str(e), pasos, None

    id_propuesta_generada = None

    for _ in range(_TOPE_ITERACIONES):
        try:
            completion = await client.chat.completions.create(
                model=MODELO,
                messages=[{"role": "system", "content": SYSTEM_PROMPT}] + pasos,
                tools=TOOLS, tool_choice="auto", max_tokens=1024,
            )
        except groq.APIStatusError as e:
            logging.error(f"Asistente_IA Groq APIStatusError: {e}")
            texto_error = _texto_error_api(e)
            pasos.append({"role": "assistant", "content": texto_error})
            return texto_error, pasos, None
        except Exception as e:
            logging.error(f"Asistente_IA Groq error inesperado: {e}")
            texto_error = "No pude conectarme con el servicio de IA. Intenta de nuevo en un momento."
            pasos.append({"role": "assistant", "content": texto_error})
            return texto_error, pasos, None

        _registrar_uso(completion)
        mensaje = completion.choices[0].message
        tool_calls = mensaje.tool_calls or []

        if not tool_calls:
            texto = mensaje.content or ""
            pasos.append({"role": "assistant", "content": texto})
            return texto, pasos, id_propuesta_generada

        pasos.append({
            "role": "assistant", "content": mensaje.content,
            "tool_calls": [_tool_call_a_dict(tc) for tc in tool_calls],
        })

        for tc in tool_calls:
            nombre = tc.function.name
            try:
                args = json.loads(tc.function.arguments or "{}")
            except json.JSONDecodeError:
                args = {}
            ejecutor = _EJECUTORES.get(nombre)
            try:
                if ejecutor is None:
                    resultado = {"error": "herramienta desconocida"}
                else:
                    resultado = ejecutor(args, id_conversacion=id_conversacion, usuario=usuario)
                    if nombre == "preparar_cotizacion" and isinstance(resultado, dict) and "id_propuesta" in resultado:
                        id_propuesta_generada = resultado["id_propuesta"]
            except Exception as e:
                logging.error(f"Asistente_IA tool '{nombre}' falló: {e}")
                resultado = {"error": str(e)}
            pasos.append({
                "role": "tool", "tool_call_id": tc.id, "name": nombre,
                "content": json.dumps(resultado, ensure_ascii=False, default=str),
            })

    texto_tope = "Se me complicó responder eso (demasiados pasos). ¿Puedes reformular la pregunta?"
    pasos.append({"role": "assistant", "content": texto_tope})
    return texto_tope, pasos, id_propuesta_generada


# ─── Persistencia de conversación ─────────────────────────────────────────────

_CONVERSACION_TIMEOUT_MIN = 120  # 2h, igual al timeout de sesión del resto del CRM


def _obtener_o_crear_conversacion(usuario: str) -> int:
    """Reutiliza la última conversación del usuario si tuvo actividad reciente (evita que
    Asistente_IA 'olvide' todo entre un mensaje y otro); si pasó mucho tiempo sin uso, empieza
    una conversación nueva en vez de seguir agregando a una de hace días. Las conversaciones
    viejas no se borran, solo dejan de mostrarse por default."""
    df = obtener_datos(
        "SELECT id, fecha_ultimo_mensaje FROM asistente_ia_conversaciones WHERE usuario=? ORDER BY id DESC LIMIT 1",
        (usuario,)
    )
    if not df.empty:
        ultimo = df.iloc[0]["fecha_ultimo_mensaje"]
        if ultimo:
            minutos = (now_local() - __import__("datetime").datetime.strptime(str(ultimo)[:19], "%Y-%m-%d %H:%M:%S")).total_seconds() / 60
            if minutos <= _CONVERSACION_TIMEOUT_MIN:
                return int(df.iloc[0]["id"])
        else:
            return int(df.iloc[0]["id"])
    return _crear_conversacion(usuario)


def _crear_conversacion(usuario: str) -> int:
    ejecutar_comando("INSERT INTO asistente_ia_conversaciones (usuario) VALUES (?)", (usuario,))
    df_new = obtener_datos("SELECT MAX(id) as n FROM asistente_ia_conversaciones")
    return int(df_new.iloc[0]["n"])


def _cargar_historial(id_conversacion: int) -> list:
    """Retorna la lista de mensajes (dicts) ya lista para pasar como `messages` a Groq.
    Descarta mensajes con formato viejo (de antes de la migración Gemini -> Groq, que
    usaban 'type' en vez de 'role') — no rompen nada, solo se pierde ese contexto viejo."""
    df = obtener_datos(
        "SELECT content_json FROM asistente_ia_mensajes WHERE id_conversacion=? ORDER BY id",
        (id_conversacion,)
    )
    if df.empty:
        return []
    pasos = [json.loads(row["content_json"]) for _, row in df.iterrows()]
    return [p for p in pasos if "role" in p]


def _persistir_pasos_nuevos(id_conversacion: int, pasos_previos: list, pasos_actualizados: list):
    nuevos = pasos_actualizados[len(pasos_previos):]
    for paso in nuevos:
        role = paso.get("role", "assistant")
        ejecutar_comando(
            "INSERT INTO asistente_ia_mensajes (id_conversacion, role, content_json) VALUES (?,?,?)",
            (id_conversacion, role, json.dumps(paso, ensure_ascii=False, default=str))
        )
    ejecutar_comando(
        "UPDATE asistente_ia_conversaciones SET fecha_ultimo_mensaje=? WHERE id=?",
        (now_local().strftime("%Y-%m-%d %H:%M:%S"), id_conversacion)
    )


# ─── Endpoints ─────────────────────────────────────────────────────────────

def _templates():
    import main as _m
    return _m.templates

def _ctx(request, extra: dict):
    import main as _m
    return _m.ctx(request, extra)

def _usuario(request):
    import main as _m
    return _m.usuario_activo(request)


def _mensajes_para_template(id_conversacion: int) -> list:
    """Aplana el historial de mensajes a una lista simple para el template: solo el
    texto de user/assistant con contenido, sin los mensajes de tool_calls/tool."""
    pasos = _cargar_historial(id_conversacion)
    salida = []
    for p in pasos:
        role = p.get("role")
        if role not in ("user", "assistant"):
            continue
        texto = p.get("content") or ""
        if texto.strip():
            salida.append({"role": role, "texto": texto})
    return salida


def _propuesta_pendiente_de_conversacion(id_conversacion: int):
    """Última propuesta de COTIZACIÓN aún sin confirmar/descartar de esta conversación — se
    usa tanto al responder un mensaje como en cada poll de /asistente_ia/mensajes, para que la
    tarjeta de confirmación no desaparezca sola antes de que el humano alcance a hacer clic."""
    df = obtener_datos(
        "SELECT id, resumen_texto, estado FROM asistente_ia_cotizaciones_pendientes "
        "WHERE id_conversacion=? AND estado='PENDIENTE_CONFIRMACION' ORDER BY id DESC LIMIT 1",
        (id_conversacion,)
    )
    if df.empty:
        return None
    return df.to_dict("records")[0]


def _propuesta_cliente_pendiente_de_conversacion(id_conversacion: int):
    """Igual que `_propuesta_pendiente_de_conversacion` pero para propuestas de CLIENTE nuevo."""
    df = obtener_datos(
        "SELECT id, resumen_texto, estado, id_cliente_existente FROM asistente_ia_clientes_pendientes "
        "WHERE id_conversacion=? AND estado='PENDIENTE_CONFIRMACION' ORDER BY id DESC LIMIT 1",
        (id_conversacion,)
    )
    if df.empty:
        return None
    return df.to_dict("records")[0]


def _uso_actual() -> dict:
    """Contador de uso construido solo con datos que Groq ya regresó gratis en cada
    respuesta (asistente_ia_uso_api) — no dispara ninguna llamada nueva a la API. Incluye
    tokens/minuto porque en Groq ese es el límite que realmente aprieta (8K TPM),
    no el de solicitudes (30 RPM)."""
    hoy = str(now_local().date())
    hace_un_min = (now_local().replace(microsecond=0) - __import__("datetime").timedelta(seconds=60)).strftime("%Y-%m-%d %H:%M:%S")
    df_min = obtener_datos(
        "SELECT COUNT(*) as n, COALESCE(SUM(tokens_total),0) as tk FROM asistente_ia_uso_api WHERE fecha_hora >= ?",
        (hace_un_min,)
    )
    df_dia = obtener_datos(
        "SELECT COUNT(*) as n, COALESCE(SUM(tokens_total),0) as tk FROM asistente_ia_uso_api WHERE fecha_hora LIKE ?",
        (f"{hoy}%",)
    )
    return {
        "rpm_usado": int(df_min.iloc[0]["n"]), "rpm_limite": RPM_LIMITE,
        "rpd_usado": int(df_dia.iloc[0]["n"]), "rpd_limite": RPD_LIMITE,
        "tpm_usado": int(df_min.iloc[0]["tk"]), "tpm_limite": TPM_LIMITE,
        "tokens_hoy": int(df_dia.iloc[0]["tk"]), "tpd_limite": TPD_LIMITE,
    }


@asistente_ia_router.get("/asistente_ia/mensajes")
async def asistente_ia_mensajes(request: Request):
    usuario = _usuario(request)
    if not usuario:
        return HTMLResponse("")
    id_conversacion = _obtener_o_crear_conversacion(usuario)
    return _templates().TemplateResponse(request, "asistente_ia_mensajes_partial.html", _ctx(request, {
        "mensajes": _mensajes_para_template(id_conversacion),
        "id_conversacion": id_conversacion,
        "propuesta": _propuesta_pendiente_de_conversacion(id_conversacion),
        "propuesta_cliente": _propuesta_cliente_pendiente_de_conversacion(id_conversacion),
    }))


@asistente_ia_router.post("/asistente_ia/nueva-conversacion")
async def asistente_ia_nueva_conversacion(request: Request):
    """Botón manual — inicia una conversación en blanco. Las anteriores quedan intactas en
    la base de datos, solo dejan de mostrarse (no hay borrado)."""
    usuario = _usuario(request)
    if not usuario:
        return HTMLResponse("")
    id_conversacion = _crear_conversacion(usuario)
    return _templates().TemplateResponse(request, "asistente_ia_mensajes_partial.html", _ctx(request, {
        "mensajes": [],
        "id_conversacion": id_conversacion,
        "propuesta": None,
        "propuesta_cliente": None,
    }))


@asistente_ia_router.post("/asistente_ia/enviar")
async def asistente_ia_enviar(request: Request, mensaje: str = Form("")):
    usuario = _usuario(request)
    if not usuario:
        return HTMLResponse("")
    mensaje = (mensaje or "").strip()
    id_conversacion = _obtener_o_crear_conversacion(usuario)

    if mensaje:
        historial = _cargar_historial(id_conversacion)
        _texto, pasos_actualizados, _id_propuesta = await responder(mensaje, historial, usuario, id_conversacion)
        _persistir_pasos_nuevos(id_conversacion, historial, pasos_actualizados)

    return _templates().TemplateResponse(request, "asistente_ia_mensajes_partial.html", _ctx(request, {
        "mensajes": _mensajes_para_template(id_conversacion),
        "id_conversacion": id_conversacion,
        "propuesta": _propuesta_pendiente_de_conversacion(id_conversacion),
        "propuesta_cliente": _propuesta_cliente_pendiente_de_conversacion(id_conversacion),
    }))


@asistente_ia_router.post("/asistente_ia/cliente/{id_propuesta}/confirmar")
async def asistente_ia_confirmar_cliente(request: Request, id_propuesta: int):
    """Nunca pasa por el modelo — solo un clic humano llega aquí."""
    usuario = _usuario(request)
    if not usuario:
        return RedirectResponse(url="/login")
    df = obtener_datos(
        "SELECT id_conversacion, payload_json, id_cliente_existente FROM asistente_ia_clientes_pendientes WHERE id=?",
        (id_propuesta,)
    )
    if not df.empty and reclamar_estado("asistente_ia_clientes_pendientes", id_propuesta, "PENDIENTE_CONFIRMACION", "CONFIRMANDO"):
        payload = json.loads(df.iloc[0]["payload_json"])
        id_existente = df.iloc[0]["id_cliente_existente"]
        if id_existente:
            ok, error = editar_cliente(id_existente, payload, usuario)
            new_id = id_existente
            texto_ok = f"✅ Datos de {payload.get('nombre','')} actualizados — ver en /clientes/{id_existente}"
        else:
            new_id, error = crear_cliente(payload, usuario)
            ok = not error
            texto_ok = f"✅ Cliente guardado con ID {new_id} — ver en /clientes/{new_id}"
        if ok:
            ejecutar_comando(
                "UPDATE asistente_ia_clientes_pendientes SET estado='CONFIRMADA' WHERE id=?",
                (id_propuesta,)
            )
            id_conversacion = int(df.iloc[0]["id_conversacion"])
            return _templates().TemplateResponse(request, "asistente_ia_mensajes_partial.html", _ctx(request, {
                "mensajes": _mensajes_para_template(id_conversacion),
                "id_conversacion": id_conversacion,
                "propuesta": _propuesta_pendiente_de_conversacion(id_conversacion),
                "propuesta_cliente": None,
                "confirmacion_texto": texto_ok,
            }))
        # crear_cliente/editar_cliente falló (ej. validación) — libera el reclamo para reintentar
        ejecutar_comando(
            "UPDATE asistente_ia_clientes_pendientes SET estado='PENDIENTE_CONFIRMACION' WHERE id=?",
            (id_propuesta,)
        )
    return HTMLResponse("<div class='asistente_ia-error'>No se pudo confirmar la propuesta.</div>")


@asistente_ia_router.post("/asistente_ia/cotizacion/{id_propuesta}/confirmar")
async def asistente_ia_confirmar_cotizacion(request: Request, id_propuesta: int):
    """Nunca pasa por el modelo — solo un clic humano llega aquí."""
    usuario = _usuario(request)
    if not usuario:
        return RedirectResponse(url="/login")
    df = obtener_datos(
        "SELECT id_conversacion, payload_json FROM asistente_ia_cotizaciones_pendientes WHERE id=?",
        (id_propuesta,)
    )
    if not df.empty and reclamar_estado("asistente_ia_cotizaciones_pendientes", id_propuesta, "PENDIENTE_CONFIRMACION", "CONFIRMANDO"):
        payload = json.loads(df.iloc[0]["payload_json"])
        new_id, error = crear_cotizacion(payload, usuario)
        if not error:
            ejecutar_comando(
                "UPDATE asistente_ia_cotizaciones_pendientes SET estado='CONFIRMADA' WHERE id=?",
                (id_propuesta,)
            )
            id_conversacion = int(df.iloc[0]["id_conversacion"])
            return _templates().TemplateResponse(request, "asistente_ia_mensajes_partial.html", _ctx(request, {
                "mensajes": _mensajes_para_template(id_conversacion),
                "id_conversacion": id_conversacion,
                "propuesta": None,
                "confirmacion_texto": f"✅ Cotización guardada — ver en /cotizaciones/{new_id}",
            }))
        # crear_cotizacion falló (ej. validación) — libera el reclamo para permitir reintentar
        ejecutar_comando(
            "UPDATE asistente_ia_cotizaciones_pendientes SET estado='PENDIENTE_CONFIRMACION' WHERE id=?",
            (id_propuesta,)
        )
    return HTMLResponse("<div class='asistente_ia-error'>No se pudo confirmar la propuesta.</div>")


@asistente_ia_router.post("/asistente_ia/cotizacion/{id_cotizacion}/convertir-confirmar")
async def asistente_ia_confirmar_conversion(request: Request, id_cotizacion: int):
    """Nunca pasa por el modelo — solo un clic humano llega aquí."""
    usuario = _usuario(request)
    if not usuario:
        return RedirectResponse(url="/login")
    hotel_opcion = (dict(await request.form())).get("hotel_opcion_elegida", 1)
    id_reserva, error = convertir_cotizacion(id_cotizacion, hotel_opcion, usuario)
    id_conversacion = _obtener_o_crear_conversacion(usuario)
    texto = f"✅ Itinerario #{id_reserva} creado — ver en /bitacora/{id_reserva}" if not error else f"❌ {error}"
    return _templates().TemplateResponse(request, "asistente_ia_mensajes_partial.html", _ctx(request, {
        "mensajes": _mensajes_para_template(id_conversacion),
        "id_conversacion": id_conversacion,
        "propuesta": None,
        "confirmacion_texto": texto,
    }))


@asistente_ia_router.get("/asistente_ia/uso")
async def asistente_ia_uso(request: Request):
    usuario = _usuario(request)
    if not usuario:
        return {}
    return _uso_actual()
