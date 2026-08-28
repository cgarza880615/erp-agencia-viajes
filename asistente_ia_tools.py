"""Tools de negocio para Asistente_IA (asistente de IA). Funciones Python puras, sin
`request` ni dependencias de FastAPI — reciben argumentos ya parseados (el dict
`input` que manda el modelo) y retornan dict/list serializables a JSON.

Las tools de este archivo son de solo lectura, salvo `preparar_cotizacion` y
`previsualizar_conversion`, que NUNCA escriben en tablas de negocio (`cotizaciones`,
`reservas`) — solo arman una propuesta en `asistente_ia_cotizaciones_pendientes` o leen.
La escritura real (`crear_cotizacion`, `convertir_cotizacion`, ambas en database.py)
solo se dispara desde un endpoint HTTP fuera del alcance del modelo, tras que el
humano haga clic en un botón de confirmación.
"""
import json
import os
import re

from database import (
    obtener_datos, ejecutar_comando, calcular_saldo_real, now_local,
    get_catalogo_hoteles, get_catalogo_aerolineas, get_catalogo_mayoristas,
    get_catalogo_proveedores_traslados, get_catalogo_proveedores_tours,
    get_catalogo_proveedores_adicionales, get_catalogo_equipaje,
    buscar_duplicados_cliente,
)


def _df_a_registros(df):
    return json.loads(df.to_json(orient="records")) if not df.empty else []


# ─── Tools de solo lectura ────────────────────────────────────────────────────

def buscar_cliente(texto: str):
    like = f"%{texto}%"
    df = obtener_datos(
        """SELECT c.id_cliente, c.nombre, c.telefono, c.email, c.fecha_nacimiento,
                  COUNT(r.id_reserva) as num_viajes
           FROM clientes c
           LEFT JOIN reservas r ON r.id_cliente = c.id_cliente
           WHERE c.nombre LIKE ? OR c.telefono LIKE ? OR c.email LIKE ? OR c.id_cliente = ?
           GROUP BY c.id_cliente
           ORDER BY c.nombre LIMIT 15""",
        (like, like, like, texto.strip())
    )
    return _df_a_registros(df)


def buscar_itinerario(texto: str):
    like = f"%{texto}%"
    df = obtener_datos(
        """SELECT r.id_reserva, c.nombre, r.destino, r.estado, r.fecha_salida, r.fecha_regreso,
                  r.venta_total, r.cobrado_cliente, r.moneda
           FROM reservas r JOIN clientes c ON r.id_cliente = c.id_cliente
           WHERE c.nombre LIKE ? OR r.destino LIKE ? OR CAST(r.id_reserva AS TEXT) = ?
           ORDER BY r.fecha_salida DESC LIMIT 15""",
        (like, like, texto.strip())
    )
    return _df_a_registros(df)


def saldo_itinerario(id_reserva: int):
    df = obtener_datos("SELECT 1 FROM reservas WHERE id_reserva = ?", (id_reserva,))
    if df.empty:
        return {"encontrado": False}
    saldo = calcular_saldo_real(int(id_reserva))
    return {"encontrado": True, **saldo}


def cartera_vencida(limit: int = 15):
    hoy = str(now_local().date())
    df = obtener_datos(
        """SELECT pp.id_reserva, c.nombre, r.destino, pp.numero_pago,
                  ROUND(pp.monto_esperado - pp.monto_pagado, 2) as monto_pendiente,
                  pp.fecha_programada, r.moneda,
                  CAST(julianday(?) - julianday(pp.fecha_programada) AS INTEGER) as dias_vencido
           FROM plan_pagos pp
           JOIN reservas r ON pp.id_reserva = r.id_reserva
           JOIN clientes c ON r.id_cliente = c.id_cliente
           WHERE pp.estado IN ('PENDIENTE','PARCIAL') AND r.estado = 'ACTIVO'
             AND pp.fecha_programada < ?
           ORDER BY pp.fecha_programada LIMIT ?""",
        (hoy, hoy, int(limit))
    )
    return _df_a_registros(df)


def resumen_financiero_mes(mes=None, anio=None):
    hoy = now_local().date()
    mes = int(mes) if mes else hoy.month
    anio = int(anio) if anio else hoy.year
    prefijo = f"{anio:04d}-{mes:02d}"
    df = obtener_datos(
        """SELECT moneda,
                  SUM(CASE WHEN tipo_movimiento='INGRESO' THEN monto ELSE 0 END) as ingresos,
                  SUM(CASE WHEN tipo_movimiento='EGRESO' THEN monto ELSE 0 END) as egresos
           FROM flujo_caja
           WHERE estado='ACTIVO' AND fecha_pago LIKE ?
           GROUP BY moneda""",
        (f"{prefijo}%",)
    )
    resultado = {"mes": mes, "anio": anio}
    for row in _df_a_registros(df):
        ingresos = round(float(row["ingresos"] or 0), 2)
        egresos = round(float(row["egresos"] or 0), 2)
        resultado[row["moneda"]] = {
            "ingresos": ingresos, "egresos": egresos, "balance": round(ingresos - egresos, 2),
        }
    return resultado


def alertas_torre_control(limit: int = 20):
    """Versión de datos puros (sin HTML) de las alertas de Torre de Control, para que
    Asistente_IA las consulte o arme el resumen narrado. Reservas de grupo se excluyen del
    pago a proveedor individual (se controla a nivel de grupo, mismo criterio que /alertas)."""
    hoy_d = now_local().date()
    hoy = str(hoy_d)

    df_pagos = obtener_datos(
        """SELECT pp.numero_pago, ROUND(pp.monto_esperado - pp.monto_pagado, 2) as monto_pendiente,
                  pp.fecha_programada, r.moneda, r.id_reserva, c.nombre,
                  CAST(julianday(?) - julianday(pp.fecha_programada) AS INTEGER) as dias_vencido
           FROM plan_pagos pp
           JOIN reservas r ON pp.id_reserva = r.id_reserva
           JOIN clientes c ON r.id_cliente = c.id_cliente
           WHERE pp.estado IN ('PENDIENTE','PARCIAL') AND r.estado = 'ACTIVO'
             AND pp.fecha_programada <= date(?, '+7 days')
           ORDER BY pp.fecha_programada LIMIT ?""",
        (hoy, hoy, int(limit))
    )
    cobranza = _df_a_registros(df_pagos)

    df_prov = obtener_datos(
        """SELECT r.id_reserva, c.nombre, r.destino, r.fecha_limite_proveedor, r.costo_total,
                  COALESCE((SELECT SUM(monto) FROM flujo_caja fc
                            WHERE fc.id_reserva = r.id_reserva AND fc.tipo_movimiento='EGRESO'
                              AND fc.estado='ACTIVO' AND fc.tipo_egreso='COSTO DIRECTO VIAJE'), 0) as pagado_prov,
                  CAST(julianday(r.fecha_limite_proveedor) - julianday(?) AS INTEGER) as dias_para_vencer
           FROM reservas r JOIN clientes c ON r.id_cliente = c.id_cliente
           WHERE r.estado='ACTIVO' AND r.id_grupo IS NULL
             AND r.fecha_limite_proveedor IS NOT NULL AND r.fecha_limite_proveedor != ''
             AND r.fecha_limite_proveedor <= date(?, '+15 days')
           ORDER BY r.fecha_limite_proveedor LIMIT ?""",
        (hoy, hoy, int(limit))
    )
    pago_proveedor = [
        row for row in _df_a_registros(df_prov)
        if float(row["costo_total"] or 0) - float(row["pagado_prov"] or 0) > 0.01
    ]

    df_checkin = obtener_datos(
        """SELECT r.id_reserva, c.nombre, r.destino, r.fecha_salida, r.fecha_regreso,
                  r.checkin_ida, r.checkin_regreso
           FROM reservas r JOIN clientes c ON r.id_cliente = c.id_cliente
           WHERE r.estado='ACTIVO'
             AND ((r.fecha_salida <= date(?, '+3 days') AND r.checkin_ida = 0)
               OR (r.fecha_regreso <= date(?, '+3 days') AND r.checkin_regreso = 0))
           ORDER BY r.fecha_salida LIMIT ?""",
        (hoy, hoy, int(limit))
    )
    checkins = _df_a_registros(df_checkin)

    return {
        "cobranza_proxima_o_vencida": cobranza,
        "pago_proveedor_proximo_o_vencido": pago_proveedor,
        "checkins_pendientes": checkins,
    }


def cumpleanos_proximos(dias: int = 7):
    df = obtener_datos(
        """SELECT id_cliente, nombre, fecha_nacimiento,
                  strftime('%m-%d', fecha_nacimiento) as mes_dia
           FROM clientes WHERE fecha_nacimiento IS NOT NULL AND fecha_nacimiento != ''"""
    )
    if df.empty:
        return []
    hoy = now_local().date()
    resultado = []
    for row in df.to_dict("records"):
        try:
            mes, dia = row["mes_dia"].split("-")
            prox = row["fecha_nacimiento"]
            candidato = hoy.replace(month=int(mes), day=int(dia))
            if candidato < hoy:
                candidato = candidato.replace(year=hoy.year + 1)
            faltan = (candidato - hoy).days
            if 0 <= faltan <= int(dias):
                resultado.append({
                    "id_cliente": row["id_cliente"], "nombre": row["nombre"],
                    "fecha_nacimiento": row["fecha_nacimiento"], "dias_faltantes": faltan,
                })
        except Exception:
            continue
    resultado.sort(key=lambda r: r["dias_faltantes"])
    return resultado


def catalogo_hoteles(filtro: str = ""):
    nombres = get_catalogo_hoteles()
    return [n for n in nombres if filtro.lower() in n.lower()] if filtro else nombres


def catalogo_aerolineas(filtro: str = ""):
    nombres = get_catalogo_aerolineas()
    return [n for n in nombres if filtro.lower() in n.lower()] if filtro else nombres


def catalogo_mayoristas(filtro: str = ""):
    nombres = get_catalogo_mayoristas()
    return [n for n in nombres if filtro.lower() in n.lower()] if filtro else nombres


def catalogo_proveedores(tipo: str, filtro: str = ""):
    mapa = {
        "traslados": get_catalogo_proveedores_traslados,
        "tours": get_catalogo_proveedores_tours,
        "adicionales": get_catalogo_proveedores_adicionales,
    }
    fn = mapa.get(tipo)
    if not fn:
        return {"error": f"tipo desconocido: {tipo}. Usa 'traslados', 'tours' o 'adicionales'."}
    nombres = fn()
    return [n for n in nombres if filtro.lower() in n.lower()] if filtro else nombres


def datos_itinerario_para_mensaje(id_reserva: int):
    df = obtener_datos(
        """SELECT r.id_reserva, c.nombre, c.telefono, r.destino, r.fecha_salida, r.fecha_regreso,
                  r.venta_total, r.cobrado_cliente, r.moneda, r.estado,
                  r.nombre_hotel, r.aerolinea, r.mayorista, r.hora_vuelo_ida, r.hora_vuelo_vuelta
           FROM reservas r JOIN clientes c ON r.id_cliente = c.id_cliente
           WHERE r.id_reserva = ?""",
        (id_reserva,)
    )
    if df.empty:
        return {"encontrado": False}
    itin = df.to_dict("records")[0]
    saldo = calcular_saldo_real(int(id_reserva))
    df_hab = obtener_datos(
        "SELECT tipo_habitacion, num_personas, hora_checkin FROM habitaciones_reserva WHERE id_reserva=?",
        (id_reserva,)
    )
    df_plan = obtener_datos(
        """SELECT numero_pago, ROUND(monto_esperado - monto_pagado, 2) as monto_pendiente, fecha_programada, estado
           FROM plan_pagos WHERE id_reserva=? AND estado IN ('PENDIENTE','PARCIAL')
           ORDER BY fecha_programada""",
        (id_reserva,)
    )
    return {
        "encontrado": True,
        "itinerario": itin,
        "saldo": saldo,
        "habitaciones": _df_a_registros(df_hab),
        "proximas_parcialidades": _df_a_registros(df_plan),
    }


# ─── Tools de escritura controlada ────────────────────────────────────────────

def preparar_cliente_nuevo(id_conversacion: int, datos: dict):
    """NO escribe en `clientes` — solo arma un borrador en asistente_ia_clientes_pendientes con
    los avisos de posibles duplicados (mismo criterio que el formulario humano de Nuevo
    Cliente), para que el humano decida si confirma desde la tarjeta de la UI."""
    nombre = (datos.get("nombre") or "").strip()
    if not nombre:
        return {"error": "Falta el nombre del cliente."}
    telefono = (datos.get("telefono") or "").strip()
    if telefono and len("".join(c for c in telefono if c.isdigit())) != 10:
        return {"error": "El teléfono debe tener exactamente 10 dígitos."}
    email = (datos.get("email") or "").strip().lower()

    avisos = buscar_duplicados_cliente(nombre, telefono, email)
    resumen = (
        f"Nombre: {nombre}\n"
        f"Teléfono: {telefono or '—'}\n"
        f"Email: {email or '—'}\n"
        f"Fecha de nacimiento: {datos.get('fecha_nacimiento') or '—'}"
    )
    if avisos:
        resumen += "\n\n⚠️ Posibles duplicados:\n" + "\n".join(f"- {a}" for a in avisos)

    payload_json = json.dumps(datos, ensure_ascii=False)
    ejecutar_comando(
        "INSERT INTO asistente_ia_clientes_pendientes (id_conversacion, payload_json, resumen_texto) VALUES (?,?,?)",
        (id_conversacion, payload_json, resumen)
    )
    df_new = obtener_datos("SELECT MAX(id) as n FROM asistente_ia_clientes_pendientes")
    id_propuesta = int(df_new.iloc[0]["n"])
    return {"id_propuesta_cliente": id_propuesta, "resumen": resumen, "posibles_duplicados": avisos}


def obtener_cliente(id_cliente: str):
    """Ficha completa de un cliente por su id exacto (a diferencia de buscar_cliente, que es
    búsqueda difusa por texto) — úsala antes de proponer una edición, para saber los valores
    actuales y no perderlos al fusionar el cambio solicitado."""
    df = obtener_datos("SELECT * FROM clientes WHERE id_cliente=?", (id_cliente,))
    if df.empty:
        return {"encontrado": False}
    return {"encontrado": True, **df.to_dict("records")[0]}


def preparar_edicion_cliente(id_conversacion: int, id_cliente: str, cambios: dict):
    """NO escribe en `clientes` — arma una propuesta de EDICIÓN en asistente_ia_clientes_pendientes
    (misma tabla que preparar_cliente_nuevo, pero con id_cliente_existente puesto) mostrando
    solo los campos que cambian (antes → después), para que el humano confirme."""
    df = obtener_datos("SELECT * FROM clientes WHERE id_cliente=?", (id_cliente,))
    if df.empty:
        return {"error": f"El id_cliente '{id_cliente}' no existe."}
    actual = df.to_dict("records")[0]

    fusionado = dict(actual)
    diffs = []
    for campo in ("nombre", "telefono", "email", "fecha_nacimiento", "codigo_pais"):
        if campo in cambios and cambios[campo] is not None and str(cambios[campo]).strip() != "":
            nuevo = str(cambios[campo]).strip()
            viejo = actual.get(campo) or "—"
            if nuevo != str(actual.get(campo) or ""):
                diffs.append(f"{campo}: {viejo} → {nuevo}")
            fusionado[campo] = nuevo

    if not diffs:
        return {"error": "No especificaste ningún cambio real respecto a los datos actuales."}

    telefono = (fusionado.get("telefono") or "").strip()
    if telefono and len("".join(c for c in telefono if c.isdigit())) != 10:
        return {"error": "El teléfono debe tener exactamente 10 dígitos."}

    resumen = f"Cliente: {actual['nombre']} ({id_cliente})\n\nCambios:\n" + "\n".join(f"- {d}" for d in diffs)
    payload_json = json.dumps(fusionado, ensure_ascii=False)
    ejecutar_comando(
        "INSERT INTO asistente_ia_clientes_pendientes (id_conversacion, payload_json, resumen_texto, id_cliente_existente) VALUES (?,?,?,?)",
        (id_conversacion, payload_json, resumen, id_cliente)
    )
    df_new = obtener_datos("SELECT MAX(id) as n FROM asistente_ia_clientes_pendientes")
    id_propuesta = int(df_new.iloc[0]["n"])
    return {"id_propuesta_cliente": id_propuesta, "resumen": resumen}


def preparar_cotizacion(id_conversacion: int, datos: dict):
    """NO escribe en `cotizaciones` — solo arma un borrador en asistente_ia_cotizaciones_pendientes
    y un resumen legible para que el humano confirme desde la tarjeta de la UI."""
    def _f(k): return float(datos.get(k) or 0)

    id_cliente = (datos.get("id_cliente") or "").strip()
    if not id_cliente:
        return {"error": "Falta id_cliente. Usa buscar_cliente primero para obtener el id exacto — nunca prepares una cotización sin un cliente real ya registrado en el sistema."}
    df_cliente = obtener_datos("SELECT nombre FROM clientes WHERE id_cliente=?", (id_cliente,))
    if df_cliente.empty:
        return {"error": f"El id_cliente '{id_cliente}' no existe en el sistema. Usa buscar_cliente para encontrar el id correcto, o dile al usuario que registre primero al cliente en Clientes."}
    nombre_cliente = df_cliente.iloc[0]["nombre"]

    destino = (datos.get("destino") or "").strip()
    if not destino:
        return {"error": "Falta el destino."}
    venta = _f("cobro_vuelos") + _f("cobro_tua") + _f("cobro_hotel") + _f("cobro_traslados") + _f("cobro_tours") + _f("cobro_adicionales")
    if venta <= 0:
        return {"error": "El total de la cotización debe ser mayor a cero."}

    moneda = datos.get("moneda") or "MXN"
    resumen = (
        f"Cliente: {nombre_cliente} ({id_cliente})\n"
        f"Destino: {destino}\n"
        f"Fechas: {datos.get('fecha_salida', '?')} a {datos.get('fecha_regreso', '?')}\n"
        f"Hotel: {datos.get('nombre_hotel') or '—'}\n"
        f"Aerolínea: {datos.get('aerolinea') or '—'} · Mayorista: {datos.get('mayorista') or '—'}\n"
        f"Venta total: ${venta:,.2f} {moneda}"
    )
    payload_json = json.dumps(datos, ensure_ascii=False)
    ejecutar_comando(
        "INSERT INTO asistente_ia_cotizaciones_pendientes (id_conversacion, payload_json, resumen_texto) VALUES (?,?,?)",
        (id_conversacion, payload_json, resumen)
    )
    df_new = obtener_datos("SELECT MAX(id) as n FROM asistente_ia_cotizaciones_pendientes")
    id_propuesta = int(df_new.iloc[0]["n"])
    return {"id_propuesta": id_propuesta, "resumen": resumen}


def previsualizar_conversion(id_cotizacion: int):
    """Solo lectura — resumen de venta/costo/utilidad/hotel elegido de una cotización YA
    guardada en `cotizaciones`, para que el modelo lo presente antes de la confirmación humana."""
    df = obtener_datos("SELECT * FROM cotizaciones WHERE id_cotizacion=?", (id_cotizacion,))
    if df.empty:
        return {"encontrado": False}
    cot = df.to_dict("records")[0]
    if cot.get("convertida_a_reserva"):
        return {"encontrado": True, "ya_convertida": True, "id_reserva": int(cot["convertida_a_reserva"])}
    opciones = []
    for n, nombre_k, cobro_k, costo_k in [
        (1, "nombre_hotel", "cobro_hotel", "costo_hotel"),
        (2, "hotel_op2_nombre", "hotel_op2_cobro", "hotel_op2_costo"),
        (3, "hotel_op3_nombre", "hotel_op3_cobro", "hotel_op3_costo"),
    ]:
        cobro = float(cot.get(cobro_k) or 0)
        if n == 1 or cobro > 0:
            opciones.append({
                "opcion": n, "nombre_hotel": cot.get(nombre_k) or None,
                "cobro_hotel": cobro, "costo_hotel": float(cot.get(costo_k) or 0),
            })
    return {
        "encontrado": True, "ya_convertida": False,
        "destino": cot["destino"], "fecha_salida": cot["fecha_salida"], "fecha_regreso": cot["fecha_regreso"],
        "venta_total": float(cot["venta_total"] or 0), "moneda": cot["moneda"],
        "opciones_hotel": opciones,
    }


_MANUAL_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "MANUAL.md")
_STOPWORDS = {
    "que", "como", "para", "por", "una", "uno", "los", "las", "del", "con", "sin",
    "puedo", "quiero", "hacer", "hago", "se", "el", "la", "de", "en", "un", "y",
    "es", "lo", "al", "un", "mi", "su", "le", "no", "si",
}


def _cargar_secciones_manual():
    """Parte MANUAL.md en secciones por encabezado '## ' — cachea en memoria del
    proceso porque el archivo no cambia en caliente (solo se edita a mano)."""
    global _secciones_manual_cache
    try:
        return _secciones_manual_cache
    except NameError:
        pass
    with open(_MANUAL_PATH, encoding="utf-8") as f:
        texto = f.read()
    partes = re.split(r"^## ", texto, flags=re.MULTILINE)[1:]
    secciones = []
    for parte in partes:
        titulo = parte.split("\n", 1)[0].strip()
        secciones.append({"titulo": titulo, "texto": "## " + parte.strip()})
    _secciones_manual_cache = secciones
    return secciones


def consultar_manual(tema: str):
    """Busca en el manual de usuario (MANUAL.md) las 1-2 secciones más relevantes al
    tema pedido, por coincidencia de palabras — para no mandar el manual completo en
    cada mensaje (gasta tokens de más). Si no hay buena coincidencia, regresa la lista
    de temas disponibles para que se pueda reformular."""
    secciones = _cargar_secciones_manual()
    palabras = [
        w for w in re.findall(r"[a-záéíóúñ]+", tema.lower())
        if len(w) > 2 and w not in _STOPWORDS
    ]
    if not palabras:
        return {"temas_disponibles": [s["titulo"] for s in secciones]}

    puntajes = []
    for s in secciones:
        texto_l = s["texto"].lower()
        puntaje = sum(texto_l.count(w) for w in palabras)
        if puntaje > 0:
            puntajes.append((puntaje, s))
    puntajes.sort(key=lambda t: -t[0])

    if not puntajes:
        return {"encontrado": False, "temas_disponibles": [s["titulo"] for s in secciones]}
    return {"encontrado": True, "secciones": [s["texto"] for _, s in puntajes[:2]]}
