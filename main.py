import os
import secrets
import logging
import time
import re as _re_main
import urllib.parse
import pathlib
import json
import pandas as pd
from html import escape as _esc
from fastapi import FastAPI, Request, Form, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse, FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware
from jinja2 import Environment, FileSystemLoader

from database import (
    verificar_tablas, obtener_datos, calcular_saldo_real, distribuir_equitativo,
    ejecutar_comando, ejecutar_transaccion, ejecutar_insert, registrar_cambio,
    now_local, actualizar_estado_plan_pagos,
    crear_cotizacion, convertir_cotizacion, _calcular_plan_fechas,
    crear_cliente, buscar_duplicados_cliente,
    registrar_intento_login, verificar_bloqueo_login, limpiar_intentos_login,
    get_catalogo_hoteles, get_catalogo_aerolineas, get_catalogo_mayoristas,
    get_catalogo_proveedores_traslados, get_catalogo_proveedores_tours, get_catalogo_proveedores_adicionales,
    get_catalogo_equipaje, get_catalogo_destinos,
    upsert_catalogo_hotel, upsert_catalogo_aerolinea, upsert_catalogo_mayorista,
    upsert_catalogo_proveedor_traslados, upsert_catalogo_proveedor_tours, upsert_catalogo_proveedor_adicionales,
    upsert_catalogo_equipaje, upsert_catalogo_destino,
    obtener_token_portal, obtener_reserva_por_token_portal
)
from wa import wa_router, _contar_wa_nuevas
from dotenv import load_dotenv
load_dotenv()
from asistente_ia import asistente_ia_router

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

app = FastAPI(title="ERP Agencia de Viajes")

# ── Cabeceras de seguridad HTTP ───────────────────────────────────────────────
@app.middleware("http")
async def security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers["X-Frame-Options"] = "SAMEORIGIN"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    response.headers["X-XSS-Protection"] = "1; mode=block"
    return response

# ── Candado real de cambio de contraseña forzado ──────────────────────────────
# El login solo hacía un redirect de una sola vez a /cambiar-password — nada
# impedía navegar a otra pantalla en su lugar y quedarte con la contraseña
# vieja/temporal. Este middleware bloquea CUALQUIER otra ruta mientras la
# sesión tenga la bandera activa (se marca en /login, se limpia al cambiar
# la contraseña con éxito en /cambiar-password).
_RUTAS_PERMITIDAS_FORZAR_PWD = {"/cambiar-password", "/logout"}

@app.middleware("http")
async def forzar_cambio_password(request: Request, call_next):
    if (request.session.get("forzar_cambio_pwd")
            and request.url.path not in _RUTAS_PERMITIDAS_FORZAR_PWD
            and not request.url.path.startswith("/static")):
        return RedirectResponse(url="/cambiar-password")
    return await call_next(request)

# Se agrega AL FINAL a propósito: en Starlette, cada add_middleware()/@app.middleware
# envuelve a los anteriores, así que el último en registrarse queda como la capa
# más externa y corre primero — necesitamos que SessionMiddleware llene
# request.session ANTES de que los middlewares de arriba intenten leerlo.
app.add_middleware(SessionMiddleware, secret_key=secrets.token_hex(32), same_site="strict", https_only=False)

app.mount("/static", StaticFiles(directory="static"), name="static")
# cache_size=0: workaround para bug de Jinja2 3.1.6 con Python 3.14
_jinja_env = Environment(loader=FileSystemLoader("templates"), autoescape=True, cache_size=0)
templates = Jinja2Templates(env=_jinja_env)

def _fmt_moneda(value, moneda="MXN"):
    try:
        return f"${float(value):,.0f} {moneda}"
    except Exception:
        return "$0 MXN"

_jinja_env.filters["moneda"] = _fmt_moneda

verificar_tablas()
if os.environ.get("HIDE_WHATSAPP") != "1":
    app.include_router(wa_router)
app.include_router(asistente_ia_router)


# ─── Helpers ────────────────────────────────────────────────────────────────

_SESSION_TIMEOUT_SEC = 7200  # 2 horas de inactividad

def usuario_activo(request: Request):
    usuario = request.session.get("usuario")
    if not usuario:
        return None
    last_hb = request.session.get("_last_hb", 0)
    if time.time() - last_hb > _SESSION_TIMEOUT_SEC:
        request.session.clear()
        return None
    return usuario

def _contar_criticas() -> int:
    """Cuenta alertas críticas activas para el badge del sidebar."""
    df = obtener_datos(f"""
        SELECT COUNT(*) as n FROM (
            SELECT 1 FROM plan_pagos pp
            JOIN reservas r ON pp.id_reserva = r.id_reserva
            WHERE pp.estado IN ('PENDIENTE','PARCIAL')
              AND pp.fecha_programada < DATE('now')
              AND r.estado = 'ACTIVO'
            UNION ALL
            SELECT 1 FROM reservas r
            WHERE r.estado = 'ACTIVO'
              AND r.fecha_limite_proveedor IS NOT NULL
              AND r.fecha_limite_proveedor != ''
              AND r.fecha_limite_proveedor != 'None'
              AND CAST(julianday(r.fecha_limite_proveedor) - julianday('now') AS INTEGER) BETWEEN 0 AND 7
              AND {_DEUDA_PROV_EXPR} > 1
        )
    """)
    return int(df.iloc[0]["n"]) if not df.empty else 0

def ctx(request: Request, extra: dict = None):
    """Contexto base para todos los templates."""
    usuario = request.session.get("usuario")
    rol     = request.session.get("rol", "")

    # Heartbeat: actualiza sesiones_activas máx. 1 vez cada 30s por usuario
    if usuario:
        import time as _time
        now_ts = _time.time()
        if now_ts - request.session.get("_last_hb", 0) > 30:
            ejecutar_comando(
                "INSERT OR REPLACE INTO sesiones_activas (usuario, rol, ultima_actividad) VALUES (?,?,?)",
                (usuario, rol, now_local().strftime("%Y-%m-%d %H:%M:%S"))
            )
            request.session["_last_hb"] = now_ts

    base = {
        "usuario": usuario,
        "rol":     rol,
        "n_criticas":  _contar_criticas() if usuario else 0,
        "n_wa_nuevas": _contar_wa_nuevas() if usuario else 0,
        "flash":   request.session.pop("flash", None),
        "mostrar_whatsapp": os.environ.get("HIDE_WHATSAPP") != "1",
    }
    if extra:
        base.update(extra)
    return base


def ctx_catalogos_reserva():
    """Catálogos para autocompletar en el formulario de reserva/itinerario."""
    return {
        "catalogo_destinos": get_catalogo_destinos(),
        "catalogo_hoteles": get_catalogo_hoteles(),
        "catalogo_aerolineas": get_catalogo_aerolineas(),
        "catalogo_mayoristas": get_catalogo_mayoristas(),
        "catalogo_prov_traslados": get_catalogo_proveedores_traslados(),
        "catalogo_prov_tours": get_catalogo_proveedores_tours(),
        "catalogo_prov_adicionales": get_catalogo_proveedores_adicionales(),
        "catalogo_equipaje": get_catalogo_equipaje(),
    }


def ctx_catalogos_cotizacion():
    """Catálogos para autocompletar en el formulario de cotización (sin proveedor_tours/adicionales: el form no los tiene)."""
    return {
        "catalogo_destinos": get_catalogo_destinos(),
        "catalogo_hoteles": get_catalogo_hoteles(),
        "catalogo_aerolineas": get_catalogo_aerolineas(),
        "catalogo_mayoristas": get_catalogo_mayoristas(),
        "catalogo_prov_traslados": get_catalogo_proveedores_traslados(),
        "catalogo_equipaje": get_catalogo_equipaje(),
    }


# ─── Auth ────────────────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def root(request: Request):
    if not usuario_activo(request):
        return RedirectResponse(url="/login")
    return RedirectResponse(url="/dashboard")


@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    if usuario_activo(request):
        return RedirectResponse(url="/dashboard")
    return templates.TemplateResponse(request, "login.html")


# ─── Manual de usuario (solo lectura, requiere login) ───────────────────────

_MANUAL_DIR = (pathlib.Path(__file__).parent / "docs" / "manual").resolve()
_MANUAL_RUTAS_ADMIN = {"12-admin.html", "content/12-admin.html"}

@app.get("/manual")
async def manual_root(request: Request):
    if not usuario_activo(request):
        return RedirectResponse(url="/login")
    rol = request.session.get("rol", "")
    return RedirectResponse(url=f"/manual/index.html?rol={rol}")

@app.get("/manual/")
async def manual_root_slash(request: Request):
    if not usuario_activo(request):
        return RedirectResponse(url="/login")
    rol = request.session.get("rol", "")
    return RedirectResponse(url=f"/manual/index.html?rol={rol}")

@app.get("/manual/{path:path}")
async def manual_asset(request: Request, path: str):
    if not usuario_activo(request):
        return RedirectResponse(url="/login")
    rol = request.session.get("rol", "")
    if path in _MANUAL_RUTAS_ADMIN and rol != "admin":
        raise HTTPException(status_code=403, detail="Esta sección del manual es solo para administradores")
    target = (_MANUAL_DIR / path).resolve()
    if _MANUAL_DIR not in target.parents and target != _MANUAL_DIR:
        raise HTTPException(status_code=404)
    if not target.is_file():
        raise HTTPException(status_code=404)
    if path == "search-index.json" and rol != "admin":
        indice = json.loads(target.read_text(encoding="utf-8"))
        indice = [item for item in indice if item.get("href") not in _MANUAL_RUTAS_ADMIN]
        return JSONResponse(indice)
    return FileResponse(target)


@app.post("/login", response_class=HTMLResponse)
async def login(request: Request, usuario: str = Form(...), password: str = Form(...)):
    from auth import verificar_password
    _usuario = usuario.strip()
    # ── Rate limiting por usuario (persistente en BD) ─────────────────────────
    bloqueado, seg_rest, _ = verificar_bloqueo_login(_usuario)
    if bloqueado:
        minutos = int(seg_rest / 60) + 1
        logging.warning(f"Login bloqueado (rate limit BD) para '{_usuario}'")
        return templates.TemplateResponse(request, "login.html",
            {"error": f"🔒 Demasiados intentos fallidos. Espera {minutos} minuto(s) e inténtalo de nuevo."})
    # ─────────────────────────────────────────────────────────────────────────
    df = obtener_datos("SELECT usuario, password, rol FROM usuarios WHERE usuario = ?", (_usuario,))
    error = "Usuario o contraseña incorrectos"
    if not df.empty:
        row = df.iloc[0]
        if verificar_password(password, row["password"]):
            limpiar_intentos_login(_usuario)
            registrar_intento_login(_usuario, exitoso=True)
            request.session["usuario"]  = row["usuario"]
            request.session["rol"]      = row["rol"]
            request.session["_last_hb"] = time.time()
            logging.info(f"Login exitoso: {row['usuario']}")

            # Forzar cambio si primer_login=1
            df_pl = obtener_datos("SELECT primer_login, ultima_cambio_password FROM usuarios WHERE usuario=?", (row["usuario"],))
            _dias_pwd = 0
            if not df_pl.empty:
                if int(df_pl.iloc[0]["primer_login"] or 0) == 1:
                    request.session["forzar_cambio_pwd"] = True
                    request.session["flash"] = {"tipo": "error", "texto": "🔑 Debes crear una nueva contraseña antes de continuar."}
                    return RedirectResponse(url="/cambiar-password", status_code=303)
                _ult_pwd = df_pl.iloc[0]["ultima_cambio_password"]
                if _ult_pwd:
                    try:
                        from datetime import datetime as _dt_pwd
                        _dias_pwd = (now_local().date() - _dt_pwd.strptime(str(_ult_pwd)[:10], "%Y-%m-%d").date()).days
                    except Exception:
                        _dias_pwd = 0
            if _dias_pwd >= 60:
                request.session["forzar_cambio_pwd"] = True
                request.session["flash"] = {"tipo": "error", "texto": f"🔑 Tu contraseña lleva {_dias_pwd} días sin cambiar (límite 60). Actualízala para continuar."}
                return RedirectResponse(url="/cambiar-password", status_code=303)

            # Calcular alertas para flash de bienvenida
            df_at = obtener_datos(
                "SELECT COUNT(*) as n FROM plan_pagos pp JOIN reservas r ON pp.id_reserva=r.id_reserva "
                "WHERE pp.estado IN ('PENDIENTE','PARCIAL') AND pp.fecha_programada < DATE('now') AND r.estado='ACTIVO'"
            )
            df_pr = obtener_datos(
                f"SELECT COUNT(*) as n FROM reservas r WHERE r.estado='ACTIVO' "
                "AND r.fecha_limite_proveedor IS NOT NULL AND r.fecha_limite_proveedor != '' "
                "AND r.fecha_limite_proveedor != 'None' "
                "AND CAST(julianday(r.fecha_limite_proveedor) - julianday('now') AS INTEGER) BETWEEN 0 AND 7 "
                f"AND {_DEUDA_PROV_EXPR} > 1"
            )
            n_at = int(df_at.iloc[0]["n"]) if not df_at.empty else 0
            n_pr = int(df_pr.iloc[0]["n"]) if not df_pr.empty else 0
            if n_at > 0 or n_pr > 0:
                partes = []
                if n_at > 0: partes.append(f"🛑 {n_at} pago(s) de cliente atrasado(s)")
                if n_pr > 0: partes.append(f"🚨 {n_pr} pago(s) a proveedor vencen en ≤7 días")
                request.session["flash"] = {"tipo": "error", "texto": " · ".join(partes) + " — revisa Alertas."}
            else:
                palabras = ["Buenos días", "Buen día", "Buenas tardes", "Bienvenido"]
                import datetime as _dt
                hora = now_local().hour
                saludo = "Buenos días" if hora < 12 else ("Buenas tardes" if hora < 19 else "Buenas noches")
                request.session["flash"] = {"tipo": "ok", "texto": f"✅ Sin alertas críticas. {saludo}, {row['usuario']}."}
            if 45 <= _dias_pwd < 60:
                flash_actual = request.session.get("flash", {})
                aviso_pwd = f"⚠️ Tu contraseña tiene {_dias_pwd} días sin cambiar (se bloqueará a los 60 días)."
                request.session["flash"] = {"tipo": "warn", "texto": aviso_pwd + (" " + flash_actual.get("texto", "") if flash_actual else "")}
            return RedirectResponse(url="/dashboard", status_code=303)
    registrar_intento_login(_usuario, exitoso=False)
    bloqueado2, _, n_int = verificar_bloqueo_login(_usuario)
    if bloqueado2:
        error = "🔒 Cuenta bloqueada por 15 minutos por demasiados intentos fallidos."
    else:
        restantes = max(0, 5 - n_int)
        error = f"Usuario o contraseña incorrectos. ({restantes} intento(s) restante(s))"
    logging.warning(f"Login fallido para '{_usuario}' (intento {n_int}/5)")
    return templates.TemplateResponse(request, "login.html", {"error": error})


@app.get("/logout")
async def logout(request: Request):
    request.session.clear()
    return RedirectResponse(url="/login")


# ─── Dashboard ───────────────────────────────────────────────────────────────

def _auto_terminar():
    """Marca como TERMINADO las reservas ACTIVAS cuya fecha de regreso ya pasó."""
    df = obtener_datos(
        "SELECT id_reserva FROM reservas WHERE estado='ACTIVO' AND fecha_regreso < DATE('now')"
    )
    if df.empty:
        return
    for _, row in df.iterrows():
        rid = int(row["id_reserva"])
        ejecutar_comando("UPDATE reservas SET estado='TERMINADO' WHERE id_reserva=?", (rid,))
        registrar_cambio(rid, "TERMINADO", "Marcado automáticamente al vencer la fecha de regreso", usuario="sistema")
    logging.info(f"Auto-terminadas {len(df)} reserva(s).")


@app.get("/dashboard", response_class=HTMLResponse)
async def dashboard(request: Request, mes: str = "", anio: str = ""):
    if not usuario_activo(request):
        return RedirectResponse(url="/login")

    _auto_terminar()

    import json as _json
    import datetime as _dt_dash
    import calendar as _cal_dash

    hoy       = now_local().date()
    hoy_str   = str(hoy)

    # Período seleccionado para la caja
    _anio_sel = int(anio) if anio and anio.isdigit() else hoy.year
    _mes_sel  = mes if mes in [f"{m:02d}" for m in range(1, 13)] else hoy.strftime('%m')
    mes_str   = f"{_anio_sel}-{_mes_sel}"
    mes_nombre = f"{_cal_dash.month_name[int(_mes_sel)].capitalize()} {_anio_sel}"
    es_mes_actual = (mes_str == hoy.strftime('%Y-%m'))
    anios_disp = [str(y) for y in range(hoy.year - 2, hoy.year + 2)]

    # ── KPIs base ────────────────────────────────────────────────────────────
    df_estados = obtener_datos("SELECT estado, COUNT(*) as total FROM reservas GROUP BY estado")
    df_saldo   = obtener_datos("SELECT COALESCE(SUM(venta_total-cobrado_cliente),0) as pendiente FROM reservas WHERE estado='ACTIVO'")
    df_cob_hoy = obtener_datos("SELECT COALESCE(SUM(monto),0) as t FROM flujo_caja WHERE tipo_movimiento='INGRESO' AND estado='ACTIVO' AND DATE(fecha_pago)=DATE('now')")
    df_vencidos= obtener_datos("SELECT COUNT(*) as n FROM plan_pagos pp JOIN reservas r ON pp.id_reserva=r.id_reserva WHERE pp.estado IN ('PENDIENTE','PARCIAL') AND pp.fecha_programada<DATE('now') AND r.estado='ACTIVO'")
    df_movhoy  = obtener_datos("SELECT COUNT(*) as n FROM reservas WHERE estado='ACTIVO' AND (fecha_salida=? OR fecha_regreso=?)", (hoy_str, hoy_str))
    df_deuda   = obtener_datos("SELECT COUNT(*) as n FROM reservas WHERE estado='ACTIVO' AND fecha_regreso<? AND (venta_total-cobrado_cliente)>1.0", (hoy_str,))

    # ── Caja del mes ─────────────────────────────────────────────────────────
    df_ing = obtener_datos("SELECT COALESCE(SUM(monto),0) as t, moneda FROM flujo_caja WHERE tipo_movimiento='INGRESO' AND estado='ACTIVO' AND fecha_pago LIKE ? GROUP BY moneda", (f"{mes_str}%",))
    df_egr = obtener_datos("SELECT COALESCE(SUM(monto),0) as t, moneda FROM flujo_caja WHERE tipo_movimiento='EGRESO'  AND estado='ACTIVO' AND fecha_pago LIKE ? GROUP BY moneda", (f"{mes_str}%",))

    def _mon(df, cur):
        sub = df[df['moneda'] == cur]
        return float(sub['t'].iloc[0]) if not sub.empty else 0.0

    ing_mxn = _mon(df_ing, 'MXN'); egr_mxn = _mon(df_egr, 'MXN')
    ing_usd = _mon(df_ing, 'USD'); egr_usd = _mon(df_egr, 'USD')

    # ── Próximas salidas ─────────────────────────────────────────────────────
    df_proximas = obtener_datos(
        "SELECT r.id_reserva, c.nombre, r.destino, r.fecha_salida, r.estado, "
        "(r.venta_total - r.cobrado_cliente) as saldo, r.moneda "
        "FROM reservas r JOIN clientes c ON r.id_cliente=c.id_cliente "
        "WHERE r.estado='ACTIVO' AND r.fecha_salida BETWEEN DATE('now') AND DATE('now','+30 days') "
        "ORDER BY r.fecha_salida ASC LIMIT 8"
    )

    # ── Check-ins de vuelo pendientes (próximas 48h o ya vencidos) ──────────
    # Los pases de abordaje se otorgan 48h antes del vuelo (ver manual) — se
    # avisa desde que entra en esa ventana hasta que se marca hecho.
    df_checkins = obtener_datos(
        "SELECT v.id_vuelo, v.id_reserva, v.numero_tramo, v.aerolinea, v.numero_vuelo, "
        "v.origen, v.destino, v.fecha, v.hora, r.destino as destino_viaje, c.nombre as nombre_cliente "
        "FROM vuelos_reserva v "
        "JOIN reservas r ON v.id_reserva = r.id_reserva "
        "JOIN clientes c ON r.id_cliente = c.id_cliente "
        "WHERE v.checkin = 0 AND r.estado = 'ACTIVO' "
        "AND v.fecha IS NOT NULL AND v.fecha != '' "
        "AND DATE(v.fecha) <= DATE('now', '+2 days') "
        "ORDER BY v.fecha ASC, v.hora ASC LIMIT 15"
    )

    # ── Gráfica 1: Ingresos vs Egresos MXN últimos 6 meses ──────────────────
    fecha_6m = str((_dt_dash.date(hoy.year, hoy.month, 1) - _dt_dash.timedelta(days=155)))
    df_tend = obtener_datos("""
        SELECT strftime('%Y-%m', fecha_pago) as mes, tipo_movimiento,
               COALESCE(SUM(monto),0) as total
        FROM flujo_caja
        WHERE estado='ACTIVO' AND moneda='MXN' AND fecha_pago >= ?
        GROUP BY mes, tipo_movimiento ORDER BY mes ASC
    """, (fecha_6m,))

    meses_labels, ing_vals, egr_vals = [], [], []
    if not df_tend.empty:
        todos_meses = sorted(df_tend['mes'].unique().tolist())
        ing_map = {}; egr_map = {}
        for _, row in df_tend.iterrows():
            if row['tipo_movimiento'] == 'INGRESO':
                ing_map[row['mes']] = float(row['total'])
            else:
                egr_map[row['mes']] = float(row['total'])
        for m in todos_meses:
            yyyy, mm = m.split('-')
            meses_labels.append(f"{_cal_dash.month_abbr[int(mm)].capitalize()} {yyyy[-2:]}")
            ing_vals.append(ing_map.get(m, 0))
            egr_vals.append(egr_map.get(m, 0))

    chart_tendencia = _json.dumps({"labels": meses_labels, "ingresos": ing_vals, "egresos": egr_vals}).replace("</", "<\\/")

    # ── Gráfica 2: Top 7 destinos por utilidad proyectada ───────────────────
    df_dest = obtener_datos("""
        SELECT destino, COALESCE(SUM(utilidad_proyectada),0) as utilidad, COUNT(*) as viajes
        FROM reservas WHERE estado != 'CANCELADO' AND moneda='MXN'
        GROUP BY destino ORDER BY utilidad DESC LIMIT 7
    """)
    if not df_dest.empty:
        chart_destinos = _json.dumps({
            "labels":  df_dest['destino'].tolist(),
            "valores": [round(float(v), 2) for v in df_dest['utilidad'].tolist()],
            "viajes":  [int(v) for v in df_dest['viajes'].tolist()],
        }).replace("</", "<\\/")
    else:
        chart_destinos = _json.dumps({"labels": [], "valores": [], "viajes": []})

    # ── Armar contexto ───────────────────────────────────────────────────────
    estados = {row["estado"]: int(row["total"]) for _, row in df_estados.iterrows()}
    stats = {
        "activas":         estados.get("ACTIVO", 0),
        "canceladas":      estados.get("CANCELADO", 0),
        "terminadas":      estados.get("TERMINADO", 0),
        "pagos_vencidos":  int(df_vencidos.iloc[0]["n"]) if not df_vencidos.empty else 0,
        "cobrado_hoy":     float(df_cob_hoy.iloc[0]["t"]) if not df_cob_hoy.empty else 0,
        "saldo_pendiente": float(df_saldo.iloc[0]["pendiente"]) if not df_saldo.empty else 0,
        "movimientos_hoy": int(df_movhoy.iloc[0]["n"]) if not df_movhoy.empty else 0,
        "deuda_post_viaje":int(df_deuda.iloc[0]["n"]) if not df_deuda.empty else 0,
        "cobrado_mes_mxn": ing_mxn,
    }

    return templates.TemplateResponse(request, "dashboard.html", ctx(request, {
        "active":           "dashboard",
        "stats":            stats,
        "proximas":         df_proximas.to_dict("records") if not df_proximas.empty else [],
        "checkins_pendientes": df_checkins.to_dict("records") if not df_checkins.empty else [],
        "chart_tendencia":  chart_tendencia,
        "chart_destinos":   chart_destinos,
        "caja": {
            "mes":     mes_nombre,
            "ing_mxn": ing_mxn, "egr_mxn": egr_mxn, "bal_mxn": ing_mxn - egr_mxn,
            "ing_usd": ing_usd, "egr_usd": egr_usd, "bal_usd": ing_usd - egr_usd,
        },
        "mes_sel":      _mes_sel,
        "anio_sel":     str(_anio_sel),
        "anios_disp":   anios_disp,
        "es_mes_actual": es_mes_actual,
    }))


# ─── Bitácora ────────────────────────────────────────────────────────────────

_BITACORA_PER_PAGE = 50


# Deuda a proveedores por reserva — misma lógica que el Control de Caja del detalle de
# itinerario (detalle_reserva/_build_semaforo): para paquetes globales cuenta todos los
# egresos menos comisiones; para el resto, solo las 6 categorías de servicio.
# Reservas de grupo (r.id_grupo NOT NULL) siempre dan 0 aquí — su pago a proveedor se
# controla en /grupos/{id}, no por id_reserva (evita que queden "IRREGULARES" para
# siempre, ya que ese pago nunca se registra contra su propio id_reserva).
_DEUDA_PROV_EXPR = """
    (CASE WHEN r.id_grupo IS NOT NULL THEN 0.0
    WHEN r.es_paquete_global = 1 THEN
        MAX(0.0, r.costo_total
            - COALESCE((SELECT SUM(f.monto) FROM flujo_caja f WHERE f.id_reserva=r.id_reserva AND f.categoria='Comisiones Bancarias' AND f.tipo_movimiento='EGRESO' AND f.estado='ACTIVO'),0)
            - COALESCE((SELECT SUM(f.monto) FROM flujo_caja f WHERE f.id_reserva=r.id_reserva AND f.categoria!='Comisiones Bancarias' AND f.tipo_movimiento='EGRESO' AND f.estado='ACTIVO'),0))
    ELSE
        MAX(0.0, (r.costo_total
            - COALESCE((SELECT SUM(f.monto) FROM flujo_caja f WHERE f.id_reserva=r.id_reserva AND f.categoria='Comisiones Bancarias' AND f.tipo_movimiento='EGRESO' AND f.estado='ACTIVO'),0))
            - COALESCE((SELECT SUM(f.monto) FROM flujo_caja f WHERE f.id_reserva=r.id_reserva AND f.categoria IN ('Pago de Vuelo (Proveedor)','Pago de TUA (Impuesto)','Pago de Hotel (Proveedor)','Pago de Traslado (Proveedor)','Pago de Tours (Proveedor)','Pago de Adicionales (Proveedor)') AND f.tipo_movimiento='EGRESO' AND f.estado='ACTIVO'),0))
    END)
"""
_CHECKINS_OK_EXPR = "(r.checkin_ida=1 AND r.checkin_regreso=1)"


def _reservas_where(q: str = "", estado: str = "ACTIVO", creacion: str = "todos"):
    import datetime as _dt_q
    hoy_str = str(now_local().date())
    where_parts, params = ["1=1"], []
    GRANULAR = {
        "PROXIMOS_PENDIENTES": ("AND r.estado='ACTIVO' AND r.fecha_regreso >= ? AND (r.venta_total - r.cobrado_cliente) > 1", [hoy_str]),
        "PROXIMOS_LIQUIDADOS": ("AND r.estado='ACTIVO' AND r.fecha_regreso >= ? AND (r.venta_total - r.cobrado_cliente) <= 1", [hoy_str]),
        # Cerrado = ya regresó Y cliente liquidado Y proveedores pagados Y checkins hechos.
        "HISTORICO_SANO":      (f"AND r.estado IN ('ACTIVO','TERMINADO') AND r.fecha_regreso < ? "
                                 f"AND (r.venta_total - r.cobrado_cliente) <= 1 "
                                 f"AND {_DEUDA_PROV_EXPR} <= 1 AND {_CHECKINS_OK_EXPR}", [hoy_str]),
        # Irregular = ya regresó Y falta algo: saldo cliente, deuda a proveedor, o checkin.
        "IRREGULARES":         (f"AND r.estado IN ('ACTIVO','TERMINADO') AND r.fecha_regreso < ? "
                                 f"AND ((r.venta_total - r.cobrado_cliente) > 1 "
                                 f"OR {_DEUDA_PROV_EXPR} > 1 OR NOT {_CHECKINS_OK_EXPR})", [hoy_str]),
    }
    if estado in GRANULAR:
        w, p = GRANULAR[estado]
        where_parts.append(w); params.extend(p)
    elif estado and estado != "TODOS":
        where_parts.append("AND r.estado = ?"); params.append(estado)
    if q:
        bq = f"%{q}%"
        where_parts.append("""AND (
            c.nombre LIKE ? OR r.destino LIKE ? OR r.origen LIKE ?
            OR CAST(r.id_reserva AS TEXT) LIKE ?
            OR COALESCE(r.nombre_hotel,'') LIKE ?
            OR COALESCE(r.mayorista,'') LIKE ?
            OR COALESCE(r.localizador_global,'') LIKE ?
            OR COALESCE(r.itinerario_vuelo_plataforma,'') LIKE ?
            OR COALESCE(r.aerolinea,'') LIKE ?
            OR COALESCE(r.proveedor_traslados,'') LIKE ?
            OR COALESCE(r.proveedor_tours,'') LIKE ?
            OR COALESCE(r.comentarios_operativos,'') LIKE ?
            OR COALESCE(r.notas_abiertas,'') LIKE ?
        )""")
        params.extend([bq] * 13)
    _dias_map = {"7": 7, "15": 15, "30": 30, "90": 90}
    if creacion in _dias_map:
        desde = str((now_local() - _dt_q.timedelta(days=_dias_map[creacion])).date())
        where_parts.append("AND DATE(r.fecha_creacion) >= ?"); params.append(desde)
    return " ".join(where_parts), params


def _count_reservas(q: str = "", estado: str = "ACTIVO", creacion: str = "todos") -> int:
    where_sql, params = _reservas_where(q, estado, creacion)
    df = obtener_datos(
        f"SELECT COUNT(*) as n FROM reservas r JOIN clientes c ON r.id_cliente=c.id_cliente WHERE {where_sql}",
        tuple(params),
    )
    return int(df.iloc[0]["n"]) if not df.empty else 0


def _query_reservas(q: str = "", estado: str = "ACTIVO", orden: str = "auto",
                    creacion: str = "todos", limit: int | None = _BITACORA_PER_PAGE, offset: int = 0):
    where_sql, params = _reservas_where(q, estado, creacion)
    if orden == "proxima":
        order_sql = "ORDER BY r.fecha_salida ASC"
    elif orden == "lejana":
        order_sql = "ORDER BY r.fecha_salida DESC"
    elif orden == "reciente":
        order_sql = "ORDER BY r.id_reserva DESC"
    elif orden == "itin_asc":
        order_sql = "ORDER BY r.id_reserva ASC"
    elif orden == "az":
        order_sql = "ORDER BY LOWER(c.nombre) ASC"
    else:  # auto
        if estado in ("PROXIMOS_PENDIENTES", "PROXIMOS_LIQUIDADOS"):
            order_sql = "ORDER BY r.fecha_salida ASC"
        else:
            order_sql = "ORDER BY r.fecha_salida DESC"
    limit_sql = f"LIMIT {int(limit)} OFFSET {int(offset)}" if limit is not None else ""
    return obtener_datos(
        f"""SELECT r.id_reserva, c.nombre, r.destino, r.origen,
                   r.fecha_salida, r.fecha_regreso,
                   r.venta_total, r.cobrado_cliente,
                   (r.venta_total - r.cobrado_cliente) as saldo,
                   r.estado, r.moneda, r.fecha_limite_liquidacion,
                   r.id_grupo, g.nombre_grupo
            FROM reservas r
            JOIN clientes c ON r.id_cliente = c.id_cliente
            LEFT JOIN grupos_viaje g ON r.id_grupo = g.id_grupo
            WHERE {where_sql}
            {order_sql} {limit_sql}""",
        tuple(params),
    )


@app.get("/bitacora", response_class=HTMLResponse)
async def bitacora(request: Request, q: str = "", estado: str = "PROXIMOS_PENDIENTES",
                   orden: str = "auto", creacion: str = "todos", page: int = 1):
    if not usuario_activo(request):
        return RedirectResponse(url="/login")
    page = max(1, page)
    offset = (page - 1) * _BITACORA_PER_PAGE
    total = _count_reservas(q, estado, creacion)
    reservas = _query_reservas(q, estado, orden, creacion, offset=offset).to_dict("records")
    return templates.TemplateResponse(request, "bitacora.html", ctx(request, {
        "active": "bitacora",
        "reservas": reservas,
        "q": q,
        "estado_sel": estado,
        "orden_sel": orden,
        "creacion_sel": creacion,
        "page": page,
        "total": total,
        "hay_mas": offset + len(reservas) < total,
    }))


@app.get("/bitacora/rows", response_class=HTMLResponse)
async def bitacora_rows(request: Request, q: str = "", estado: str = "PROXIMOS_PENDIENTES",
                        orden: str = "auto", creacion: str = "todos", page: int = 1):
    if not usuario_activo(request):
        return HTMLResponse("", status_code=401)
    page = max(1, page)
    offset = (page - 1) * _BITACORA_PER_PAGE
    total = _count_reservas(q, estado, creacion)
    reservas = _query_reservas(q, estado, orden, creacion, offset=offset).to_dict("records")
    hay_mas = offset + len(reservas) < total
    rows_html = _jinja_env.get_template("bitacora_rows.html").render(
        reservas=reservas, page=page, hay_mas=hay_mas, total=total,
    )
    oob_count = f'<span id="conteo" hx-swap-oob="true">{total} reserva(s)</span>'
    return HTMLResponse(rows_html + "\n" + oob_count)


@app.get("/bitacora/exportar")
async def bitacora_exportar(request: Request, q: str = "", estado: str = "TODOS",
                             orden: str = "auto", creacion: str = "todos"):
    if not usuario_activo(request):
        return RedirectResponse(url="/login")
    hoy = now_local().date()
    df = _query_reservas(q, estado, orden, creacion, limit=None)

    df_costos = obtener_datos(
        "SELECT id_reserva, costo_total, costo_vuelos, costo_tua, costo_hotel, "
        "costo_traslados, costo_tours, costo_adicionales, fecha_limite_proveedor, fecha_limite_liquidacion, "
        "checkin_ida, checkin_regreso, es_paquete_global FROM reservas"
    )
    costos_map = {int(r["id_reserva"]): r.to_dict() for _, r in df_costos.iterrows()} if not df_costos.empty else {}

    df_extras_agg = obtener_datos(
        "SELECT id_reserva, SUM(monto_cobrado_cliente) as cob, SUM(monto_costo_proveedor) as cos "
        "FROM extras_viaje GROUP BY id_reserva"
    )
    extras_costo = {int(r["id_reserva"]): float(r["cos"] or 0.0) for _, r in df_extras_agg.iterrows()} if not df_extras_agg.empty else {}

    df_eg = obtener_datos(
        "SELECT id_reserva, categoria, SUM(monto) as total FROM flujo_caja "
        "WHERE tipo_movimiento='EGRESO' AND estado='ACTIVO' AND id_reserva IS NOT NULL "
        "GROUP BY id_reserva, categoria"
    )
    egresos_por_reserva = {}
    if not df_eg.empty:
        for _, r in df_eg.iterrows():
            v = r["id_reserva"]
            if v is None or v != v:
                continue
            egresos_por_reserva.setdefault(int(v), {})[r["categoria"]] = float(r["total"] or 0.0)

    def _costo(idr, col):
        row = costos_map.get(int(idr))
        return float(row[col] or 0.0) if row else 0.0

    def _pag(idr, categoria):
        return egresos_por_reserva.get(int(idr), {}).get(categoria, 0.0)

    def _pagado_prov_row(idr):
        d = egresos_por_reserva.get(int(idr), {})
        row = costos_map.get(int(idr))
        if row and int(row.get("es_paquete_global", 0) or 0) == 1:
            # Paquete Global: igual que el semáforo/Control de Caja del detalle — todo
            # egreso de la reserva cuenta como "pagado a proveedores" salvo comisiones.
            return sum(v for k, v in d.items() if k != "Comisiones Bancarias")
        return (d.get("Pago de Paquete (Mayorista)", 0.0) + d.get("Pago de Vuelo (Proveedor)", 0.0)
                + d.get("Pago de TUA (Impuesto)", 0.0) + d.get("Pago de Hotel (Proveedor)", 0.0)
                + d.get("Pago de Traslado (Proveedor)", 0.0) + d.get("Pago de Tours (Proveedor)", 0.0)
                + d.get("Pago de Adicionales (Proveedor)", 0.0))

    df_exp = df[["id_reserva", "nombre", "destino", "origen", "fecha_salida",
                 "fecha_regreso", "moneda", "venta_total", "cobrado_cliente", "saldo", "estado"]].copy()

    df_exp["pagado_proveedores"] = df_exp["id_reserva"].apply(_pagado_prov_row)
    _comisiones = df_exp["id_reserva"].apply(lambda x: _pag(x, "Comisiones Bancarias"))
    _venta_total_full = df_exp["venta_total"]
    _costo_total_full = df_exp["id_reserva"].apply(lambda x: _costo(x, "costo_total") + extras_costo.get(int(x), 0.0))
    df_exp["deuda_proveedores"] = (_costo_total_full - _comisiones - df_exp["pagado_proveedores"]).clip(lower=0.0)
    # Reservas de grupo: su pago a proveedor se controla en /grupos/{id}, no aquí.
    df_exp.loc[df["id_grupo"].notna().values, "deuda_proveedores"] = 0.0

    # Desglose por servicio — mismas categorías que se restan en el total, para
    # que la suma del desglose siempre cuadre con "Deuda a Proveedores".
    _ca_full = df_exp["id_reserva"].apply(lambda x: _costo(x, "costo_adicionales") + extras_costo.get(int(x), 0.0))
    df_exp["deuda_vuelo"] = (df_exp["id_reserva"].apply(lambda x: _costo(x, "costo_vuelos"))
                             - df_exp["id_reserva"].apply(lambda x: _pag(x, "Pago de Vuelo (Proveedor)"))).clip(lower=0.0)
    df_exp["deuda_tua"] = (df_exp["id_reserva"].apply(lambda x: _costo(x, "costo_tua"))
                           - df_exp["id_reserva"].apply(lambda x: _pag(x, "Pago de TUA (Impuesto)"))).clip(lower=0.0)
    df_exp["deuda_hotel"] = (df_exp["id_reserva"].apply(lambda x: _costo(x, "costo_hotel"))
                             - df_exp["id_reserva"].apply(lambda x: _pag(x, "Pago de Hotel (Proveedor)"))).clip(lower=0.0)
    df_exp["deuda_traslados"] = (df_exp["id_reserva"].apply(lambda x: _costo(x, "costo_traslados"))
                                 - df_exp["id_reserva"].apply(lambda x: _pag(x, "Pago de Traslado (Proveedor)"))).clip(lower=0.0)
    df_exp["deuda_tours"] = (df_exp["id_reserva"].apply(lambda x: _costo(x, "costo_tours"))
                             - df_exp["id_reserva"].apply(lambda x: _pag(x, "Pago de Tours (Proveedor)"))).clip(lower=0.0)
    df_exp["deuda_adicionales"] = (_ca_full - df_exp["id_reserva"].apply(lambda x: _pag(x, "Pago de Adicionales (Proveedor)"))).clip(lower=0.0)

    # Igual que en la tarjeta de Control de Caja del itinerario: se resta también la
    # comisión bancaria, y lo "retenido" se limita a lo que todavía hace falta para cubrir
    # la deuda a proveedores — el excedente ya es utilidad cobrada, no dinero pendiente.
    _retenido_bruto = df_exp["cobrado_cliente"] - df_exp["pagado_proveedores"] - _comisiones
    df_exp["utilidad_cobrada"] = (_retenido_bruto - df_exp["deuda_proveedores"]).clip(lower=0.0)
    df_exp["retenido_sin_aplicar"] = _retenido_bruto - df_exp["utilidad_cobrada"]
    df_exp["comisiones_pagadas"] = _comisiones
    df_exp["utilidad_total"] = _venta_total_full - _costo_total_full
    df_exp["margen_pct"] = ((df_exp["utilidad_total"] / _venta_total_full.replace(0, float('nan'))) * 100).round(1)
    # Fecha límite proveedor solo si aún hay deuda pendiente — si ya se pagó todo, no aplica
    df_exp["fecha_limite_proveedor"] = df_exp["id_reserva"].apply(lambda x: costos_map.get(int(x), {}).get("fecha_limite_proveedor"))
    df_exp["fecha_limite_proveedor"] = df_exp["fecha_limite_proveedor"].where(df_exp["deuda_proveedores"] > 0.01, other=None)
    df_exp["fecha_limite_liquidacion"] = df_exp["id_reserva"].apply(lambda x: costos_map.get(int(x), {}).get("fecha_limite_liquidacion"))

    # Estado operativo — mismo criterio que el filtro "Estatus" de arriba: Cerrado/Irregular
    # solo aplica a reservas cuyo viaje ya regresó; antes de eso siguen siendo "ACTIVO".
    _hoy_str = str(hoy)
    def _checkins_ok_row(idr):
        row = costos_map.get(int(idr)) or {}
        return int(row.get("checkin_ida") or 0) == 1 and int(row.get("checkin_regreso") or 0) == 1
    def _clasificar_estado(row):
        if row["estado"] == "CANCELADO": return "CANCELADO"
        if row["estado"] == "INCOBRABLE": return "INCOBRABLE"
        if str(row["fecha_regreso"]) >= _hoy_str: return "ACTIVO"
        if row["saldo"] <= 1.0 and row["deuda_proveedores"] <= 1.0 and _checkins_ok_row(row["id_reserva"]): return "CERRADO"
        return "IRREGULAR"
    df_exp["estado"] = df_exp.apply(_clasificar_estado, axis=1)

    # Redondeo final: las restas/sumas encadenadas de floats arrastran residuos
    # binarios (ej. 1713.5999999999999) — se redondean a centavos antes de exportar.
    for _col_money in ["venta_total", "cobrado_cliente", "pagado_proveedores", "retenido_sin_aplicar",
                        "deuda_proveedores", "deuda_vuelo", "deuda_tua", "deuda_hotel", "deuda_traslados",
                        "deuda_tours", "deuda_adicionales", "comisiones_pagadas", "utilidad_total",
                        "utilidad_cobrada", "saldo"]:
        df_exp[_col_money] = df_exp[_col_money].round(2)

    df_exp = df_exp[["id_reserva", "nombre", "destino", "origen", "fecha_salida", "fecha_regreso", "moneda",
                      "venta_total", "cobrado_cliente", "pagado_proveedores", "retenido_sin_aplicar",
                      "deuda_proveedores", "deuda_vuelo", "deuda_tua", "deuda_hotel", "deuda_traslados",
                      "deuda_tours", "deuda_adicionales", "fecha_limite_liquidacion", "fecha_limite_proveedor", "comisiones_pagadas",
                      "utilidad_total", "utilidad_cobrada", "margen_pct", "saldo", "estado"]]
    df_exp.columns = ["Itin #", "Cliente", "Destino", "Origen", "Salida", "Regreso", "Moneda",
                      "Venta Total", "Cobrado", "Pagado a Proveedores", "Retenido sin Aplicar",
                      "Deuda a Proveedores", "Deuda Vuelo", "Deuda TUA", "Deuda Hotel", "Deuda Traslados",
                      "Deuda Tours", "Deuda Adicionales", "Fecha Límite Cliente", "Fecha Límite Proveedor", "Comisiones Pagadas",
                      "Utilidad Total", "Utilidad Cobrada", "Margen %", "Saldo Pendiente", "Estado"]
    return _excel_response(df_exp, f"Bitacora_{hoy}.xlsx")


@app.get("/bitacora/nueva", response_class=HTMLResponse)
async def nueva_reserva_form(request: Request, id_cliente: str = "", id_grupo: str = ""):
    if not usuario_activo(request):
        return RedirectResponse(url="/login")
    clientes = obtener_datos("SELECT id_cliente, nombre FROM clientes ORDER BY nombre").to_dict("records")
    acompanantes_iniciales = []
    if id_cliente:
        df_ac = obtener_datos(
            "SELECT id_acompanante, nombre, fecha_nacimiento, parentesco FROM acompanantes_cliente WHERE id_cliente=? ORDER BY nombre",
            (id_cliente,)
        )
        acompanantes_iniciales = df_ac.to_dict("records") if not df_ac.empty else []
    grupos_activos = obtener_datos("SELECT id_grupo, nombre_grupo, destino, fecha_salida FROM grupos_viaje WHERE estado='ACTIVO' ORDER BY fecha_salida DESC").to_dict("records")
    return templates.TemplateResponse(request, "reserva_form.html", ctx(request, {
        "active": "bitacora",
        "reserva": None,
        "clientes": clientes,
        "titulo": "Nuevo Itinerario",
        "accion": "/bitacora/nueva",
        "preselect_cliente": id_cliente,
        "preselect_grupo": id_grupo,
        "grupos_activos": grupos_activos,
        "acompanantes_iniciales": acompanantes_iniciales,
        "habitaciones": [],
        **ctx_catalogos_reserva(),
    }))


@app.get("/api/acompanantes-viaje", response_class=HTMLResponse)
async def acompanantes_viaje(request: Request, id_cliente: str = ""):
    if not usuario_activo(request):
        return HTMLResponse("")
    _clear_preview = '<div id="preview-cliente" hx-swap-oob="true"></div>'
    if not id_cliente:
        return HTMLResponse(_clear_preview)
    df = obtener_datos(
        "SELECT id_acompanante, nombre, fecha_nacimiento, parentesco FROM acompanantes_cliente WHERE id_cliente=? ORDER BY nombre",
        (id_cliente,)
    )
    acompanantes = df.to_dict("records") if not df.empty else []
    html_opts = _jinja_env.get_template("acompanantes_viaje_options.html").render(acompanantes=acompanantes)

    df_cli = obtener_datos("SELECT nombre FROM clientes WHERE id_cliente=?", (id_cliente,))
    df_hist = obtener_datos("""
        SELECT COUNT(*) as total_viajes,
               SUM(venta_total) as venta_total,
               SUM(cobrado_cliente) as cobrado,
               SUM(venta_total - cobrado_cliente) as deuda
        FROM reservas WHERE id_cliente=?
    """, (id_cliente,))
    df_last = obtener_datos(
        "SELECT destino FROM reservas WHERE id_cliente=? ORDER BY fecha_salida DESC LIMIT 1",
        (id_cliente,)
    )
    preview_html = _clear_preview
    if not df_cli.empty:
        nom = _esc(str(df_cli.iloc[0]["nombre"]))
        h = df_hist.iloc[0] if not df_hist.empty else {}
        total_v = int(h.get("total_viajes", 0) or 0)
        venta_t = float(h.get("venta_total", 0) or 0)
        deuda   = float(h.get("deuda", 0) or 0)
        ult_dest = _esc(str(df_last.iloc[0]["destino"])) if not df_last.empty else "—"
        color_deuda = "var(--danger)" if deuda > 0.5 else "var(--primary)"
        preview_html = f"""<div id="preview-cliente" hx-swap-oob="true">
          <div class="card" style="margin-top:8px; border-left:4px solid var(--primary); padding:10px 16px;">
            <div style="font-weight:700; font-size:0.85rem; color:var(--primary); margin-bottom:6px;">👤 {nom}</div>
            <div style="display:grid; grid-template-columns:auto 1fr; gap:4px 14px; font-size:0.8rem;">
              <span style="color:var(--text2);">Viajes</span><strong>{total_v}</strong>
              <span style="color:var(--text2);">Último destino</span><strong>{ult_dest}</strong>
              <span style="color:var(--text2);">Venta histórica</span><strong>${venta_t:,.0f}</strong>
              <span style="color:var(--text2);">Deuda acum.</span><strong style="color:{color_deuda};">${deuda:,.0f}</strong>
            </div>
          </div>
        </div>"""
    return HTMLResponse(html_opts + preview_html)


@app.post("/bitacora/nueva")
async def crear_reserva(request: Request):
    if not usuario_activo(request):
        return RedirectResponse(url="/login")
    form   = await request.form()
    usuario = usuario_activo(request)

    def _f(k, d=0.0):
        try: return float(form.get(k) or d)
        except: return d

    id_cliente   = (form.get("id_cliente") or "").strip()
    destino      = upsert_catalogo_destino((form.get("destino") or "").strip())
    if not id_cliente or not destino:
        clientes = obtener_datos("SELECT id_cliente, nombre FROM clientes ORDER BY nombre").to_dict("records")
        return templates.TemplateResponse(request, "reserva_form.html", ctx(request, {
            "active": "bitacora", "reserva": None, "clientes": clientes,
            "titulo": "Nuevo Itinerario", "accion": "/bitacora/nueva",
            "error": "Cliente y destino son obligatorios.",
            "habitaciones": [],
            **ctx_catalogos_reserva(),
        }))

    es_paquete = 1 if form.get("es_paquete_global") else 0

    cobro_v = _f("cobro_vuelos"); cobro_t = _f("cobro_tua"); cobro_h = _f("cobro_hotel")
    cobro_tr = _f("cobro_traslados"); cobro_to = _f("cobro_tours"); cobro_a = _f("cobro_adicionales")
    venta_total = cobro_v + cobro_t + cobro_h + cobro_tr + cobro_to + cobro_a

    costo_v = _f("costo_vuelos"); costo_t = _f("costo_tua"); costo_h = _f("costo_hotel")
    costo_tr = _f("costo_traslados"); costo_to = _f("costo_tours"); costo_a = _f("costo_adicionales")
    costo_com = _f("costo_comisiones")
    costo_total = costo_v + costo_t + costo_h + costo_tr + costo_to + costo_a + costo_com
    utilidad = venta_total - costo_total

    v_mayorista = upsert_catalogo_mayorista(form.get("mayorista") or "")
    v_aerolinea = upsert_catalogo_aerolinea(form.get("aerolinea") or "")
    v_nombre_hotel = upsert_catalogo_hotel(form.get("nombre_hotel") or "")
    v_prov_traslados = upsert_catalogo_proveedor_traslados(form.get("proveedor_traslados") or "")
    v_prov_tours = upsert_catalogo_proveedor_tours(form.get("proveedor_tours") or "")
    v_prov_adicionales = upsert_catalogo_proveedor_adicionales(form.get("proveedor_adicionales") or "")
    v_equipaje = upsert_catalogo_equipaje(form.get("detalle_equipaje") or "")

    ok = ejecutar_comando("""
        INSERT INTO reservas (
            id_cliente, destino, origen, fecha_salida, fecha_regreso,
            fecha_limite_liquidacion, fecha_limite_proveedor, moneda,
            cobro_vuelos, cobro_tua, cobro_hotel, cobro_traslados, cobro_tours,
            cobro_adicionales, especificar_adicionales, venta_total,
            costo_vuelos, costo_tua, costo_hotel, costo_traslados, costo_tours,
            costo_adicionales, costo_comisiones, costo_total, utilidad_proyectada,
            es_paquete_global,
            mayorista, aerolinea, nombre_hotel, localizador_global,
            itinerario_hotel_plataforma, itinerario_vuelo_plataforma,
            proveedor_traslados, confirmacion_proveedor_traslados,
            proveedor_tours, confirmacion_proveedor_tours,
            proveedor_adicionales, confirmacion_proveedor_adicionales,
            fecha_vuelo_ida, hora_vuelo_ida, fecha_vuelo_vuelta, hora_vuelo_vuelta, detalle_equipaje,
            restricciones_medicas, solicitudes_especiales,
            comentarios_operativos, notas_abiertas,
            hotel_confirmado, hotel_liquidado,
            usuario_creador, fecha_creacion, estado, id_grupo, num_pax
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
    """, (
        id_cliente, destino,
        form.get("origen") or "Monterrey",
        form.get("fecha_salida"), form.get("fecha_regreso"),
        form.get("fecha_limite_liquidacion") or None,
        form.get("fecha_limite_proveedor") or None,
        form.get("moneda") or "MXN",
        cobro_v, cobro_t, cobro_h, cobro_tr, cobro_to, cobro_a,
        form.get("especificar_adicionales") or None, venta_total,
        costo_v, costo_t, costo_h, costo_tr, costo_to, costo_a, costo_com,
        costo_total, utilidad, es_paquete,
        v_mayorista or None, v_aerolinea or None,
        v_nombre_hotel or None, form.get("localizador_global") or None,
        form.get("itinerario_hotel_plataforma") or None,
        form.get("itinerario_vuelo_plataforma") or None,
        v_prov_traslados or None,
        form.get("confirmacion_proveedor_traslados") or None,
        v_prov_tours or None,
        form.get("confirmacion_proveedor_tours") or None,
        v_prov_adicionales or None,
        form.get("confirmacion_proveedor_adicionales") or None,
        form.get("fecha_vuelo_ida") or form.get("fecha_salida"),
        form.get("hora_vuelo_ida") or None,
        form.get("fecha_vuelo_vuelta") or form.get("fecha_regreso"),
        form.get("hora_vuelo_vuelta") or None,
        v_equipaje or None,
        form.get("restricciones_medicas") or None,
        form.get("solicitudes_especiales") or None,
        form.get("comentarios_operativos") or None,
        form.get("notas_abiertas") or None,
        1 if form.get("hotel_confirmado") else 0,
        1 if form.get("hotel_liquidado") else 0,
        usuario, str(now_local().date()), "ACTIVO",
        int(form.get("id_grupo")) if (form.get("id_grupo") or "").strip() else None,
        int(form.get("num_pax") or 1),
    ))

    if ok:
        df = obtener_datos(
            "SELECT id_reserva FROM reservas WHERE id_cliente=? AND destino=? ORDER BY id_reserva DESC LIMIT 1",
            (id_cliente, destino)
        )
        id_new = int(df.iloc[0]["id_reserva"])
        # Insertar al cliente como pasajero titular automáticamente
        df_cli = obtener_datos("SELECT nombre, fecha_nacimiento FROM clientes WHERE id_cliente=?", (id_cliente,))
        if not df_cli.empty:
            ejecutar_comando(
                "INSERT INTO pasajeros_reserva (id_reserva, nombre, fecha_nacimiento, parentesco) VALUES (?,?,?,?)",
                (id_new, df_cli.iloc[0]["nombre"], df_cli.iloc[0]["fecha_nacimiento"] or "", "Titular")
            )
        # Insertar habitaciones capturadas en el formulario
        try:
            n_hab = int(form.get("hab_n") or 0)
        except Exception:
            n_hab = 0
        for i in range(1, n_hab + 1):
            h_tipo = (form.get(f"hab_tipo_{i}") or "").strip()
            if not h_tipo:
                continue
            try: h_pers = int(form.get(f"hab_personas_{i}") or 1)
            except Exception: h_pers = 1
            ejecutar_comando(
                "INSERT INTO habitaciones_reserva (id_reserva, tipo_habitacion, num_personas, hora_checkin, descripcion) VALUES (?,?,?,?,?)",
                (id_new, h_tipo, h_pers, (form.get(f"hab_checkin_{i}") or "15:00").strip() or "15:00", (form.get(f"hab_descripcion_{i}") or "").strip() or None)
            )

        # Tipo de vuelo + vuelos por tramo + hoteles múltiples (opcionales, puramente
        # logísticos — el dinero sigue siendo un solo total en cobro/costo de arriba)
        _tv_new = form.get("tipo_vuelo") or "REDONDO"
        if _tv_new not in ("SENCILLO", "REDONDO"):
            _tv_new = "REDONDO"
        ejecutar_comando("UPDATE reservas SET tipo_vuelo=? WHERE id_reserva=?", (_tv_new, id_new))
        try:
            n_vue = int(form.get("vue_n") or 0)
        except Exception:
            n_vue = 0
        for i in range(1, n_vue + 1):
            v_aero_i = (form.get(f"vue_aerolinea_{i}") or "").strip()
            if not v_aero_i:
                continue
            ejecutar_comando(
                "INSERT INTO vuelos_reserva (id_reserva, numero_tramo, aerolinea, numero_vuelo, origen, destino, fecha, hora, localizador) VALUES (?,?,?,?,?,?,?,?,?)",
                (id_new, i, v_aero_i, (form.get(f"vue_numero_{i}") or "").strip(),
                 (form.get(f"vue_origen_{i}") or "").strip(), (form.get(f"vue_destino_{i}") or "").strip(),
                 form.get(f"vue_fecha_{i}") or None, form.get(f"vue_hora_{i}") or None,
                 (form.get(f"vue_localizador_{i}") or "").strip())
            )
        try:
            n_hmu = int(form.get("hmu_n") or 0)
        except Exception:
            n_hmu = 0
        for i in range(1, n_hmu + 1):
            h_nombre_i = (form.get(f"hmu_nombre_{i}") or "").strip()
            if not h_nombre_i:
                continue
            _id_hotel_i = ejecutar_insert(
                "INSERT INTO hoteles_reserva (id_reserva, numero_orden, ciudad_destino, nombre_hotel, localizador, fecha_checkin, fecha_checkout) VALUES (?,?,?,?,?,?,?)",
                (id_new, i, (form.get(f"hmu_ciudad_{i}") or "").strip(), h_nombre_i,
                 (form.get(f"hmu_localizador_{i}") or "").strip(),
                 form.get(f"hmu_checkin_{i}") or None, form.get(f"hmu_checkout_{i}") or None)
            )
            try:
                _n_hmu_hab = int(form.get(f"hmu_hab_n_{i}") or 0)
            except Exception:
                _n_hmu_hab = 0
            for j in range(1, _n_hmu_hab + 1):
                _hh_tipo = (form.get(f"hmu_hab_tipo_{i}_{j}") or "").strip()
                if not _hh_tipo:
                    continue
                try: _hh_pers = int(form.get(f"hmu_hab_personas_{i}_{j}") or 1)
                except Exception: _hh_pers = 1
                ejecutar_comando(
                    "INSERT INTO habitaciones_reserva (id_reserva, id_hotel_itin, tipo_habitacion, num_personas, hora_checkin, descripcion) VALUES (?,?,?,?,?,?)",
                    (id_new, _id_hotel_i, _hh_tipo, _hh_pers,
                     (form.get(f"hmu_hab_checkin_{i}_{j}") or "15:00").strip() or "15:00",
                     (form.get(f"hmu_hab_descripcion_{i}_{j}") or "").strip() or None)
                )

        # Insertar acompañantes seleccionados desde la DB del cliente
        ids_acompanante = form.getlist("acompanante_id")
        for id_ac in ids_acompanante:
            df_ac = obtener_datos(
                "SELECT nombre, fecha_nacimiento, parentesco FROM acompanantes_cliente WHERE id_acompanante=?",
                (int(id_ac),)
            )
            if not df_ac.empty:
                a = df_ac.iloc[0]
                ejecutar_comando(
                    "INSERT INTO pasajeros_reserva (id_reserva, nombre, fecha_nacimiento, parentesco) VALUES (?,?,?,?)",
                    (id_new, a["nombre"], a["fecha_nacimiento"] or "", a["parentesco"] or "")
                )
        # Insertar plan de pagos si se capturó en el formulario
        try:
            n_plan = int(form.get("plan_n_pagos") or 0)
        except Exception:
            n_plan = 0
        for i in range(1, n_plan + 1):
            p_fecha = (form.get(f"plan_fecha_{i}") or "").strip()
            try:
                p_monto = float(form.get(f"plan_monto_{i}") or 0)
            except Exception:
                p_monto = 0.0
            if p_fecha and p_monto > 0:
                ejecutar_comando(
                    "INSERT INTO plan_pagos (id_reserva, numero_pago, monto_esperado, fecha_programada) VALUES (?,?,?,?)",
                    (id_new, i, p_monto, p_fecha)
                )

        # Registrar anticipo inicial si se capturó en el formulario
        anticipo_monto = _f("anticipo_monto")
        if anticipo_monto > 0:
            moneda_res = form.get("moneda") or "MXN"
            metodo_ant = form.get("anticipo_metodo") or "Transferencia"
            fecha_ant  = form.get("anticipo_fecha") or str(now_local().date())
            pct_com_ant = _f("anticipo_comision_pct")
            concepto_ant = f"[{metodo_ant}] Anticipo inicial"
            ops_ant = [(
                "INSERT INTO flujo_caja (id_reserva, tipo_movimiento, tipo_egreso, categoria, concepto, monto, moneda, fecha_pago, usuario_creador, metodo_pago, fecha_creacion) VALUES (?, 'INGRESO', 'NO APLICA', 'Abono Parcial de Viaje', ?, ?, ?, ?, ?, ?, ?)",
                (id_new, concepto_ant, anticipo_monto, moneda_res, fecha_ant, usuario, metodo_ant, str(now_local().date()))
            )]
            if metodo_ant == "Tarjeta de Crédito/Débito" and pct_com_ant > 0:
                comision_ant = round(anticipo_monto * pct_com_ant / 100, 2)
                ops_ant.append((
                    "INSERT INTO flujo_caja (id_reserva, tipo_movimiento, tipo_egreso, categoria, concepto, monto, moneda, fecha_pago, fecha_creacion) VALUES (?, 'EGRESO', 'COSTO DIRECTO VIAJE', 'Comisiones Bancarias', ?, ?, ?, ?, ?)",
                    (id_new, "[Automático] Comisión Tarjeta", comision_ant, moneda_res, fecha_ant, str(now_local().date()))
                ))
            ejecutar_transaccion(ops_ant)
            from database import sincronizar_cobrado_cliente
            sincronizar_cobrado_cliente(id_new)

        registrar_cambio(id_new, "CREACIÓN", "Reserva creada", usuario=usuario)
        return RedirectResponse(url=f"/bitacora/{id_new}", status_code=303)

    clientes = obtener_datos("SELECT id_cliente, nombre FROM clientes ORDER BY nombre").to_dict("records")
    return templates.TemplateResponse(request, "reserva_form.html", ctx(request, {
        "active": "bitacora", "reserva": None, "clientes": clientes,
        "titulo": "Nuevo Itinerario", "accion": "/bitacora/nueva",
        "error": "Error al crear el itinerario. Intenta de nuevo.",
        "habitaciones": [],
        **ctx_catalogos_reserva(),
    }))


@app.get("/bitacora/{id_reserva}/editar", response_class=HTMLResponse)
async def editar_reserva_form(request: Request, id_reserva: int):
    if not usuario_activo(request):
        return RedirectResponse(url="/login")
    df = obtener_datos("SELECT * FROM reservas WHERE id_reserva = ?", (id_reserva,))
    if df.empty:
        return RedirectResponse(url="/bitacora")
    clientes = obtener_datos("SELECT id_cliente, nombre FROM clientes ORDER BY nombre").to_dict("records")
    df_hab = obtener_datos(
        "SELECT id_habitacion, tipo_habitacion, num_personas, hora_checkin, descripcion FROM habitaciones_reserva WHERE id_reserva=? ORDER BY id_habitacion",
        (id_reserva,)
    )
    return templates.TemplateResponse(request, "reserva_form.html", ctx(request, {
        "active": "bitacora",
        "reserva": df.iloc[0].to_dict(),
        "clientes": clientes,
        "titulo": f"Editando Reserva #{id_reserva}",
        "accion": f"/bitacora/{id_reserva}/editar",
        "habitaciones": df_hab.to_dict("records") if not df_hab.empty else [],
        **ctx_catalogos_reserva(),
    }))


@app.post("/bitacora/{id_reserva}/editar")
async def editar_reserva(request: Request, id_reserva: int):
    if not usuario_activo(request):
        return RedirectResponse(url="/login")
    form    = await request.form()
    usuario = usuario_activo(request)

    def _f(k, d=0.0):
        try: return float(form.get(k) or d)
        except: return d

    cobro_v = _f("cobro_vuelos"); cobro_t = _f("cobro_tua"); cobro_h = _f("cobro_hotel")
    cobro_tr = _f("cobro_traslados"); cobro_to = _f("cobro_tours"); cobro_a = _f("cobro_adicionales")
    venta_base_nueva = cobro_v + cobro_t + cobro_h + cobro_tr + cobro_to + cobro_a

    costo_v = _f("costo_vuelos"); costo_t = _f("costo_tua"); costo_h = _f("costo_hotel")
    costo_tr = _f("costo_traslados"); costo_to = _f("costo_tours"); costo_a = _f("costo_adicionales")
    costo_base_nueva = costo_v + costo_t + costo_h + costo_tr + costo_to + costo_a

    # Candado — igual espíritu que Streamlit (que separa esto en dos formularios y
    # deshabilita cualquier costo ya pagado al proveedor): no permitir cambiar un
    # componente de costo que ya tiene un pago a proveedor registrado, y preservar
    # (en vez de sobrescribir) cualquier monto que "Extras"/comisiones ya hayan
    # sumado a venta_total/costo_total — de lo contrario este formulario los borra
    # silenciosamente al recalcular desde cero.
    df_actual = obtener_datos(
        "SELECT venta_total, costo_total, costo_comisiones, "
        "cobro_vuelos, cobro_tua, cobro_hotel, cobro_traslados, cobro_tours, cobro_adicionales, "
        "costo_vuelos, costo_tua, costo_hotel, costo_traslados, costo_tours, costo_adicionales "
        "FROM reservas WHERE id_reserva=?", (id_reserva,)
    )
    fila_actual = df_actual.iloc[0].to_dict() if not df_actual.empty else {}
    venta_base_anterior = sum(float(fila_actual.get(k, 0) or 0) for k in
        ("cobro_vuelos", "cobro_tua", "cobro_hotel", "cobro_traslados", "cobro_tours", "cobro_adicionales"))
    costo_base_anterior = sum(float(fila_actual.get(k, 0) or 0) for k in
        ("costo_vuelos", "costo_tua", "costo_hotel", "costo_traslados", "costo_tours", "costo_adicionales"))

    df_pag_cat = obtener_datos(
        "SELECT categoria, COALESCE(SUM(monto),0) as total FROM flujo_caja "
        "WHERE id_reserva=? AND tipo_movimiento='EGRESO' AND estado='ACTIVO' GROUP BY categoria",
        (id_reserva,)
    )
    pagos_cat = {r["categoria"]: float(r["total"] or 0) for _, r in df_pag_cat.iterrows()} if not df_pag_cat.empty else {}
    _candados = [
        ("Pago de Vuelo (Proveedor)", "costo_vuelos", costo_v, "Vuelo"),
        ("Pago de TUA (Impuesto)", "costo_tua", costo_t, "TUA"),
        ("Pago de Hotel (Proveedor)", "costo_hotel", costo_h, "Hotel"),
        ("Pago de Traslado (Proveedor)", "costo_traslados", costo_tr, "Traslados"),
        ("Pago de Tours (Proveedor)", "costo_tours", costo_to, "Tours"),
        ("Pago de Adicionales (Proveedor)", "costo_adicionales", costo_a, "Adicionales"),
    ]
    for categoria, col, nuevo_val, etiqueta in _candados:
        if pagos_cat.get(categoria, 0.0) > 0.01:
            valor_anterior = float(fila_actual.get(col, 0) or 0)
            if abs(nuevo_val - valor_anterior) > 0.01:
                request.session["flash"] = {"tipo": "error", "texto": f"⚠️ El costo de {etiqueta} ya tiene un pago a proveedor registrado — no se puede modificar desde aquí. Los demás cambios no se guardaron."}
                return RedirectResponse(url=f"/bitacora/{id_reserva}/editar", status_code=303)

    venta_total = float(fila_actual.get("venta_total", 0) or 0) + (venta_base_nueva - venta_base_anterior)
    costo_com = float(fila_actual.get("costo_comisiones", 0) or 0)  # no editable desde este formulario
    costo_total = float(fila_actual.get("costo_total", 0) or 0) + (costo_base_nueva - costo_base_anterior)
    utilidad = venta_total - costo_total

    v_mayorista = upsert_catalogo_mayorista(form.get("mayorista") or "")
    v_aerolinea = upsert_catalogo_aerolinea(form.get("aerolinea") or "")
    v_nombre_hotel = upsert_catalogo_hotel(form.get("nombre_hotel") or "")
    v_prov_traslados = upsert_catalogo_proveedor_traslados(form.get("proveedor_traslados") or "")
    v_prov_tours = upsert_catalogo_proveedor_tours(form.get("proveedor_tours") or "")
    v_prov_adicionales = upsert_catalogo_proveedor_adicionales(form.get("proveedor_adicionales") or "")
    v_equipaje = upsert_catalogo_equipaje(form.get("detalle_equipaje") or "")

    ejecutar_comando("""
        UPDATE reservas SET
            id_cliente=?, destino=?, origen=?,
            fecha_salida=?, fecha_regreso=?,
            fecha_limite_liquidacion=?, fecha_limite_proveedor=?, moneda=?,
            cobro_vuelos=?, cobro_tua=?, cobro_hotel=?, cobro_traslados=?,
            cobro_tours=?, cobro_adicionales=?, especificar_adicionales=?,
            venta_total=?,
            costo_vuelos=?, costo_tua=?, costo_hotel=?, costo_traslados=?,
            costo_tours=?, costo_adicionales=?, costo_comisiones=?,
            costo_total=?, utilidad_proyectada=?,
            mayorista=?, aerolinea=?, nombre_hotel=?, localizador_global=?,
            itinerario_hotel_plataforma=?, itinerario_vuelo_plataforma=?,
            proveedor_traslados=?, confirmacion_proveedor_traslados=?,
            proveedor_tours=?, confirmacion_proveedor_tours=?,
            proveedor_adicionales=?, confirmacion_proveedor_adicionales=?,
            fecha_vuelo_ida=?, hora_vuelo_ida=?, fecha_vuelo_vuelta=?, hora_vuelo_vuelta=?, detalle_equipaje=?,
            restricciones_medicas=?, solicitudes_especiales=?,
            comentarios_operativos=?, notas_abiertas=?
        WHERE id_reserva=?
    """, (
        (form.get("id_cliente") or "").strip(),
        upsert_catalogo_destino((form.get("destino") or "").strip()),
        form.get("origen") or "Monterrey",
        form.get("fecha_salida"), form.get("fecha_regreso"),
        form.get("fecha_limite_liquidacion") or None,
        form.get("fecha_limite_proveedor") or None,
        form.get("moneda") or "MXN",
        cobro_v, cobro_t, cobro_h, cobro_tr, cobro_to, cobro_a,
        form.get("especificar_adicionales") or None, venta_total,
        costo_v, costo_t, costo_h, costo_tr, costo_to, costo_a, costo_com,
        costo_total, utilidad,
        v_mayorista or None, v_aerolinea or None,
        v_nombre_hotel or None, form.get("localizador_global") or None,
        form.get("itinerario_hotel_plataforma") or None,
        form.get("itinerario_vuelo_plataforma") or None,
        v_prov_traslados or None,
        form.get("confirmacion_proveedor_traslados") or None,
        v_prov_tours or None,
        form.get("confirmacion_proveedor_tours") or None,
        v_prov_adicionales or None,
        form.get("confirmacion_proveedor_adicionales") or None,
        form.get("fecha_vuelo_ida") or form.get("fecha_salida"),
        form.get("hora_vuelo_ida") or None,
        form.get("fecha_vuelo_vuelta") or form.get("fecha_regreso"),
        form.get("hora_vuelo_vuelta") or None,
        v_equipaje or None,
        form.get("restricciones_medicas") or None,
        form.get("solicitudes_especiales") or None,
        form.get("comentarios_operativos") or None,
        form.get("notas_abiertas") or None,
        id_reserva,
    ))

    # Reemplazar habitaciones con lo capturado en el formulario
    ejecutar_comando("DELETE FROM habitaciones_reserva WHERE id_reserva=?", (id_reserva,))
    try:
        n_hab = int(form.get("hab_n") or 0)
    except Exception:
        n_hab = 0
    for i in range(1, n_hab + 1):
        h_tipo = (form.get(f"hab_tipo_{i}") or "").strip()
        if not h_tipo:
            continue
        try: h_pers = int(form.get(f"hab_personas_{i}") or 1)
        except Exception: h_pers = 1
        ejecutar_comando(
            "INSERT INTO habitaciones_reserva (id_reserva, tipo_habitacion, num_personas, hora_checkin, descripcion) VALUES (?,?,?,?,?)",
            (id_reserva, h_tipo, h_pers, (form.get(f"hab_checkin_{i}") or "15:00").strip() or "15:00", (form.get(f"hab_descripcion_{i}") or "").strip() or None)
        )

    registrar_cambio(id_reserva, "EDICIÓN", "Datos generales actualizados", usuario=usuario)
    return RedirectResponse(url=f"/bitacora/{id_reserva}", status_code=303)


# ─── Pasajeros (HTMX) ────────────────────────────────────────────────────────

def _pasajeros_html(request, id_reserva):
    df = obtener_datos(
        "SELECT id_pasajero, nombre, fecha_nacimiento, parentesco FROM pasajeros_reserva WHERE id_reserva=? ORDER BY id_pasajero",
        (id_reserva,)
    )
    df_cli = obtener_datos("SELECT id_cliente FROM reservas WHERE id_reserva=?", (id_reserva,))
    acompanantes = []
    nombres_frecuentes = set()
    if not df_cli.empty:
        id_cliente = df_cli.iloc[0]["id_cliente"]
        df_ac = obtener_datos(
            "SELECT id_acompanante, nombre, fecha_nacimiento, parentesco FROM acompanantes_cliente WHERE id_cliente=? ORDER BY nombre",
            (id_cliente,)
        )
        nombres_frecuentes = {r["nombre"] for r in df_ac.to_dict("records")} if not df_ac.empty else set()
        nombres_en_reserva = {p["nombre"] for p in df.to_dict("records")}
        acompanantes = [r for r in df_ac.to_dict("records") if r["nombre"] not in nombres_en_reserva]
    return templates.TemplateResponse(request, "pasajeros_section.html", {
        "pasajeros": df.to_dict("records"),
        "id_reserva": id_reserva,
        "acompanantes": acompanantes,
        "nombres_frecuentes": nombres_frecuentes,
        "request": request,
    })


@app.post("/bitacora/{id_reserva}/pasajeros/agregar", response_class=HTMLResponse)
async def agregar_pasajero(request: Request, id_reserva: int):
    if not usuario_activo(request):
        return HTMLResponse("", status_code=401)
    form = await request.form()
    nombre = (form.get("nombre") or "").strip()
    if nombre:
        fnac = form.get("fecha_nacimiento") or ""
        parentesco = form.get("parentesco") or ""
        ejecutar_comando(
            "INSERT INTO pasajeros_reserva (id_reserva, nombre, fecha_nacimiento, parentesco) VALUES (?,?,?,?)",
            (id_reserva, nombre, fnac, parentesco),
        )
        registrar_cambio(id_reserva, "PASAJERO", f"Agregado: {nombre}", usuario=usuario_activo(request))
        # Guardar también en lista del cliente si se marcó
        if form.get("guardar_frecuente") == "1":
            df_cli = obtener_datos("SELECT id_cliente FROM reservas WHERE id_reserva=?", (id_reserva,))
            if not df_cli.empty:
                id_cliente = df_cli.iloc[0]["id_cliente"]
                existe = obtener_datos(
                    "SELECT 1 FROM acompanantes_cliente WHERE id_cliente=? AND nombre=?", (id_cliente, nombre)
                )
                if existe.empty:
                    ejecutar_comando(
                        "INSERT INTO acompanantes_cliente (id_cliente, nombre, fecha_nacimiento, parentesco) VALUES (?,?,?,?)",
                        (id_cliente, nombre, fnac, parentesco)
                    )
    return _pasajeros_html(request, id_reserva)


@app.post("/bitacora/{id_reserva}/pasajeros/agregar-frecuente/{id_acompanante}", response_class=HTMLResponse)
async def agregar_pasajero_frecuente(request: Request, id_reserva: int, id_acompanante: int):
    if not usuario_activo(request):
        return HTMLResponse("", status_code=401)
    df = obtener_datos("SELECT * FROM acompanantes_cliente WHERE id_acompanante=?", (id_acompanante,))
    if not df.empty:
        r = df.iloc[0]
        ejecutar_comando(
            "INSERT INTO pasajeros_reserva (id_reserva, nombre, fecha_nacimiento, parentesco) VALUES (?,?,?,?)",
            (id_reserva, r["nombre"], r["fecha_nacimiento"] or "", r["parentesco"] or "")
        )
        registrar_cambio(id_reserva, "PASAJERO", f"Agregado (frecuente): {r['nombre']}", usuario=usuario_activo(request))
    return _pasajeros_html(request, id_reserva)


@app.post("/bitacora/{id_reserva}/pasajeros/{id_pasajero}/eliminar", response_class=HTMLResponse)
async def eliminar_pasajero(request: Request, id_reserva: int, id_pasajero: int):
    if not usuario_activo(request):
        return HTMLResponse("", status_code=401)
    df = obtener_datos("SELECT nombre FROM pasajeros_reserva WHERE id_pasajero=?", (id_pasajero,))
    nombre = df.iloc[0]["nombre"] if not df.empty else "?"
    ejecutar_comando("DELETE FROM pasajeros_reserva WHERE id_pasajero=?", (id_pasajero,))
    registrar_cambio(id_reserva, "PASAJERO", f"Eliminado: {nombre}", usuario=usuario_activo(request))
    return _pasajeros_html(request, id_reserva)


@app.post("/bitacora/{id_reserva}/pasajeros/{id_pasajero}/guardar-frecuente", response_class=HTMLResponse)
async def guardar_pasajero_frecuente(request: Request, id_reserva: int, id_pasajero: int):
    if not usuario_activo(request):
        return HTMLResponse("", status_code=401)
    df_p = obtener_datos("SELECT nombre, fecha_nacimiento, parentesco FROM pasajeros_reserva WHERE id_pasajero=?", (id_pasajero,))
    if df_p.empty:
        return _pasajeros_html(request, id_reserva)
    p = df_p.iloc[0]
    df_r = obtener_datos("SELECT id_cliente FROM reservas WHERE id_reserva=?", (id_reserva,))
    if df_r.empty:
        return _pasajeros_html(request, id_reserva)
    id_cliente = int(df_r.iloc[0]["id_cliente"])
    # Solo insertar si no existe ya (por nombre)
    existe = obtener_datos(
        "SELECT 1 FROM acompanantes_cliente WHERE id_cliente=? AND nombre=?",
        (id_cliente, p["nombre"])
    )
    if existe.empty:
        ejecutar_comando(
            "INSERT INTO acompanantes_cliente (id_cliente, nombre, fecha_nacimiento, parentesco) VALUES (?,?,?,?)",
            (id_cliente, p["nombre"], p.get("fecha_nacimiento") or "", p.get("parentesco") or "")
        )
    return _pasajeros_html(request, id_reserva)


# ─── Alertas ─────────────────────────────────────────────────────────────────

@app.get("/alertas", response_class=HTMLResponse)
async def alertas(request: Request, mes_cumple: str = ""):
    if not usuario_activo(request):
        return RedirectResponse(url="/login")

    hoy = str(now_local().date())

    df_pagos = obtener_datos(
        """SELECT pp.numero_pago, ROUND(pp.monto_esperado - pp.monto_pagado, 2) as monto_esperado, pp.fecha_programada, r.moneda,
                  r.id_reserva, c.nombre
           FROM plan_pagos pp
           JOIN reservas r ON pp.id_reserva = r.id_reserva
           JOIN clientes c ON r.id_cliente = c.id_cliente
           WHERE pp.estado IN ('PENDIENTE','PARCIAL') AND r.estado = 'ACTIVO'
           ORDER BY pp.fecha_programada"""
    )
    df_op = obtener_datos(
        """SELECT r.id_reserva, c.nombre, r.destino, r.fecha_salida, r.fecha_regreso,
                  r.fecha_limite_proveedor, r.costo_total, r.checkin_ida, r.checkin_regreso,
                  r.hotel_confirmado, r.cobrado_cliente, r.venta_total, r.id_grupo
           FROM reservas r JOIN clientes c ON r.id_cliente = c.id_cliente
           WHERE r.estado = 'ACTIVO'"""
    )
    df_egresos_tc = obtener_datos(
        """SELECT id_reserva, SUM(monto) as total FROM flujo_caja
           WHERE tipo_movimiento='EGRESO' AND estado='ACTIVO'
             AND tipo_egreso='COSTO DIRECTO VIAJE' AND id_reserva IS NOT NULL
           GROUP BY id_reserva"""
    )
    df_sin_anticipo = obtener_datos(
        """SELECT r.id_reserva, c.nombre, r.destino, r.fecha_salida, r.venta_total
           FROM reservas r JOIN clientes c ON r.id_cliente = c.id_cliente
           WHERE r.estado='ACTIVO' AND r.cobrado_cliente = 0 AND r.venta_total > 0"""
    )
    import datetime as _dt
    from datetime import datetime as _dt2

    egresos_tc = {}
    if not df_egresos_tc.empty:
        for _, r in df_egresos_tc.iterrows():
            egresos_tc[int(r["id_reserva"])] = float(r["total"] or 0)

    # ── 1) Alertas de Pago a Proveedores — máxima prioridad (riesgo de multas) ──
    prov_items = []   # (tier, d_prov, dict) — tier 0 = muy atrasado (+30 días), ordena primero
    # ── 2) Alertas de Viaje — check-in, nada de dinero aquí ──
    viaje_items = []  # (tier, dias, dict) — tier 0 = muy atrasado (+30 días)
    # ── 4) Advertencias — cobranza próxima (no vencida) + sin anticipo ──
    advertencias = []
    # Parcialidades vencidas por reserva, para el detalle de Cartera Vencida
    parcialidades_vencidas = {}

    pagos_records = df_pagos.to_dict("records") if not df_pagos.empty else []
    for p in pagos_records:
        try:
            d = (_dt2.strptime(p["fecha_programada"], "%Y-%m-%d").date() - _dt2.strptime(hoy, "%Y-%m-%d").date()).days
            _pn = _esc(str(p['nombre']))
            if d < 0:
                parcialidades_vencidas.setdefault(int(p['id_reserva']), []).append((p, abs(d)))
            elif 0 <= d <= 7:
                advertencias.append({
                    "icono": "⏳", "tipo": f"COBRANZA EN {d} DÍA(S)",
                    "texto": f"<b>{_pn}</b> — Parcialidad #{p['numero_pago']} por "
                             f"<b>${p['monto_esperado']:,.2f} {p['moneda']}</b>. Itin #{p['id_reserva']}",
                    "link": f"/bitacora/{p['id_reserva']}"
                })
        except Exception:
            pass

    op_records = df_op.to_dict("records") if not df_op.empty else []
    for r in op_records:
        try:
            sal = _dt2.strptime(str(r["fecha_salida"]), "%Y-%m-%d").date()
            reg = _dt2.strptime(str(r["fecha_regreso"]), "%Y-%m-%d").date()
            hoy_d = _dt2.strptime(hoy, "%Y-%m-%d").date()
            ds = (sal - hoy_d).days
            dr = (reg - hoy_d).days
            rid = int(r["id_reserva"])
            _rn = _esc(str(r['nombre'])); _rd = _esc(str(r['destino']))

            if ds <= 3 and r["checkin_ida"] == 0:
                if ds < -30:
                    viaje_items.append((0, ds, {"icono": "🚨🚨", "tipo": "CHECK-IN IDA MUY ATRASADO (+30 DÍAS)",
                        "texto": f"<b>{_rn}</b> voló a <b>{_rd}</b> hace <b>{abs(ds)} día(s)</b> y sigue sin registrarse. ¡Revisar y corregir ya!",
                        "link": f"/bitacora/{rid}", "escalado": True}))
                elif ds < 0:
                    viaje_items.append((1, ds, {"icono": "🛫", "tipo": "CHECK-IN IDA NO REALIZADO",
                        "texto": f"<b>{_rn}</b> voló a <b>{_rd}</b> hace <b>{abs(ds)} día(s)</b> y no se ha registrado el check-in.",
                        "link": f"/bitacora/{rid}", "escalado": False}))
                else:
                    viaje_items.append((1, ds, {"icono": "🛫", "tipo": "CHECK-IN IDA",
                        "texto": f"<b>{_rn}</b> vuela a <b>{_rd}</b> en <b>{ds} día(s)</b>.",
                        "link": f"/bitacora/{rid}", "escalado": False}))

            if dr <= 3 and r["checkin_regreso"] == 0:
                if dr < -30:
                    viaje_items.append((0, dr, {"icono": "🚨🚨", "tipo": "CHECK-IN REGRESO MUY ATRASADO (+30 DÍAS)",
                        "texto": f"<b>{_rn}</b> regresó de <b>{_rd}</b> hace <b>{abs(dr)} día(s)</b> y sigue sin registrarse. ¡Revisar y corregir ya!",
                        "link": f"/bitacora/{rid}", "escalado": True}))
                elif dr < 0:
                    viaje_items.append((1, dr, {"icono": "🛬", "tipo": "CHECK-IN REGRESO NO REALIZADO",
                        "texto": f"<b>{_rn}</b> regresó de <b>{_rd}</b> hace <b>{abs(dr)} día(s)</b> y no se ha registrado el check-in.",
                        "link": f"/bitacora/{rid}", "escalado": False}))
                else:
                    viaje_items.append((1, dr, {"icono": "🛬", "tipo": "CHECK-IN REGRESO",
                        "texto": f"<b>{_rn}</b> regresa de <b>{_rd}</b> en <b>{dr} día(s)</b>.",
                        "link": f"/bitacora/{rid}", "escalado": False}))

            # Reservas de grupo: el pago a proveedor se controla en /grupos/{id}, no aquí —
            # incluirlas duplicaría la alerta una vez por cada pax/familia del mismo grupo.
            lp = str(r["fecha_limite_proveedor"] or "") if pd.isna(r.get("id_grupo")) else ""
            if lp and lp not in ("", "None", "nan"):
                dp = (_dt2.strptime(lp, "%Y-%m-%d").date() - hoy_d).days
                costo_prov = float(r["costo_total"] or 0)
                egreso_prov = egresos_tc.get(rid, 0.0)
                falta_prov = costo_prov - egreso_prov
                if costo_prov > 0 and falta_prov > 0.01:
                    if dp < -30:
                        prov_items.append((0, dp, {"icono": "🚨🚨", "tipo": "PAGO A PROVEEDOR MUY ATRASADO (+30 DÍAS)",
                            "texto": f"El límite de <b>{_rn}</b> (<b>{_rd}</b>) venció hace <b>{abs(dp)} día(s)</b> y sigue sin pagarse. Falta <b>${falta_prov:,.2f}</b>. Itin #{rid}. ¡Revisar y corregir ya — riesgo de multa!",
                            "link": f"/bitacora/{rid}", "escalado": True}))
                    elif dp < 0:
                        prov_items.append((1, dp, {"icono": "🚨", "tipo": "PAGO A PROVEEDOR VENCIDO",
                            "texto": f"El límite de <b>{_rn}</b> (<b>{_rd}</b>) venció hace <b>{abs(dp)} día(s)</b>. Falta pagar <b>${falta_prov:,.2f}</b>. Itin #{rid}. ¡Riesgo de recargo o multa!",
                            "link": f"/bitacora/{rid}", "escalado": False}))
                    elif dp <= 7:
                        prov_items.append((1, dp, {"icono": "⏰", "tipo": f"PAGO A PROVEEDOR EN {dp} DÍA(S)",
                            "texto": f"<b>{_rn}</b> (<b>{_rd}</b>) — falta pagar <b>${falta_prov:,.2f}</b>. Itin #{rid}. ¡Actuar de inmediato!",
                            "link": f"/bitacora/{rid}", "escalado": False}))
                    elif dp <= 15:
                        prov_items.append((1, dp, {"icono": "💸", "tipo": "PAGO A PROVEEDOR",
                            "texto": f"<b>{_rn}</b> (<b>{_rd}</b>) vence en <b>{dp} día(s)</b> — falta pagar <b>${falta_prov:,.2f}</b>. Itin #{rid}",
                            "link": f"/bitacora/{rid}", "escalado": False}))
        except Exception as e:
            logging.error(f"Alertas op reserva {r.get('id_reserva','?')}: {e}")

    sin_anticipo = df_sin_anticipo.to_dict("records") if not df_sin_anticipo.empty else []
    for r in sin_anticipo:
        advertencias.append({"icono": "💳", "tipo": "SIN ANTICIPO",
            "texto": f"Itin #{r['id_reserva']} — <b>{_esc(str(r['nombre']))}</b> a <b>{_esc(str(r['destino']))}</b> "
                     f"(salida {_esc(str(r['fecha_salida']))}) sin ningún pago registrado. Venta: <b>${r['venta_total']:,.2f}</b>.",
            "link": f"/bitacora/{r['id_reserva']}"})

    # Orden final: tier 0 (muy atrasado) siempre primero; en Alertas de Viaje además "0 días"
    # tiene prioridad y los empates se resuelven a favor de lo ya vencido.
    prov_items.sort(key=lambda x: (x[0], x[1]))
    viaje_items.sort(key=lambda x: (x[0], x[1]) if x[0] == 0 else (x[0], abs(x[1]), x[1] > 0))
    criticas = [it[2] for it in prov_items]
    viajes_alertas = [it[2] for it in viaje_items]

    # ── Radar de cumpleaños ──────────────────────────────────────────────────────
    from datetime import timedelta as _td
    import datetime as _dt3
    hoy_d = _dt3.date.fromisoformat(hoy)
    proximos_md = [(hoy_d + _td(days=i)).strftime('%m-%d') for i in range(1, 8)]
    meses_validos = {f"{i:02d}" for i in range(1, 13)}
    mes_cumple_mm = mes_cumple if mes_cumple in meses_validos else hoy_d.strftime('%m')
    placeholder_7 = ','.join(['?'] * 7)
    df_cumples_prox = obtener_datos(
        f"SELECT nombre, telefono, fecha_nacimiento FROM clientes "
        f"WHERE fecha_nacimiento IS NOT NULL AND fecha_nacimiento != '' "
        f"AND strftime('%m-%d', fecha_nacimiento) IN ({placeholder_7}) "
        f"ORDER BY strftime('%m-%d', fecha_nacimiento)",
        tuple(proximos_md)
    )
    df_cumples_mes = obtener_datos(
        "SELECT nombre, telefono, fecha_nacimiento FROM clientes "
        "WHERE fecha_nacimiento IS NOT NULL AND fecha_nacimiento != '' "
        "AND strftime('%m', fecha_nacimiento) = ? "
        "ORDER BY strftime('%d', fecha_nacimiento)",
        (mes_cumple_mm,)
    )

    def _parse_cumple(fnac_str, ref_year):
        from datetime import datetime as _dt4
        try:
            b = _dt4.strptime(str(fnac_str)[:10], "%Y-%m-%d").date()
            edad = ref_year - b.year
            return b, edad
        except Exception:
            return None, None

    cumples_proximos = []
    for _, c in (df_cumples_prox.iterrows() if not df_cumples_prox.empty else []):
        b, edad = _parse_cumple(c["fecha_nacimiento"], hoy_d.year)
        if b is None:
            continue
        cumple_este = b.replace(year=hoy_d.year)
        dias_f = (cumple_este - hoy_d).days
        if dias_f < 0:
            cumple_este = b.replace(year=hoy_d.year + 1)
            dias_f = (cumple_este - hoy_d).days
        valida = 1 <= edad <= 110
        cumples_proximos.append({
            "nombre": c["nombre"],
            "telefono": c.get("telefono") or "—",
            "dia": b.day,
            "fecha": cumple_este.strftime("%d/%m"),
            "edad": edad,
            "dias_f": dias_f,
            "valida": valida,
        })

    cumples_mes = []
    for _, c in (df_cumples_mes.iterrows() if not df_cumples_mes.empty else []):
        b, edad = _parse_cumple(c["fecha_nacimiento"], hoy_d.year)
        if b is None:
            continue
        valida = 1 <= edad <= 110
        cumples_mes.append({
            "nombre": c["nombre"],
            "telefono": c.get("telefono") or "—",
            "dia": b.day,
            "edad": edad,
            "valida": valida,
            "es_hoy": b.month == hoy_d.month and b.day == hoy_d.day,
        })

    import calendar as _cal_m
    meses_dict = {f"{i:02d}": _cal_m.month_name[i].capitalize() for i in range(1, 13)}
    nombre_mes_actual = meses_dict[mes_cumple_mm]

    # ── Cartera Vencida ─────────────────────────────────────────────────────────
    import re as _re
    df_cv = obtener_datos("""
        SELECT r.id_reserva, c.nombre, c.telefono, r.destino,
               r.fecha_regreso, r.venta_total, r.cobrado_cliente,
               (r.venta_total - r.cobrado_cliente) AS deuda, r.moneda
        FROM reservas r JOIN clientes c ON r.id_cliente = c.id_cliente
        WHERE r.estado = 'ACTIVO'
          AND (r.venta_total - r.cobrado_cliente) > 1.0
          AND (
              r.fecha_regreso < DATE('now', '-6 hours')
              OR EXISTS (
                  SELECT 1 FROM plan_pagos pp
                  WHERE pp.id_reserva = r.id_reserva
                    AND pp.estado IN ('PENDIENTE','PARCIAL')
                    AND pp.fecha_programada < DATE('now', '-6 hours')
              )
          )
        ORDER BY deuda DESC
    """)
    _cartera_items_sort = []  # (tier, -deuda, dict) — tier 0 = muy atrasado (+30 días)
    if not df_cv.empty:
        import datetime as _dt_
        from datetime import datetime as _dt2_
        for _, row in df_cv.iterrows():
            rid = int(row["id_reserva"])
            try:
                dias_reg = (_dt2_.strptime(hoy, "%Y-%m-%d").date() - _dt2_.strptime(str(row["fecha_regreso"]), "%Y-%m-%d").date()).days
                estado_viaje = f"Regresó hace {dias_reg} día(s)" if dias_reg > 0 else "Viaje activo"
            except Exception:
                dias_reg = 0
                estado_viaje = "Viaje activo"
            tel_limpio = _re.sub(r'\D', '', str(row.get("telefono") or ""))
            msg_wa = (f"Hola {str(row['nombre']).split()[0]}, te recordamos que tienes un saldo pendiente "
                      f"de ${float(row['deuda']):,.2f} {row['moneda']} por tu viaje a {row['destino']}. "
                      f"Tu Agencia de Viajes.")

            _parciales = parcialidades_vencidas.get(rid)
            if _parciales:
                _items_parcial = [f"Parcialidad #{p['numero_pago']}: ${p['monto_esperado']:,.2f} {p['moneda']} (venció hace {dv} días — {p['fecha_programada']})" for p, dv in _parciales]
                detalle_parcial = " · ".join(_items_parcial)
                dias_atraso_max = max(dv for _, dv in _parciales)
            else:
                detalle_parcial = ""
                dias_atraso_max = dias_reg if dias_reg > 0 else 0

            escalado = dias_atraso_max > 30
            item = {
                "id_reserva": rid,
                "nombre": row["nombre"],
                "telefono": row.get("telefono") or "—",
                "destino": row["destino"],
                "fecha_regreso": row["fecha_regreso"],
                "venta_total": float(row["venta_total"] or 0),
                "cobrado_cliente": float(row["cobrado_cliente"] or 0),
                "deuda": float(row["deuda"] or 0),
                "moneda": row["moneda"],
                "estado_viaje": estado_viaje,
                "detalle_parcial": detalle_parcial,
                "dias_atraso_max": dias_atraso_max,
                "escalado": escalado,
                "wa_url": f"https://wa.me/52{tel_limpio}?text={urllib.parse.quote(msg_wa, safe='')}",
            }
            _cartera_items_sort.append((0 if escalado else 1, -float(row['deuda'] or 0), item))

    _cartera_items_sort.sort(key=lambda x: (x[0], x[1]))
    cartera_vencida = [it[2] for it in _cartera_items_sort]

    total_vencido_mxn = sum(r["deuda"] for r in cartera_vencida if r["moneda"] == "MXN")
    total_vencido_usd = sum(r["deuda"] for r in cartera_vencida if r["moneda"] == "USD")

    # ── Cartera al Corriente ─────────────────────────────────────────────────────
    df_cc = obtener_datos("""
        SELECT r.id_reserva, c.nombre, r.destino,
               r.fecha_salida, r.fecha_regreso, r.venta_total, r.cobrado_cliente,
               (r.venta_total - r.cobrado_cliente) AS deuda, r.moneda
        FROM reservas r JOIN clientes c ON r.id_cliente = c.id_cliente
        WHERE r.estado = 'ACTIVO'
          AND (r.venta_total - r.cobrado_cliente) > 1.0
          AND r.fecha_regreso >= DATE('now', '-6 hours')
          AND NOT EXISTS (
              SELECT 1 FROM plan_pagos pp
              WHERE pp.id_reserva = r.id_reserva
                AND pp.estado IN ('PENDIENTE','PARCIAL')
                AND pp.fecha_programada < DATE('now', '-6 hours')
          )
        ORDER BY r.fecha_salida ASC
    """)
    cartera_corriente = df_cc.to_dict("records") if not df_cc.empty else []
    total_corriente_mxn = sum(float(r["deuda"] or 0) for r in cartera_corriente if r["moneda"] == "MXN")
    total_corriente_usd = sum(float(r["deuda"] or 0) for r in cartera_corriente if r["moneda"] == "USD")

    return templates.TemplateResponse(request, "alertas.html", ctx(request, {
        "active": "alertas",
        "alertas_proveedor": criticas,
        "alertas_viaje": viajes_alertas,
        "advertencias": advertencias,
        "cartera_vencida": cartera_vencida,
        "total_vencido_mxn": total_vencido_mxn,
        "total_vencido_usd": total_vencido_usd,
        "cartera_corriente": cartera_corriente,
        "total_corriente_mxn": total_corriente_mxn,
        "total_corriente_usd": total_corriente_usd,
        "cumples_proximos": cumples_proximos,
        "cumples_mes": cumples_mes,
        "nombre_mes_actual": nombre_mes_actual,
        "mes_cumple_mm": mes_cumple_mm,
        "meses_dict": meses_dict,
    }))


@app.get("/alertas/cartera/exportar")
async def alertas_cartera_exportar(request: Request):
    if not usuario_activo(request):
        return RedirectResponse(url="/login")
    df = obtener_datos("""
        SELECT r.id_reserva AS 'Itin #', c.nombre AS Cliente, c.telefono AS Teléfono,
               r.destino AS Destino, r.fecha_regreso AS 'Regresó',
               r.venta_total AS Venta, r.cobrado_cliente AS Cobrado,
               (r.venta_total - r.cobrado_cliente) AS Deuda, r.moneda AS Moneda
        FROM reservas r JOIN clientes c ON r.id_cliente = c.id_cliente
        WHERE r.estado = 'ACTIVO' AND (r.venta_total - r.cobrado_cliente) > 1.0
          AND (r.fecha_regreso < DATE('now', '-6 hours')
               OR EXISTS (SELECT 1 FROM plan_pagos pp WHERE pp.id_reserva = r.id_reserva
                          AND pp.estado IN ('PENDIENTE','PARCIAL') AND pp.fecha_programada < DATE('now','-6 hours')))
        ORDER BY Deuda DESC
    """)
    return _excel_response(df, f"CarteraVencida_{now_local().date()}.xlsx")


# ─── Plan de pagos (HTMX) ────────────────────────────────────────────────────

def _plan_ctx(id_reserva):
    df_r = obtener_datos("SELECT venta_total, cobrado_cliente, moneda FROM reservas WHERE id_reserva=?", (id_reserva,))
    reserva = df_r.iloc[0].to_dict() if not df_r.empty else {}
    reserva["id_reserva"] = id_reserva
    df_plan = obtener_datos(
        "SELECT id_pago, numero_pago, monto_esperado, monto_pagado, fecha_programada, estado FROM plan_pagos WHERE id_reserva=? ORDER BY numero_pago",
        (id_reserva,)
    )
    saldo = calcular_saldo_real(id_reserva)
    # Candado de consistencia (igual que el_sistema_legado app.py:1650-1658): el anticipo +
    # la suma de monto_esperado del plan debe cuadrar con venta_total. Si algo desalineó
    # la reserva (edición manual, bug, etc.) esto lo saca a la luz en vez de fallar en silencio.
    candado_dif = None
    if not df_plan.empty:
        df_ant = obtener_datos(
            "SELECT COALESCE(SUM(monto),0) as t FROM flujo_caja WHERE id_reserva=? AND tipo_movimiento='INGRESO' AND estado='ACTIVO' AND concepto LIKE '%Anticipo%'",
            (id_reserva,)
        )
        anticipo = float(df_ant["t"].iloc[0])
        plan_total = float(df_plan["monto_esperado"].sum())
        venta = float(reserva.get("venta_total") or 0)
        dif = round(venta - (anticipo + plan_total), 2)
        if abs(dif) > 1.0:
            candado_dif = {"anticipo": anticipo, "plan_total": plan_total, "venta": venta, "dif": abs(dif), "signo": "falta" if dif > 0 else "sobra"}
    return {
        "reserva": reserva,
        "plan_pagos": df_plan.to_dict("records"),
        "saldo": saldo,
        "today": str(now_local().date()),
        "candado_dif": candado_dif,
    }

def _plan_html(request, id_reserva):
    ctx_data = _plan_ctx(id_reserva)
    return templates.TemplateResponse(request, "plan_pagos_section.html", {"request": request, **ctx_data})

def _plan_str(id_reserva):
    return _jinja_env.get_template("plan_pagos_section.html").render(**_plan_ctx(id_reserva))


def _cobros_ctx(id_reserva):
    df_r = obtener_datos("SELECT moneda FROM reservas WHERE id_reserva=?", (id_reserva,))
    moneda = df_r.iloc[0]["moneda"] if not df_r.empty else "MXN"
    df_cobros = obtener_datos(
        """SELECT id_movimiento, concepto, monto, fecha_pago, metodo_pago, cuenta_destino
           FROM flujo_caja
           WHERE id_reserva=? AND tipo_movimiento='INGRESO' AND estado='ACTIVO'
           ORDER BY id_movimiento""",
        (id_reserva,)
    )
    df_plan = obtener_datos(
        "SELECT numero_pago, monto_esperado, monto_pagado, estado FROM plan_pagos WHERE id_reserva=? ORDER BY numero_pago",
        (id_reserva,)
    )
    saldo = calcular_saldo_real(id_reserva)
    return {
        "id_reserva": id_reserva,
        "moneda": moneda,
        "cobros": df_cobros.to_dict("records"),
        "plan_pagos": df_plan.to_dict("records"),
        "saldo": saldo,
        "today": str(now_local().date()),
        "cuentas_destino": CUENTAS_SIMPLE_DEFAULT,
        "cuentas_por_metodo": CUENTAS_POR_METODO,
    }

def _cobros_html(request, id_reserva):
    ctx_data = _cobros_ctx(id_reserva)
    return templates.TemplateResponse(request, "cobros_section.html", {"request": request, **ctx_data})

def _cobros_str(id_reserva):
    return _jinja_env.get_template("cobros_section.html").render(**_cobros_ctx(id_reserva))


@app.post("/bitacora/{id_reserva}/plan-pagos/generar", response_class=HTMLResponse)
async def generar_plan(request: Request, id_reserva: int):
    if not usuario_activo(request):
        return HTMLResponse("", status_code=401)
    form = await request.form()
    usuario = usuario_activo(request)

    try:
        n_pagos = int(form.get("n_pagos") or 1)
        n_pagos = max(1, min(n_pagos, 24))
    except Exception:
        n_pagos = 1

    n_pagados = int(obtener_datos(
        "SELECT COUNT(*) as n FROM plan_pagos WHERE id_reserva=? AND estado='PAGADO'", (id_reserva,)
    ).iloc[0]["n"])

    fechas_montos = []
    for i in range(1, n_pagos + 1):
        fecha = (form.get(f"fecha_{i}") or "").strip()
        try:
            monto = float(form.get(f"monto_{i}") or 0)
        except Exception:
            monto = 0.0
        if fecha and monto > 0:
            fechas_montos.append((fecha, monto))

    # Candado server-side igual que Streamlit: la suma del plan nuevo debe cuadrar
    # contra el saldo pendiente REAL de la reserva (venta_total - cobrado en
    # flujo_caja, fuente de verdad) — el JS del formulario se puede evadir o
    # quedar desactualizado. Las filas ya PAGADO del plan no se tocan ni se
    # vuelven a contar aquí: lo cobrado ya está reflejado en calcular_saldo_real.
    _saldo_real = calcular_saldo_real(id_reserva)
    _suma_nueva = sum(m for _, m in fechas_montos)
    _saldo_pendiente = _saldo_real["saldo_pendiente"]
    if _saldo_pendiente > 0 and abs(_suma_nueva - _saldo_pendiente) > 1.0:
        request.session["flash"] = {"tipo": "error", "texto": f"⚠️ El plan no cierra: la suma de los pagos debe ser ${_saldo_pendiente:,.2f}."}
        return _plan_html(request, id_reserva)

    ops = [("DELETE FROM plan_pagos WHERE id_reserva=? AND estado IN ('PENDIENTE','PARCIAL')", (id_reserva,))]
    filas_insertadas = 0
    for fecha, monto in fechas_montos:
        ops.append((
            "INSERT INTO plan_pagos (id_reserva, numero_pago, monto_esperado, fecha_programada) VALUES (?,?,?,?)",
            (id_reserva, n_pagados + filas_insertadas + 1, monto, fecha)
        ))
        filas_insertadas += 1

    if filas_insertadas > 0:
        ejecutar_transaccion(ops)
        actualizar_estado_plan_pagos(id_reserva)
        registrar_cambio(id_reserva, "PLAN PAGOS", f"Plan generado: {filas_insertadas} parcialidades", usuario=usuario)

    # HTMX: respuesta parcial. POST normal: redirigir al detalle
    if request.headers.get("HX-Request") == "true":
        return _plan_html(request, id_reserva)
    request.session["flash"] = {"tipo": "ok", "texto": f"✅ Plan de {filas_insertadas} parcialidades generado."}
    return RedirectResponse(url=f"/bitacora/{id_reserva}", status_code=303)


@app.post("/bitacora/{id_reserva}/plan-pagos/eliminar/{id_pago}", response_class=HTMLResponse)
async def eliminar_pago_plan(request: Request, id_reserva: int, id_pago: int):
    if not usuario_activo(request):
        return HTMLResponse("", status_code=401)
    ejecutar_comando("DELETE FROM plan_pagos WHERE id_pago=? AND estado IN ('PENDIENTE','PARCIAL')", (id_pago,))
    actualizar_estado_plan_pagos(id_reserva)
    registrar_cambio(id_reserva, "PLAN PAGOS", "Parcialidad eliminada", usuario=usuario_activo(request))
    return _plan_html(request, id_reserva)


@app.post("/bitacora/{id_reserva}/cobros/registrar", response_class=HTMLResponse)
async def registrar_cobro(request: Request, id_reserva: int):
    if not usuario_activo(request):
        return HTMLResponse("", status_code=401)
    form = await request.form()
    usuario = usuario_activo(request)

    def _f(k, d=0.0):
        try: return float(form.get(k) or d)
        except: return d

    monto = _f("monto")
    if monto <= 0:
        return _cobros_html(request, id_reserva)

    # Candado igual que Streamlit: no permitir un abono que supere el saldo
    # pendiente (con 1% de margen por redondeos) — sin este límite el saldo
    # queda negativo y la cascada de actualizar_estado_plan_pagos se confunde.
    _saldo_pre = calcular_saldo_real(id_reserva)
    if monto > _saldo_pre["saldo_pendiente"] * 1.01:
        request.session["flash"] = {"tipo": "error", "texto": f"⚠️ El abono (${monto:,.2f}) supera el saldo pendiente (${_saldo_pre['saldo_pendiente']:,.2f})."}
        return _cobros_html(request, id_reserva)

    metodo      = form.get("metodo_pago") or "Transferencia"
    tipo        = form.get("tipo_cobro") or "Abono"
    fecha       = form.get("fecha_pago") or str(now_local().date())
    notas       = (form.get("notas") or "").strip()
    pct_com     = _f("comision_pct")
    ajuste_plan = (form.get("ajuste_plan") or "ninguno").strip()
    cuenta_destino = _resolver_cuenta_destino(form.get("cuenta_destino"), metodo)

    if tipo == "Anticipo":
        concepto = f"[{metodo}] Anticipo Inicial"
        categoria = "Abono Parcial de Viaje"
    elif tipo == "Parcialidad":
        num = form.get("num_parcialidad") or ""
        concepto = f"[{metodo}] Parcialidad #{num}" if num else f"[{metodo}] Parcialidad"
        categoria = "Abono Parcial de Viaje"
    else:
        concepto = f"[{metodo}] Abono" + (f" — {notas}" if notas else "")
        categoria = "Abono Parcial de Viaje"

    df_r = obtener_datos("SELECT moneda FROM reservas WHERE id_reserva=?", (id_reserva,))
    moneda = df_r.iloc[0]["moneda"] if not df_r.empty else "MXN"

    ops = [(
        "INSERT INTO flujo_caja (id_reserva, tipo_movimiento, tipo_egreso, categoria, concepto, monto, moneda, fecha_pago, usuario_creador, metodo_pago, cuenta_destino, fecha_creacion) VALUES (?, 'INGRESO', 'NO APLICA', ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (id_reserva, categoria, concepto, monto, moneda, fecha, usuario, metodo, cuenta_destino, str(now_local().date()))
    )]
    if metodo == "Tarjeta de Crédito/Débito" and pct_com > 0:
        comision = round(monto * pct_com / 100, 2)
        # id_movimiento_vinculado apunta al abono (INGRESO) recién insertado, vía
        # last_insert_rowid() de la MISMA transacción — permite cancelar la comisión
        # si el abono se anula después (ver anular_cobro).
        ops.append((
            "INSERT INTO flujo_caja (id_reserva, tipo_movimiento, tipo_egreso, categoria, concepto, monto, moneda, fecha_pago, fecha_creacion, id_movimiento_vinculado) VALUES (?, 'EGRESO', 'COSTO DIRECTO VIAJE', 'Comisiones Bancarias', ?, ?, ?, ?, ?, (SELECT last_insert_rowid()))",
            (id_reserva, f"[Automático] Comisión Tarjeta", comision, moneda, fecha, str(now_local().date()))
        ))
        # Igual que Streamlit: la comisión también se refleja en las columnas de
        # reservas (costo_comisiones/costo_total/utilidad_proyectada), no solo en
        # flujo_caja — de lo contrario esas columnas quedan congeladas desde la
        # creación y cualquier reporte que las lea directo subestima el costo real.
        ops.append((
            "UPDATE reservas SET costo_comisiones = ROUND(costo_comisiones + ?, 2), costo_total = ROUND(costo_total + ?, 2), utilidad_proyectada = ROUND(utilidad_proyectada - ?, 2) WHERE id_reserva = ?",
            (comision, comision, comision, id_reserva)
        ))
    ejecutar_transaccion(ops)

    from database import sincronizar_cobrado_cliente
    sincronizar_cobrado_cliente(id_reserva)

    # El plan de pagos ya NO se muta manualmente aquí — monto_esperado de cada
    # parcialidad es fijo. actualizar_estado_plan_pagos() asigna el total cobrado
    # en cascada (más antigua primero) y calcula monto_pagado/estado de cada una.
    actualizar_estado_plan_pagos(id_reserva)
    registrar_cambio(id_reserva, "COBRO", f"{concepto} — ${monto:,.2f} {moneda}", usuario=usuario)

    if request.headers.get("HX-Request") == "true":
        plan_oob = _plan_str(id_reserva).replace(
            '<div id="seccion-plan-pagos"',
            '<div id="seccion-plan-pagos" hx-swap-oob="true"',
            1
        )
        return HTMLResponse(_cobros_str(id_reserva) + "\n" + plan_oob)
    request.session["flash"] = {"tipo": "ok", "texto": f"✅ Cobro de ${monto:,.2f} {moneda} registrado."}
    return RedirectResponse(url=f"/bitacora/{id_reserva}", status_code=303)


@app.post("/bitacora/{id_reserva}/cobros/{id_mov}/anular", response_class=HTMLResponse)
async def anular_cobro(request: Request, id_reserva: int, id_mov: int):
    if not usuario_activo(request):
        return HTMLResponse("", status_code=401)
    form = await request.form()
    motivo = (form.get("motivo") or "Sin motivo").strip()
    # Ver nota en anular_extra: por defecto se revierte la comisión (error de captura). Si
    # es una devolución al cliente, el banco no la regresa — se deja como costo real.
    revertir_comision = (form.get("revertir_comision") or "1") == "1"
    # Guardar datos del movimiento antes de cancelar
    df_mov = obtener_datos("SELECT tipo_movimiento, monto, concepto, categoria FROM flujo_caja WHERE id_movimiento=?", (id_mov,))
    tipo_mov = df_mov.iloc[0]["tipo_movimiento"] if not df_mov.empty else "INGRESO"
    monto_mov = float(df_mov.iloc[0]["monto"] or 0) if not df_mov.empty else 0
    concepto_mov = str(df_mov.iloc[0]["concepto"] or "") if not df_mov.empty else ""
    categoria_mov = str(df_mov.iloc[0]["categoria"] or "") if not df_mov.empty else ""

    ejecutar_comando(
        "UPDATE flujo_caja SET estado='CANCELADO', motivo_anulacion=? WHERE id_movimiento=? AND tipo_movimiento='INGRESO'",
        (motivo, id_mov)
    )
    # Extra "Pagado al Momento" pagado en el mismo recibo: revertir venta_total y borrar el registro,
    # igual que hace el panel de "Anular Extra" — si no se hace aquí, venta_total queda inflado
    # de forma permanente (el registro deja de existir y ya no se puede revertir desde ahí).
    if tipo_mov == "INGRESO" and categoria_mov == "Cobro Extra al Cliente":
        df_extra_void = obtener_datos(
            "SELECT id_extra, monto_cobrado_cliente FROM extras_viaje WHERE id_reserva=? AND tipo_registro='PAGADO_MOMENTO' AND ABS(monto_cobrado_cliente-?)<0.01 LIMIT 1",
            (id_reserva, monto_mov)
        )
        if not df_extra_void.empty:
            _id_extra_void = int(df_extra_void.iloc[0]["id_extra"])
            _monto_extra_void = round(float(df_extra_void.iloc[0]["monto_cobrado_cliente"]), 2)
            ejecutar_comando("DELETE FROM extras_viaje WHERE id_extra=?", (_id_extra_void,))
            ejecutar_comando("UPDATE reservas SET venta_total = ROUND(venta_total - ?, 2) WHERE id_reserva=?", (_monto_extra_void, id_reserva))
    # Si este abono tenía una comisión bancaria automática vinculada, se cancela también —
    # por defecto (error de captura: el abono nunca se hizo, la comisión tampoco se cobró).
    # Si el motivo real es una devolución al cliente, revertir_comision=False y la comisión
    # se deja activa — el banco no la regresa aunque el dinero sí se le devuelva al cliente.
    if tipo_mov == "INGRESO":
        df_comi_void = obtener_datos(
            "SELECT id_movimiento, monto FROM flujo_caja WHERE id_movimiento_vinculado=? AND categoria='Comisiones Bancarias' AND estado='ACTIVO'",
            (id_mov,)
        ) if revertir_comision else pd.DataFrame()
        if not df_comi_void.empty:
            _id_comi_void = int(df_comi_void.iloc[0]["id_movimiento"])
            _monto_comi_void = round(float(df_comi_void.iloc[0]["monto"] or 0), 2)
            ejecutar_comando(
                "UPDATE flujo_caja SET estado='CANCELADO', motivo_anulacion=? WHERE id_movimiento=?",
                (f"Auto-cancelada: comisión del abono #{id_mov}, anulado. {motivo}", _id_comi_void)
            )
            # Revertir el reflejo en reservas que se aplicó al registrar la comisión
            # (ver registrar_cobro) — si no, costo_comisiones/costo_total quedan inflados
            # de forma permanente tras anular el abono que la originó.
            ejecutar_comando(
                "UPDATE reservas SET costo_comisiones = ROUND(costo_comisiones - ?, 2), costo_total = ROUND(costo_total - ?, 2), utilidad_proyectada = ROUND(utilidad_proyectada + ?, 2) WHERE id_reserva = ?",
                (_monto_comi_void, _monto_comi_void, _monto_comi_void, id_reserva)
            )
            registrar_cambio(id_reserva, "COSTOS ACTUALIZADOS", f"Comisión bancaria #{_id_comi_void} (${_monto_comi_void:,.2f}) cancelada y revertida por anulación del abono #{id_mov}.", usuario=usuario_activo(request))
    # Registro de auditoría
    ejecutar_comando(
        "INSERT INTO anulaciones_audit (id_movimiento, tipo_movimiento, monto_anulado, usuario_anulo, fecha_anulacion, razon_anulacion, movimiento_original) VALUES (?,?,?,?,?,?,?)",
        (id_mov, tipo_mov, monto_mov, usuario_activo(request), str(now_local().date()), motivo, concepto_mov)
    )
    from database import sincronizar_cobrado_cliente
    sincronizar_cobrado_cliente(id_reserva)
    actualizar_estado_plan_pagos(id_reserva)
    registrar_cambio(id_reserva, "ANULACIÓN", f"Cobro #{id_mov} anulado: {motivo}", usuario=usuario_activo(request))
    plan_oob = _plan_str(id_reserva).replace(
        '<div id="seccion-plan-pagos"',
        '<div id="seccion-plan-pagos" hx-swap-oob="true"',
        1
    )
    return HTMLResponse(_cobros_str(id_reserva) + "\n" + plan_oob)


@app.get("/bitacora/{id_reserva}/cobros/{id_mov}/pdf")
async def pdf_recibo_cobro(request: Request, id_reserva: int, id_mov: int, copia: str = "cliente"):
    if copia not in ("cliente", "interna"):
        copia = "cliente"
    if not usuario_activo(request):
        return RedirectResponse(url="/login")
    import re as _re_pdf
    from pdf_engine import generar_recibo_pdf as _gen_recibo
    df_mov = obtener_datos(
        "SELECT id_movimiento, concepto, monto, moneda, fecha_pago, metodo_pago, usuario_creador, cuenta_destino "
        "FROM flujo_caja WHERE id_movimiento=? AND id_reserva=? AND tipo_movimiento='INGRESO'",
        (id_mov, id_reserva)
    )
    if df_mov.empty:
        return RedirectResponse(url=f"/bitacora/{id_reserva}")
    mov = df_mov.iloc[0].to_dict()
    df_res = obtener_datos(
        "SELECT r.venta_total, r.destino, c.nombre, c.telefono, c.email "
        "FROM reservas r JOIN clientes c ON r.id_cliente=c.id_cliente WHERE r.id_reserva=?",
        (id_reserva,)
    )
    if df_res.empty:
        return RedirectResponse(url=f"/bitacora/{id_reserva}")
    res = df_res.iloc[0].to_dict()
    df_total = obtener_datos(
        "SELECT COALESCE(SUM(monto), 0) as total FROM flujo_caja "
        "WHERE id_reserva=? AND tipo_movimiento='INGRESO' AND estado='ACTIVO'",
        (id_reserva,)
    )
    total_cobrado = float(df_total.iloc[0]["total"] or 0)
    saldo_actual  = float(res["venta_total"] or 0) - total_cobrado
    saldo_anterior = saldo_actual + float(mov["monto"] or 0)
    metodo = (mov.get("metodo_pago") or
              next(iter(_re_pdf.findall(r'\[([^\]]+)\]', str(mov.get("concepto") or ""))), "Desconocido"))
    folio = f"AGP-{str(mov['fecha_pago'])[:4]}-{int(id_mov):04d}"
    token = obtener_token_portal(id_reserva)
    portal_url = str(request.base_url) + f"portal/{token}" if token else None
    pdf = _gen_recibo(
        id_movimiento=id_mov,
        nombre_cliente=res["nombre"],
        telefono_cliente=res["telefono"],
        email_cliente=res.get("email", ""),
        monto=float(mov["monto"]),
        moneda=mov["moneda"],
        fecha_pago=str(mov["fecha_pago"]),
        metodo_pago=metodo,
        concepto=mov["concepto"],
        saldo_anterior=saldo_anterior,
        saldo_actual=saldo_actual,
        operador=mov.get("usuario_creador") or usuario_activo(request),
        cuenta_destino=mov.get("cuenta_destino"),
        id_reserva=id_reserva,
        destino=res["destino"],
        logo_path=LOGO_PATH,
        portal_url=portal_url,
        copia=copia,
    )
    sufijo = "" if copia == "cliente" else "-interno"
    return FastAPIResponse(content=pdf, media_type="application/pdf",
        headers={"Content-Disposition": f"inline; filename={folio}{sufijo}.pdf"})


# ─── Custodia del dinero: en qué cuenta/persona quedó cada cobro o de dónde
# salió cada pago ───────────────────────────────────────────────────────────
OPERADORAS = ["Caja Agente 1", "Caja Agente 2", "Caja Agente 3"]
CUENTAS_DESTINO = ["Banco (Cuenta 1)", "Banco (Cuenta 2)"] + OPERADORAS

# Con Tarjeta solo tiene sentido preguntar la terminal (Cuenta 1 o Cuenta 2); con
# Efectivo o Transferencia se puede elegir cualquiera de las 5.
CUENTAS_POR_METODO = {
    "Tarjeta de Crédito/Débito": ["Banco (Cuenta 1)", "Banco (Cuenta 2)"],
    "Efectivo": CUENTAS_DESTINO,
    "Transferencia": CUENTAS_DESTINO,
}
# Métodos menos comunes (Cheque, Depósito bancario, crédito de aerolínea...) muestran las 5.
CUENTAS_SIMPLE_DEFAULT = CUENTAS_POR_METODO["Transferencia"]


def _resolver_cuenta_destino(cuenta_simple, metodo_pago):
    """Valida que la cuenta elegida en el formulario sea una de las 5 reales.
    Ya no depende de metodo_pago para nada (se deja el parámetro para no tener que
    tocar las llamadas existentes) — cada persona/cuenta es un solo bucket sin
    importar cómo le llegó el dinero."""
    cuenta_simple = (cuenta_simple or "").strip()
    if cuenta_simple in CUENTAS_DESTINO:
        return cuenta_simple
    return None


# ─── Pagos a Proveedores (HTMX) ───────────────────────────────────────────────

CATEGORIAS_PAGO_PROV = [
    ("Pago de Paquete (Mayorista)",       "📦 Paquete (Mayorista)"),
    ("Pago de Vuelo (Proveedor)",         "✈️  Vuelo"),
    ("Pago de TUA (Impuesto)",                        "⛽ TUA"),
    ("Pago de Hotel (Proveedor)",          "🏨 Hotel"),
    ("Pago de Traslado (Proveedor)",       "🚐 Traslados"),
    ("Pago de Tours (Proveedor)",          "🗺️  Tours"),
    ("Pago de Adicionales (Proveedor)",    "➕ Adicionales"),
    ("Pago de Ajustes/Cambios (Proveedor)","🔄 Ajustes / Cambios"),
]

# Categorías con costo registrado en `reservas` — permiten comparar pago vs. costo
# y ofrecer el ajuste automático de "pago final". Ajustes/Cambios no mapea a una
# columna de costo propia, se excluye a propósito (igual que en el_sistema_legado).
_CAT_TO_COL_PROV = {
    "Pago de Vuelo (Proveedor)":      "costo_vuelos",
    "Pago de TUA (Impuesto)":                    "costo_tua",
    "Pago de Hotel (Proveedor)":      "costo_hotel",
    "Pago de Traslado (Proveedor)":   "costo_traslados",
    "Pago de Tours (Proveedor)":      "costo_tours",
    "Pago de Adicionales (Proveedor)":"costo_adicionales",
}

def _pago_prov_preview_ctx(id_reserva, categoria, monto):
    """Calcula costo registrado / ya pagado / total tras este pago para la
    categoría dada, y determina qué aviso mostrar (doble pago, supera el costo,
    o es menor al costo).

    "Pago de Paquete (Mayorista)" es un caso especial: no tiene una columna de
    costo propia (costo_X), el costo del paquete completo es costo_total menos
    costo_comisiones (igual que en el_sistema_legado), y solo aplica si la reserva es
    es_paquete_global=1 — si no lo es, no hay nada que comparar (sin candado)."""
    col_costo = _CAT_TO_COL_PROV.get(categoria)
    ctx = {"id_reserva": id_reserva, "categoria": categoria, "monto": monto,
           "col_costo": col_costo, "aviso": None,
           "costo_reg": 0.0, "total_ya": 0.0, "total_tras": 0.0,
           "saldo_credito_aerolinea": _saldo_credito_aerolinea_total() if categoria == "Pago de Vuelo (Proveedor)" else None}
    if categoria == "Pago de Paquete (Mayorista)":
        df_pkg = obtener_datos("SELECT costo_total, costo_comisiones, es_paquete_global FROM reservas WHERE id_reserva=?", (id_reserva,))
        if df_pkg.empty or int(df_pkg.iloc[0]["es_paquete_global"] or 0) != 1:
            return ctx
        costo_reg = round(float(df_pkg.iloc[0]["costo_total"] or 0) - float(df_pkg.iloc[0]["costo_comisiones"] or 0), 2)
    elif not col_costo:
        return ctx
    else:
        df_r = obtener_datos(f"SELECT {col_costo} as c FROM reservas WHERE id_reserva=?", (id_reserva,))
        costo_reg = float(df_r.iloc[0]["c"] or 0) if not df_r.empty else 0.0
    df_pag = obtener_datos(
        "SELECT COALESCE(SUM(monto),0) as total FROM flujo_caja "
        "WHERE id_reserva=? AND categoria=? AND tipo_movimiento='EGRESO' AND estado='ACTIVO'",
        (id_reserva, categoria)
    )
    total_ya = float(df_pag.iloc[0]["total"]) if not df_pag.empty else 0.0
    total_tras = total_ya + monto
    aviso = None
    if costo_reg > 0:
        if total_ya >= costo_reg - 0.01:
            aviso = "doble"
        elif monto > 0 and total_tras > costo_reg + 0.01:
            aviso = "mayor"
        elif monto > 0 and total_tras < costo_reg - 0.01:
            aviso = "menor"
    ctx.update({"costo_reg": costo_reg, "total_ya": total_ya, "total_tras": total_tras, "aviso": aviso})
    return ctx


@app.post("/bitacora/{id_reserva}/pagos-proveedor/preview", response_class=HTMLResponse)
async def preview_pago_proveedor(request: Request, id_reserva: int):
    if not usuario_activo(request):
        return HTMLResponse("", status_code=401)
    form = await request.form()
    categoria = (form.get("categoria") or "").strip()
    try:
        monto = float(form.get("monto") or 0)
    except Exception:
        monto = 0.0
    ctx = _pago_prov_preview_ctx(id_reserva, categoria, monto)
    return templates.TemplateResponse(request, "pago_prov_preview.html", ctx)

def _pago_prov_costos_map(id_reserva):
    """Mapa categoria -> {costo_reg, total_ya} para todas las categorías de pago a
    proveedor, para que el formulario pueda avisar de un monto que no coincide con
    el costo SIN depender del round-trip async de /pagos-proveedor/preview (ese
    debounce de 400ms deja una ventana en la que Enter dispara el submit antes de
    que el aviso/checkbox "pago final" se alcance a renderizar)."""
    mapa = {}
    for categoria in list(_CAT_TO_COL_PROV.keys()) + ["Pago de Paquete (Mayorista)"]:
        ctx = _pago_prov_preview_ctx(id_reserva, categoria, 0.0)
        mapa[categoria] = {"costo_reg": ctx["costo_reg"], "total_ya": ctx["total_ya"]}
    return mapa

def _pagos_prov_ctx(id_reserva):
    df_r = obtener_datos(
        "SELECT r.moneda, r.id_grupo, g.nombre_grupo FROM reservas r "
        "LEFT JOIN grupos_viaje g ON r.id_grupo = g.id_grupo WHERE r.id_reserva=?", (id_reserva,)
    )
    moneda = df_r.iloc[0]["moneda"] if not df_r.empty else "MXN"
    es_de_grupo = bool(not df_r.empty and pd.notna(df_r.iloc[0]["id_grupo"]))
    nombre_grupo = df_r.iloc[0]["nombre_grupo"] if es_de_grupo else None
    id_grupo_reserva = int(df_r.iloc[0]["id_grupo"]) if es_de_grupo else None
    df = obtener_datos(
        "SELECT id_movimiento, categoria, concepto, monto, moneda, fecha_pago, metodo_pago, cuenta_destino, estado "
        "FROM flujo_caja WHERE id_reserva=? AND tipo_movimiento='EGRESO' "
        "AND tipo_egreso='COSTO DIRECTO VIAJE' "
        "AND categoria NOT IN ('Comisiones Bancarias','Deuda Incobrable','Papelería y Otros') "
        "ORDER BY fecha_pago DESC, id_movimiento DESC",
        (id_reserva,)
    )
    return {
        "id_reserva": id_reserva,
        "moneda": moneda,
        "pagos_prov_lista": df.to_dict("records") if not df.empty else [],
        "categorias_pago_prov": CATEGORIAS_PAGO_PROV,
        "cuentas_destino": CUENTAS_SIMPLE_DEFAULT,
        "cuentas_por_metodo": CUENTAS_POR_METODO,
        "today": str(now_local().date()),
        # El semáforo/registro de pago a proveedor individual se desactiva para reservas
        # de grupo — ese control vive a nivel de grupo (evita alertas falsas duplicadas
        # y pagos dobles si alguien paga aquí Y en /grupos/{id}).
        "semaforo_prov": [] if es_de_grupo else _build_semaforo(id_reserva),
        "costos_prov_map": {} if es_de_grupo else _pago_prov_costos_map(id_reserva),
        "es_de_grupo": es_de_grupo,
        "nombre_grupo": nombre_grupo,
        "id_grupo": id_grupo_reserva,
    }

def _build_semaforo(id_reserva):
    """Recalcula el semáforo de proveedores para OOB HTMX."""
    df_r = obtener_datos(
        "SELECT costo_vuelos, costo_tua, costo_hotel, costo_traslados, "
        "costo_tours, costo_adicionales, costo_total, es_paquete_global FROM reservas WHERE id_reserva=?",
        (id_reserva,)
    )
    if df_r.empty:
        return []
    r = df_r.iloc[0].to_dict()
    df_eg = obtener_datos(
        "SELECT categoria, COALESCE(SUM(monto),0) as pagado FROM flujo_caja "
        "WHERE tipo_movimiento='EGRESO' AND estado='ACTIVO' AND id_reserva=? GROUP BY categoria",
        (id_reserva,)
    )
    pagos = {x["categoria"]: float(x["pagado"] or 0) for _, x in df_eg.iterrows()} if not df_eg.empty else {}
    _cc = pagos.get("Comisiones Bancarias", 0.0)
    if int(r.get("es_paquete_global", 0) or 0) == 1:
        cp = max(0.0, float(r.get("costo_total", 0) or 0) - _cc)
        pp = sum(v for k, v in pagos.items() if k != "Comisiones Bancarias")
        return [{"label": "📦 Paquete Global", "costo": cp, "pagado": pp,
                 "estado": "ok" if pp >= cp - 0.01 else "pendiente"}]
    _pag_adi = (pagos.get("Pago de Adicionales (Proveedor)", 0)
                + pagos.get("Pago de Ajustes/Cambios (Proveedor)", 0)
                + pagos.get("Papelería y Otros", 0))
    def _st(c, p):
        c, p = float(c or 0), float(p or 0)
        return ("na" if c <= 0 else ("ok" if p >= c - 0.01 else "pendiente")), c, p
    semaforo = []
    for lbl, col, pag in [
        ("✈️ Vuelo",      "costo_vuelos",     pagos.get("Pago de Vuelo (Proveedor)", 0)),
        ("⛽ TUA",         "costo_tua",        pagos.get("Pago de TUA (Impuesto)", 0)),
        ("🏨 Hotel",       "costo_hotel",      pagos.get("Pago de Hotel (Proveedor)", 0)),
        ("🚐 Traslados",   "costo_traslados",  pagos.get("Pago de Traslado (Proveedor)", 0)),
        ("🗺️ Tours",      "costo_tours",      pagos.get("Pago de Tours (Proveedor)", 0)),
        ("➕ Adicionales", "costo_adicionales", _pag_adi),
    ]:
        est, c, p = _st(r.get(col, 0), pag)
        semaforo.append({"label": lbl, "costo": c, "pagado": p, "estado": est})
    return semaforo

def _saldo_credito_aerolinea_total():
    """Saldo disponible de crédito con la aerolínea — snapshot en vivo, no depende de
    ningún itinerario ni período. Vive enteramente en flujo_caja, sin tabla propia:
    entra como INGRESO 'Crédito Aerolínea Generado' (id_reserva NULL) y sale como
    EGRESO normal de 'Pago de Vuelo (Proveedor)' cuando metodo_pago='Crédito Aerolínea'
    — al anular ese pago, el saldo se restaura solo porque se recalcula sobre
    estado='ACTIVO'. 'Ajuste Crédito Aerolínea' (INGRESO o EGRESO) es el ajuste manual
    — para cargar el saldo real inicial o corregir por expiración, ver
    _credito_aerolinea_ajustar_manual()."""
    df_in = obtener_datos(
        "SELECT COALESCE(SUM(monto),0) as t FROM flujo_caja "
        "WHERE tipo_movimiento='INGRESO' AND categoria IN ('Crédito Aerolínea Generado','Ajuste Crédito Aerolínea') "
        "AND estado='ACTIVO'"
    )
    df_out = obtener_datos(
        "SELECT COALESCE(SUM(monto),0) as t FROM flujo_caja "
        "WHERE tipo_movimiento='EGRESO' AND estado='ACTIVO' AND ("
        "(categoria='Pago de Vuelo (Proveedor)' AND metodo_pago='Crédito Aerolínea') "
        "OR categoria='Ajuste Crédito Aerolínea')"
    )
    generado = float(df_in.iloc[0]["t"]) if not df_in.empty else 0.0
    aplicado = float(df_out.iloc[0]["t"]) if not df_out.empty else 0.0
    return round(generado - aplicado, 2)


def _credito_aerolinea_ajustar_manual(tipo, monto, motivo, usuario):
    """Ajuste manual del saldo de crédito con la aerolínea — abierto a cualquier
    usuario logueado, siempre requiere motivo, y queda registrado como movimiento
    normal en Libro Diario con su propia categoría ('Ajuste Crédito Aerolínea'), nunca
    se edita el saldo en silencio."""
    hoy = str(now_local().date())
    concepto = f"[Ajuste manual crédito aerolínea] {motivo}"
    tipo_mov = "INGRESO" if tipo == "sumar" else "EGRESO"
    id_mov = ejecutar_insert(
        "INSERT INTO flujo_caja (id_reserva, tipo_movimiento, categoria, concepto, "
        "monto, moneda, fecha_pago, usuario_creador, fecha_creacion) "
        f"VALUES (NULL, '{tipo_mov}', 'Ajuste Crédito Aerolínea', ?, ?, 'MXN', ?, ?, ?)",
        (concepto, monto, hoy, usuario, hoy)
    )
    return id_mov


def _credito_aerolinea_registrar_generado(id_reserva_origen, motivo, monto, usuario):
    """Registra crédito de aerolínea generado (cancelación o cambio de vuelo) como un
    INGRESO normal de flujo_caja — id_reserva=NULL para no inflar cobrado_cliente/
    venta_total del itinerario de origen (mismo criterio que Ingresos de oficina)."""
    hoy = str(now_local().date())
    concepto = f"[Crédito Aerolínea] Itin #{id_reserva_origen} — {motivo}"
    id_mov = ejecutar_insert(
        "INSERT INTO flujo_caja (id_reserva, tipo_movimiento, categoria, concepto, "
        "monto, moneda, fecha_pago, usuario_creador, fecha_creacion) "
        "VALUES (NULL, 'INGRESO', 'Crédito Aerolínea Generado', ?, ?, 'MXN', ?, ?, ?)",
        (concepto, monto, hoy, usuario, hoy)
    )
    if id_mov:
        registrar_cambio(id_reserva_origen, "CRÉDITO AEROLÍNEA GENERADO",
                          f"${monto:,.2f} MXN — {motivo}", usuario=usuario)
    return id_mov


def _pagos_prov_response(id_reserva):
    """Devuelve la sección HTMX + OOB del semáforo."""
    ctx_prov = _pagos_prov_ctx(id_reserva)          # ya incluye semaforo_prov
    seccion = _jinja_env.get_template("pagos_proveedor_section.html").render(**ctx_prov)
    oob = _jinja_env.get_template("semaforo_items_oob.html").render(semaforo=ctx_prov["semaforo_prov"])
    return HTMLResponse(seccion + "\n" + oob)


@app.post("/bitacora/{id_reserva}/pagos-proveedor/registrar", response_class=HTMLResponse)
async def registrar_pago_proveedor(request: Request, id_reserva: int):
    if not usuario_activo(request):
        return HTMLResponse("", status_code=401)
    form    = await request.form()
    usuario = usuario_activo(request)
    hoy     = str(now_local().date())

    categoria   = (form.get("categoria") or "").strip()
    concepto    = (form.get("concepto") or categoria).strip()
    fecha       = (form.get("fecha_pago") or hoy).strip()
    metodo      = (form.get("metodo_pago") or "Transferencia").strip()
    pago_final  = form.get("pago_final") == "1"
    cuenta_destino = _resolver_cuenta_destino(form.get("cuenta_destino"), metodo)
    try:
        monto = float(form.get("monto") or 0)
    except Exception:
        monto = 0.0

    df_r = obtener_datos("SELECT moneda FROM reservas WHERE id_reserva=?", (id_reserva,))
    moneda = df_r.iloc[0]["moneda"] if not df_r.empty else "MXN"

    # Bloqueo real de pago doble a proveedor — se recalcula server-side (el preview
    # de /pagos-proveedor/preview es solo informativo en la UI, no basta como candado).
    _ctx_doble = _pago_prov_preview_ctx(id_reserva, categoria, monto) if categoria else {"aviso": None}
    if _ctx_doble.get("aviso") == "doble":
        return _pagos_prov_response(id_reserva)

    # El crédito de aerolínea solo aplica a Vuelo, y solo hasta el saldo disponible —
    # igual que el candado de doble pago, se recalcula server-side (el preview es
    # solo informativo).
    if metodo == "Crédito Aerolínea":
        if categoria != "Pago de Vuelo (Proveedor)" or monto > _saldo_credito_aerolinea_total():
            return _pagos_prov_response(id_reserva)

    if monto > 0 and categoria:
        id_mov_nuevo = ejecutar_insert(
            "INSERT INTO flujo_caja (id_reserva, tipo_movimiento, tipo_egreso, categoria, concepto, "
            "monto, moneda, fecha_pago, metodo_pago, cuenta_destino, estado, usuario_creador, fecha_creacion) "
            "VALUES (?, 'EGRESO', 'COSTO DIRECTO VIAJE', ?, ?, ?, ?, ?, ?, ?, 'ACTIVO', ?, ?)",
            (id_reserva, categoria, concepto, monto, moneda, fecha, metodo, cuenta_destino, usuario, hoy)
        )
        if id_mov_nuevo:
            registrar_cambio(id_reserva, "PAGO PROVEEDOR",
                             f"{categoria} — ${monto:,.2f} {moneda} vía {metodo}", usuario=usuario)

            col_costo = _CAT_TO_COL_PROV.get(categoria)
            if pago_final and col_costo:
                df_c = obtener_datos(f"SELECT {col_costo} as c FROM reservas WHERE id_reserva=?", (id_reserva,))
                costo_anterior = round(float(df_c.iloc[0]["c"] or 0), 2) if not df_c.empty else 0.0
                df_pag = obtener_datos(
                    "SELECT COALESCE(SUM(monto),0) as total FROM flujo_caja "
                    "WHERE id_reserva=? AND categoria=? AND tipo_movimiento='EGRESO' AND estado='ACTIVO'",
                    (id_reserva, categoria)
                )
                total_pagado = round(float(df_pag.iloc[0]["total"]) if not df_pag.empty else 0.0, 2)
                delta = round(total_pagado - costo_anterior, 2)
                # Se guarda en el propio movimiento qué columna de costo ajustó y con qué valores,
                # para poder revertir el ajuste con precisión si se anula después.
                ejecutar_transaccion([
                    (f"UPDATE reservas SET {col_costo} = ?, costo_total = ROUND(costo_total + ?, 2) WHERE id_reserva=?",
                     (total_pagado, delta, id_reserva)),
                    ("UPDATE flujo_caja SET ajuste_costo_columna=?, ajuste_costo_anterior=?, ajuste_costo_nuevo=? WHERE id_movimiento=?",
                     (col_costo, costo_anterior, total_pagado, id_mov_nuevo)),
                ])
                registrar_cambio(id_reserva, "COSTOS ACTUALIZADOS",
                    f"Pago final de {categoria} — costo ajustado de ${costo_anterior:,.2f} a ${total_pagado:,.2f}",
                    usuario=usuario)
    return _pagos_prov_response(id_reserva)


@app.post("/bitacora/{id_reserva}/pagos-proveedor/{id_mov}/anular", response_class=HTMLResponse)
async def anular_pago_proveedor(request: Request, id_reserva: int, id_mov: int):
    if not usuario_activo(request):
        return HTMLResponse("", status_code=401)
    usuario = usuario_activo(request)
    ejecutar_comando(
        "UPDATE flujo_caja SET estado='CANCELADO' "
        "WHERE id_movimiento=? AND tipo_egreso='COSTO DIRECTO VIAJE' AND id_reserva=?",
        (id_mov, id_reserva)
    )
    # Si este pago había sido marcado "pago final" (ajustó costo_X/costo_total),
    # revertir el ajuste — solo si nadie más tocó esa columna desde entonces.
    df_ajuste = obtener_datos(
        "SELECT ajuste_costo_columna, ajuste_costo_anterior, ajuste_costo_nuevo FROM flujo_caja WHERE id_movimiento=?",
        (id_mov,)
    )
    if not df_ajuste.empty and df_ajuste["ajuste_costo_columna"].iloc[0]:
        col_rev = df_ajuste["ajuste_costo_columna"].iloc[0]
        val_ant = round(float(df_ajuste["ajuste_costo_anterior"].iloc[0]), 2)
        val_nuevo = round(float(df_ajuste["ajuste_costo_nuevo"].iloc[0]), 2)
        if col_rev in {"costo_vuelos", "costo_tua", "costo_hotel", "costo_traslados", "costo_tours", "costo_adicionales"}:
            df_actual = obtener_datos(f"SELECT {col_rev} FROM reservas WHERE id_reserva=?", (id_reserva,))
            valor_actual = round(float(df_actual[col_rev].iloc[0]), 2) if not df_actual.empty else None
            if valor_actual is not None and abs(valor_actual - val_nuevo) < 0.01:
                delta = round(val_nuevo - val_ant, 2)
                ejecutar_comando(
                    f"UPDATE reservas SET {col_rev} = ?, costo_total = ROUND(costo_total - ?, 2) WHERE id_reserva=?",
                    (val_ant, delta, id_reserva)
                )
                registrar_cambio(id_reserva, "COSTOS ACTUALIZADOS",
                    f"Revertido por anulación del pago #{id_mov}: {col_rev} regresó de ${val_nuevo:,.2f} a ${val_ant:,.2f}",
                    usuario=usuario)
    registrar_cambio(id_reserva, "PAGO PROVEEDOR", f"Pago #{id_mov} anulado", usuario=usuario)
    return _pagos_prov_response(id_reserva)


@app.post("/bitacora/{id_reserva}/credito-aerolinea/generar", response_class=HTMLResponse)
async def credito_aerolinea_generar(request: Request, id_reserva: int):
    """Registra crédito de aerolínea generado por un cambio de itinerario que NO llega
    a cancelar la reserva (para cancelación, ver el campo dedicado en cancelar_reserva)
    — vive en Pagos a Proveedores porque aplica sin importar si la reserva se cancela
    o no."""
    if not usuario_activo(request):
        return HTMLResponse("", status_code=401)
    form = await request.form()
    usuario = usuario_activo(request)
    motivo = (form.get("motivo") or "Cambio de vuelo").strip()
    try:
        monto = float(form.get("monto") or 0)
    except Exception:
        monto = 0.0
    if monto > 0:
        _credito_aerolinea_registrar_generado(id_reserva, motivo, monto, usuario)
    return _pagos_prov_response(id_reserva)


# ─── Extras / Suplementos (HTMX) ─────────────────────────────────────────────

LOGO_PATH = "assets/logos/logo.png"

def _extras_html(request, id_reserva):
    df_r   = obtener_datos("SELECT moneda, estado FROM reservas WHERE id_reserva=?", (id_reserva,))
    moneda = df_r.iloc[0]["moneda"] if not df_r.empty else "MXN"
    reserva_activa = (df_r.iloc[0]["estado"] == "ACTIVO") if not df_r.empty else False
    df_ex  = obtener_datos(
        "SELECT id_extra, descripcion, monto_cobrado_cliente, monto_costo_proveedor, "
        "metodo_pago, fecha_registro, tipo_registro "
        "FROM extras_viaje WHERE id_reserva=? ORDER BY fecha_registro DESC",
        (id_reserva,)
    )
    df_pend = obtener_datos(
        "SELECT COUNT(*) as n FROM plan_pagos WHERE id_reserva=? AND estado IN ('PENDIENTE','PARCIAL')",
        (id_reserva,)
    )
    hay_pend = int(df_pend.iloc[0]["n"]) > 0 if not df_pend.empty else False
    df_total_plan = obtener_datos(
        "SELECT COUNT(*) as n FROM plan_pagos WHERE id_reserva=?",
        (id_reserva,)
    )
    modo_libre = int(df_total_plan.iloc[0]["n"]) == 0 if not df_total_plan.empty else True
    return templates.TemplateResponse(request, "extras_section.html", {
        "request": request,
        "id_reserva": id_reserva,
        "moneda": moneda,
        "extras": df_ex.to_dict("records"),
        "hay_parcialidades_pendientes": hay_pend,
        "modo_libre": modo_libre,
        "puede_diferir_plan": hay_pend or modo_libre,
        "today": str(now_local().date()),
        "reserva_activa": reserva_activa,
    })


@app.post("/bitacora/{id_reserva}/extras/agregar", response_class=HTMLResponse)
async def agregar_extra(request: Request, id_reserva: int):
    if not usuario_activo(request):
        return HTMLResponse("", status_code=401)
    form     = await request.form()
    usuario  = usuario_activo(request)
    desc     = (form.get("descripcion") or "").strip()
    if not desc:
        return _extras_html(request, id_reserva)

    df_estado = obtener_datos("SELECT estado, moneda FROM reservas WHERE id_reserva=?", (id_reserva,))
    if df_estado.empty or df_estado.iloc[0]["estado"] != "ACTIVO":
        # No se pueden agregar cargos nuevos a una reserva Cancelada/Incobrable.
        return _extras_html(request, id_reserva)

    def _f(k, d=0.0):
        try: return float(form.get(k) or d)
        except: return d

    cobro    = round(_f("monto_cobrado_cliente"), 2)
    costo    = _f("monto_costo_proveedor")
    fecha    = form.get("fecha_registro") or str(now_local().date())
    tipo_reg = form.get("tipo_registro") or "PAGADO_MOMENTO"
    metodo   = form.get("metodo_pago") or "Efectivo"
    pct_com  = _f("comision_pct")

    moneda = df_estado.iloc[0]["moneda"]

    if tipo_reg == "PLAN_PAGOS":
        df_pend = obtener_datos(
            "SELECT id_pago FROM plan_pagos WHERE id_reserva=? AND estado IN ('PENDIENTE','PARCIAL')", (id_reserva,)
        )
        if df_pend.empty:
            df_total_plan = obtener_datos(
                "SELECT id_pago, numero_pago, fecha_programada FROM plan_pagos WHERE id_reserva=? ORDER BY numero_pago DESC LIMIT 1", (id_reserva,)
            )
            if df_total_plan.empty:
                # Reserva "Libre" (sin plan de parcialidades): se suma al saldo
                # pendiente total en vez de repartir en cuotas inexistentes.
                ejecutar_comando(
                    "UPDATE reservas SET venta_total = ROUND(venta_total + ?, 2) WHERE id_reserva = ?",
                    (cobro, id_reserva)
                )
                ejecutar_comando(
                    "INSERT INTO extras_viaje (id_reserva, descripcion, monto_cobrado_cliente, "
                    "monto_costo_proveedor, fecha_registro, tipo_registro, usuario_creador) "
                    "VALUES (?,?,?,?,?,?,?)",
                    (id_reserva, desc, cobro, costo, fecha, "ABONO_LIBRE", usuario)
                )
                registrar_cambio(id_reserva, "EXTRA", f"[Libre] {desc} — Cobro: ${cobro:,.2f}", usuario=usuario)
            # Candado: plan cerrado (todas las parcialidades ya pagadas) — no se puede diferir
            return _extras_html(request, id_reserva)
        # Se crea una parcialidad NUEVA y dedicada para el extra, en vez de repartirlo
        # entre las existentes — evita mutar monto_esperado de parcialidades ya
        # establecidas (mismo mecanismo que causó el bug de plan_pagos ya corregido).
        _num_pago_nuevo = int(df_total_plan.iloc[0]["numero_pago"]) + 1 if not df_total_plan.empty else 1
        _fecha_nueva = df_total_plan.iloc[0]["fecha_programada"] if not df_total_plan.empty else fecha
        _id_pago_nuevo = ejecutar_insert(
            "INSERT INTO plan_pagos (id_reserva, numero_pago, monto_esperado, monto_pagado, fecha_programada, estado) VALUES (?, ?, ?, 0, ?, 'PENDIENTE')",
            (id_reserva, _num_pago_nuevo, cobro, _fecha_nueva)
        )
        if _id_pago_nuevo:
            ops = [
                ("INSERT INTO extras_viaje (id_reserva, descripcion, monto_cobrado_cliente, "
                 "monto_costo_proveedor, fecha_registro, tipo_registro, usuario_creador, id_pago_plan) "
                 "VALUES (?,?,?,?,?,?,?,?)",
                 (id_reserva, desc, cobro, costo, fecha, "PLAN_PAGOS", usuario, _id_pago_nuevo)),
                ("UPDATE reservas SET venta_total = ROUND(venta_total + ?, 2) WHERE id_reserva = ?", (cobro, id_reserva)),
            ]
            # No se registra flujo_caja aquí: el dinero todavía no se ha cobrado, solo se agregó
            # como parcialidad pendiente. Insertar un ingreso inmediato lo contaría dos veces
            # cuando esa parcialidad se cobre de verdad más adelante.
            ejecutar_transaccion(ops)
            actualizar_estado_plan_pagos(id_reserva)
            registrar_cambio(id_reserva, "EXTRA", f"[Plan] {desc} — Cobro: ${cobro:,.2f}", usuario=usuario)
    else:
        _id_extra_nuevo = ejecutar_insert(
            "INSERT INTO extras_viaje (id_reserva, descripcion, monto_cobrado_cliente, "
            "monto_costo_proveedor, metodo_pago, fecha_registro, tipo_registro, usuario_creador) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (id_reserva, desc, cobro, costo, metodo, fecha, "PAGADO_MOMENTO", usuario)
        )
        if _id_extra_nuevo:
            ops = []
            if cobro > 0:
                ops.append(("UPDATE reservas SET venta_total = ROUND(venta_total + ?, 2) WHERE id_reserva = ?", (cobro, id_reserva)))
                ops.append(("INSERT INTO flujo_caja (id_reserva, tipo_movimiento, categoria, concepto, "
                            "monto, moneda, fecha_pago, usuario_creador, metodo_pago, fecha_creacion, id_extra) "
                            "VALUES (?, 'INGRESO', 'Cobro Extra al Cliente', ?, ?, ?, ?, ?, ?, ?, ?)",
                            (id_reserva, desc, cobro, moneda, fecha, usuario, metodo, str(now_local().date()), _id_extra_nuevo)))
            if metodo == "Tarjeta de Crédito/Débito" and pct_com > 0 and cobro > 0:
                comision = round(cobro * pct_com / 100, 2)
                ops.append(("INSERT INTO flujo_caja (id_reserva, tipo_movimiento, tipo_egreso, categoria, "
                            "concepto, monto, moneda, fecha_pago, fecha_creacion, id_extra) "
                            "VALUES (?, 'EGRESO', 'COSTO DIRECTO VIAJE', 'Comisiones Bancarias', ?, ?, ?, ?, ?, ?)",
                            (id_reserva, f"[Automático] Comisión Extra: {desc}", comision, moneda, fecha, str(now_local().date()), _id_extra_nuevo)))
                # Igual que en registrar_cobro: la comisión también se refleja en las
                # columnas de reservas, no solo en flujo_caja — de lo contrario
                # costo_total/utilidad_proyectada quedan congeladas e inflan la utilidad
                # mostrada.
                ops.append((
                    "UPDATE reservas SET costo_comisiones = ROUND(costo_comisiones + ?, 2), costo_total = ROUND(costo_total + ?, 2), utilidad_proyectada = ROUND(utilidad_proyectada - ?, 2) WHERE id_reserva = ?",
                    (comision, comision, comision, id_reserva)
                ))
            if ops:
                ejecutar_transaccion(ops)
            from database import sincronizar_cobrado_cliente
            sincronizar_cobrado_cliente(id_reserva)
            actualizar_estado_plan_pagos(id_reserva)
            registrar_cambio(id_reserva, "EXTRA", f"[Momento] {desc} — Cobro: ${cobro:,.2f}", usuario=usuario)
    return _extras_html(request, id_reserva)


@app.post("/bitacora/{id_reserva}/extras/{id_extra}/anular", response_class=HTMLResponse)
async def anular_extra(request: Request, id_reserva: int, id_extra: int):
    if not usuario_activo(request):
        return HTMLResponse("", status_code=401)
    form   = await request.form()
    motivo = (form.get("motivo") or "Sin motivo").strip()
    usuario = usuario_activo(request)
    # Si el extra tenía comisión bancaria automática: por defecto se revierte (error de
    # captura, no se cobró de verdad). Si el usuario marca "es una devolución", la comisión
    # NO se revierte — el banco no regresa esa comisión aunque se le devuelva el dinero al
    # cliente, así que sigue siendo un costo real.
    revertir_comision = (form.get("revertir_comision") or "1") == "1"

    df_ex = obtener_datos(
        "SELECT descripcion, monto_cobrado_cliente, tipo_registro, id_pago_plan FROM extras_viaje WHERE id_extra=?",
        (id_extra,)
    )
    if df_ex.empty:
        return _extras_html(request, id_reserva)

    desc   = df_ex.iloc[0]["descripcion"]
    cobro  = round(float(df_ex.iloc[0]["monto_cobrado_cliente"]), 2)
    tipo   = df_ex.iloc[0]["tipo_registro"]
    id_pago_plan_ex = df_ex.iloc[0]["id_pago_plan"]

    ejecutar_comando("DELETE FROM extras_viaje WHERE id_extra=?", (id_extra,))

    if cobro > 0:
        ejecutar_comando("UPDATE reservas SET venta_total = ROUND(venta_total - ?, 2) WHERE id_reserva = ?",
                         (cobro, id_reserva))

    if tipo == "PAGADO_MOMENTO" and cobro > 0:
        # Extras creados después de este fix se anulan por id_extra (preciso).
        # Extras viejos (sin ese vínculo) usan el respaldo anterior por monto.
        n_por_id = obtener_datos(
            "SELECT COUNT(*) as n FROM flujo_caja WHERE id_extra=? AND categoria='Cobro Extra al Cliente'",
            (id_extra,)
        )
        if not n_por_id.empty and int(n_por_id.iloc[0]["n"]) > 0:
            # Sin el filtro de categoría, este UPDATE cancelaba de rebote la fila de
            # "Comisiones Bancarias" del mismo id_extra ANTES de que el bloque de abajo
            # la encontrara ACTIVA — la comisión quedaba cancelada pero
            # costo_comisiones/costo_total/utilidad_proyectada nunca se revertían. Ahora
            # solo toca la fila de "Cobro Extra al Cliente".
            ejecutar_comando(
                "UPDATE flujo_caja SET estado='CANCELADO', motivo_anulacion=? WHERE id_extra=? AND categoria='Cobro Extra al Cliente'",
                (motivo, id_extra)
            )
        else:
            ejecutar_comando(
                "UPDATE flujo_caja SET estado='CANCELADO', motivo_anulacion=? "
                "WHERE id_reserva=? AND categoria='Cobro Extra al Cliente' AND ABS(monto-?)<0.01 "
                "AND estado='ACTIVO' LIMIT 1",
                (motivo, id_reserva, cobro)
            )
        # Si este extra tenía una comisión bancaria automática vinculada (pagado con
        # tarjeta), se cancela también y se revierte su reflejo en reservas — mismo
        # patrón que anular_cobro. Solo si el usuario indicó que fue error de captura —
        # si es una devolución al cliente, la comisión del banco NO se recupera y se
        # deja activa como costo real.
        df_comi_void_ex = obtener_datos(
            "SELECT id_movimiento, monto FROM flujo_caja WHERE id_extra=? AND categoria='Comisiones Bancarias' AND estado='ACTIVO'",
            (id_extra,)
        ) if revertir_comision else pd.DataFrame()
        if not df_comi_void_ex.empty:
            _id_comi_void_ex = int(df_comi_void_ex.iloc[0]["id_movimiento"])
            _monto_comi_void_ex = round(float(df_comi_void_ex.iloc[0]["monto"] or 0), 2)
            ejecutar_comando(
                "UPDATE flujo_caja SET estado='CANCELADO', motivo_anulacion=? WHERE id_movimiento=?",
                (f"Auto-cancelada: comisión del extra anulado. {motivo}", _id_comi_void_ex)
            )
            ejecutar_comando(
                "UPDATE reservas SET costo_comisiones = ROUND(costo_comisiones - ?, 2), costo_total = ROUND(costo_total - ?, 2), utilidad_proyectada = ROUND(utilidad_proyectada + ?, 2) WHERE id_reserva = ?",
                (_monto_comi_void_ex, _monto_comi_void_ex, _monto_comi_void_ex, id_reserva)
            )
            registrar_cambio(id_reserva, "COSTOS ACTUALIZADOS",
                              f"Comisión bancaria #{_id_comi_void_ex} (${_monto_comi_void_ex:,.2f}) cancelada y revertida por anulación del extra.",
                              usuario=usuario)
        from database import sincronizar_cobrado_cliente
        sincronizar_cobrado_cliente(id_reserva)
        actualizar_estado_plan_pagos(id_reserva)
    elif tipo == "PLAN_PAGOS" and cobro > 0:
        if id_pago_plan_ex is not None and not pd.isna(id_pago_plan_ex):
            # Extra creado después de este fix: tiene su propia fila dedicada
            # en plan_pagos — se borra directo, sin tocar las demás parcialidades.
            ejecutar_comando("DELETE FROM plan_pagos WHERE id_pago=?", (int(id_pago_plan_ex),))
        else:
            # Extra viejo (creado antes de este fix): respaldo con el reparto en
            # cascada anterior, único método disponible para esos registros.
            df_pend = obtener_datos(
                "SELECT id_pago FROM plan_pagos WHERE id_reserva=? AND estado IN ('PENDIENTE','PARCIAL')", (id_reserva,)
            )
            if not df_pend.empty:
                montos_reparto_an = distribuir_equitativo(cobro, len(df_pend))
                for i, (_, row) in enumerate(df_pend.iterrows()):
                    ejecutar_comando("UPDATE plan_pagos SET monto_esperado = ROUND(monto_esperado - ?, 2) WHERE id_pago=?",
                                     (montos_reparto_an[i], int(row["id_pago"])))
        actualizar_estado_plan_pagos(id_reserva)

    registrar_cambio(id_reserva, "EXTRA ANULADO", f"{desc} — Motivo: {motivo}", usuario=usuario)
    return _extras_html(request, id_reserva)


# ─── Vuelos y Hoteles por tramo (HTMX) ───────────────────────────────────────
# Puramente logístico: el dinero sigue siendo un solo total en
# cobro_vuelos/costo_vuelos y cobro_hotel/costo_hotel sin importar cuántas
# filas haya aquí (decisión explícita de Carlos, 2026-09-03).

def _vuelos_html(request, id_reserva):
    df_r = obtener_datos("SELECT tipo_vuelo, estado FROM reservas WHERE id_reserva=?", (id_reserva,))
    tipo_vuelo = df_r.iloc[0]["tipo_vuelo"] if not df_r.empty else "REDONDO"
    reserva_activa = (df_r.iloc[0]["estado"] == "ACTIVO") if not df_r.empty else False
    df_v = obtener_datos(
        "SELECT id_vuelo, numero_tramo, aerolinea, numero_vuelo, origen, destino, fecha, hora, localizador, checkin "
        "FROM vuelos_reserva WHERE id_reserva=? ORDER BY numero_tramo, id_vuelo",
        (id_reserva,)
    )
    return templates.TemplateResponse(request, "vuelos_section.html", {
        "request": request,
        "id_reserva": id_reserva,
        "tipo_vuelo": tipo_vuelo,
        "vuelos": df_v.to_dict("records"),
        "reserva_activa": reserva_activa,
    })


def _habitaciones_por_hotel(tabla_hab: str, col_fk: str, id_val: int):
    """Agrupa habitaciones_reserva/habitaciones_cotizacion por id_hotel_itin."""
    df = obtener_datos(
        f"SELECT id_habitacion, id_hotel_itin, tipo_habitacion, num_personas, hora_checkin, descripcion "
        f"FROM {tabla_hab} WHERE {col_fk}=? AND id_hotel_itin IS NOT NULL ORDER BY id_habitacion",
        (id_val,)
    )
    agrupado = {}
    for r in df.to_dict("records"):
        agrupado.setdefault(int(r["id_hotel_itin"]), []).append(r)
    return agrupado


def _hoteles_html(request, id_reserva):
    df_r = obtener_datos("SELECT estado FROM reservas WHERE id_reserva=?", (id_reserva,))
    reserva_activa = (df_r.iloc[0]["estado"] == "ACTIVO") if not df_r.empty else False
    df_h = obtener_datos(
        "SELECT id_hotel_itin, numero_orden, ciudad_destino, nombre_hotel, localizador, fecha_checkin, fecha_checkout "
        "FROM hoteles_reserva WHERE id_reserva=? ORDER BY numero_orden, id_hotel_itin",
        (id_reserva,)
    )
    return templates.TemplateResponse(request, "hoteles_section.html", {
        "request": request,
        "id_reserva": id_reserva,
        "hoteles": df_h.to_dict("records"),
        "habitaciones_por_hotel": _habitaciones_por_hotel("habitaciones_reserva", "id_reserva", id_reserva),
        "reserva_activa": reserva_activa,
    })


@app.post("/bitacora/{id_reserva}/tipo_vuelo", response_class=HTMLResponse)
async def cambiar_tipo_vuelo(request: Request, id_reserva: int):
    if not usuario_activo(request):
        return HTMLResponse("", status_code=401)
    form = await request.form()
    tipo = form.get("tipo_vuelo") or "REDONDO"
    if tipo not in ("SENCILLO", "REDONDO"):
        tipo = "REDONDO"
    ejecutar_comando("UPDATE reservas SET tipo_vuelo=? WHERE id_reserva=?", (tipo, id_reserva))
    return _vuelos_html(request, id_reserva)


@app.post("/bitacora/{id_reserva}/vuelos/agregar", response_class=HTMLResponse)
async def agregar_vuelo(request: Request, id_reserva: int):
    if not usuario_activo(request):
        return HTMLResponse("", status_code=401)
    form = await request.form()
    aerolinea = (form.get("aerolinea") or "").strip()
    if not aerolinea:
        return _vuelos_html(request, id_reserva)
    df_n = obtener_datos("SELECT COALESCE(MAX(numero_tramo),0) as n FROM vuelos_reserva WHERE id_reserva=?", (id_reserva,))
    num_tramo = int(df_n.iloc[0]["n"] or 0) + 1
    ejecutar_comando(
        "INSERT INTO vuelos_reserva (id_reserva, numero_tramo, aerolinea, numero_vuelo, origen, destino, fecha, hora, localizador) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        (id_reserva, num_tramo, aerolinea, (form.get("numero_vuelo") or "").strip(),
         (form.get("origen") or "").strip(), (form.get("destino") or "").strip(),
         form.get("fecha") or None, form.get("hora") or None, (form.get("localizador") or "").strip())
    )
    registrar_cambio(id_reserva, "VUELO", f"Tramo #{num_tramo} agregado: {aerolinea} {form.get('origen') or ''}→{form.get('destino') or ''}", usuario=usuario_activo(request))
    return _vuelos_html(request, id_reserva)


@app.post("/bitacora/{id_reserva}/vuelos/{id_vuelo}/eliminar", response_class=HTMLResponse)
async def eliminar_vuelo(request: Request, id_reserva: int, id_vuelo: int):
    if not usuario_activo(request):
        return HTMLResponse("", status_code=401)
    ejecutar_comando("DELETE FROM vuelos_reserva WHERE id_vuelo=? AND id_reserva=?", (id_vuelo, id_reserva))
    registrar_cambio(id_reserva, "VUELO ELIMINADO", f"Tramo id {id_vuelo}", usuario=usuario_activo(request))
    return _vuelos_html(request, id_reserva)


@app.post("/bitacora/{id_reserva}/vuelos/{id_vuelo}/checkin", response_class=HTMLResponse)
async def toggle_checkin_vuelo(request: Request, id_reserva: int, id_vuelo: int):
    if not usuario_activo(request):
        return HTMLResponse("", status_code=401)
    df_v = obtener_datos("SELECT checkin, numero_tramo FROM vuelos_reserva WHERE id_vuelo=? AND id_reserva=?", (id_vuelo, id_reserva))
    if not df_v.empty:
        nuevo = 0 if int(df_v.iloc[0]["checkin"] or 0) == 1 else 1
        ejecutar_comando("UPDATE vuelos_reserva SET checkin=? WHERE id_vuelo=?", (nuevo, id_vuelo))
        registrar_cambio(id_reserva, "CHECK-IN VUELO", f"Tramo #{df_v.iloc[0]['numero_tramo']} — {'confirmado' if nuevo else 'revertido'}", usuario=usuario_activo(request))
    # Desde Torre de Control (no es el fragmento #seccion-vuelos) manda recargar
    # la página completa en vez de devolver el partial, que solo tiene sentido
    # dentro del detalle del itinerario.
    if request.headers.get("HX-Target") != "seccion-vuelos":
        return HTMLResponse("", headers={"HX-Redirect": "/dashboard"})
    return _vuelos_html(request, id_reserva)


@app.post("/bitacora/{id_reserva}/hoteles/agregar", response_class=HTMLResponse)
async def agregar_hotel_itin(request: Request, id_reserva: int):
    if not usuario_activo(request):
        return HTMLResponse("", status_code=401)
    form = await request.form()
    nombre = (form.get("nombre_hotel") or "").strip()
    if not nombre:
        return _hoteles_html(request, id_reserva)
    df_n = obtener_datos("SELECT COALESCE(MAX(numero_orden),0) as n FROM hoteles_reserva WHERE id_reserva=?", (id_reserva,))
    num_orden = int(df_n.iloc[0]["n"] or 0) + 1
    ejecutar_comando(
        "INSERT INTO hoteles_reserva (id_reserva, numero_orden, ciudad_destino, nombre_hotel, localizador, fecha_checkin, fecha_checkout) "
        "VALUES (?,?,?,?,?,?,?)",
        (id_reserva, num_orden, (form.get("ciudad_destino") or "").strip(), nombre,
         (form.get("localizador") or "").strip(), form.get("fecha_checkin") or None, form.get("fecha_checkout") or None)
    )
    registrar_cambio(id_reserva, "HOTEL", f"Hotel #{num_orden} agregado: {nombre} ({form.get('ciudad_destino') or ''})", usuario=usuario_activo(request))
    return _hoteles_html(request, id_reserva)


@app.post("/bitacora/{id_reserva}/hoteles/{id_hotel_itin}/eliminar", response_class=HTMLResponse)
async def eliminar_hotel_itin(request: Request, id_reserva: int, id_hotel_itin: int):
    if not usuario_activo(request):
        return HTMLResponse("", status_code=401)
    ejecutar_comando("DELETE FROM hoteles_reserva WHERE id_hotel_itin=? AND id_reserva=?", (id_hotel_itin, id_reserva))
    ejecutar_comando("DELETE FROM habitaciones_reserva WHERE id_hotel_itin=?", (id_hotel_itin,))
    registrar_cambio(id_reserva, "HOTEL ELIMINADO", f"Hotel id {id_hotel_itin}", usuario=usuario_activo(request))
    return _hoteles_html(request, id_reserva)


@app.post("/bitacora/{id_reserva}/hoteles/{id_hotel_itin}/habitaciones/agregar", response_class=HTMLResponse)
async def agregar_habitacion_hotel(request: Request, id_reserva: int, id_hotel_itin: int):
    if not usuario_activo(request):
        return HTMLResponse("", status_code=401)
    form = await request.form()
    tipo = (form.get("tipo_habitacion") or "").strip()
    if not tipo:
        return _hoteles_html(request, id_reserva)
    try: personas = int(form.get("num_personas") or 1)
    except Exception: personas = 1
    ejecutar_comando(
        "INSERT INTO habitaciones_reserva (id_reserva, id_hotel_itin, tipo_habitacion, num_personas, hora_checkin, descripcion) VALUES (?,?,?,?,?,?)",
        (id_reserva, id_hotel_itin, tipo, personas,
         (form.get("hora_checkin") or "15:00").strip() or "15:00", (form.get("descripcion") or "").strip() or None)
    )
    return _hoteles_html(request, id_reserva)


@app.post("/bitacora/{id_reserva}/hoteles/{id_hotel_itin}/habitaciones/{id_habitacion}/eliminar", response_class=HTMLResponse)
async def eliminar_habitacion_hotel(request: Request, id_reserva: int, id_hotel_itin: int, id_habitacion: int):
    if not usuario_activo(request):
        return HTMLResponse("", status_code=401)
    ejecutar_comando("DELETE FROM habitaciones_reserva WHERE id_habitacion=? AND id_hotel_itin=?", (id_habitacion, id_hotel_itin))
    return _hoteles_html(request, id_reserva)


# ─── Vuelos y Hoteles para Cotizaciones (mismo patrón, tabla _cotizacion) ────

def _vuelos_cot_html(request, id_cot):
    df_c = obtener_datos("SELECT tipo_vuelo, estado FROM cotizaciones WHERE id_cotizacion=?", (id_cot,))
    tipo_vuelo = df_c.iloc[0]["tipo_vuelo"] if not df_c.empty else "REDONDO"
    cot_activa = (df_c.iloc[0]["estado"] not in ("ACEPTADA", "RECHAZADA", "EXPIRADA")) if not df_c.empty else False
    df_v = obtener_datos(
        "SELECT id_vuelo, numero_tramo, aerolinea, numero_vuelo, origen, destino, fecha, hora, localizador "
        "FROM vuelos_cotizacion WHERE id_cotizacion=? ORDER BY numero_tramo, id_vuelo",
        (id_cot,)
    )
    return templates.TemplateResponse(request, "vuelos_cot_section.html", {
        "request": request, "id_cot": id_cot, "tipo_vuelo": tipo_vuelo,
        "vuelos": df_v.to_dict("records"), "cot_activa": cot_activa,
    })


def _hoteles_cot_html(request, id_cot):
    df_c = obtener_datos("SELECT estado FROM cotizaciones WHERE id_cotizacion=?", (id_cot,))
    cot_activa = (df_c.iloc[0]["estado"] not in ("ACEPTADA", "RECHAZADA", "EXPIRADA")) if not df_c.empty else False
    df_h = obtener_datos(
        "SELECT id_hotel_itin, numero_orden, ciudad_destino, nombre_hotel, localizador, fecha_checkin, fecha_checkout "
        "FROM hoteles_cotizacion WHERE id_cotizacion=? ORDER BY numero_orden, id_hotel_itin",
        (id_cot,)
    )
    return templates.TemplateResponse(request, "hoteles_cot_section.html", {
        "request": request, "id_cot": id_cot,
        "hoteles": df_h.to_dict("records"), "cot_activa": cot_activa,
        "habitaciones_por_hotel": _habitaciones_por_hotel("habitaciones_cotizacion", "id_cotizacion", id_cot),
    })


@app.post("/cotizaciones/{id_cot}/tipo_vuelo", response_class=HTMLResponse)
async def cotizacion_cambiar_tipo_vuelo(request: Request, id_cot: int):
    if not usuario_activo(request):
        return HTMLResponse("", status_code=401)
    form = await request.form()
    tipo = form.get("tipo_vuelo") or "REDONDO"
    if tipo not in ("SENCILLO", "REDONDO"):
        tipo = "REDONDO"
    ejecutar_comando("UPDATE cotizaciones SET tipo_vuelo=? WHERE id_cotizacion=?", (tipo, id_cot))
    return _vuelos_cot_html(request, id_cot)


@app.post("/cotizaciones/{id_cot}/vuelos/agregar", response_class=HTMLResponse)
async def cotizacion_agregar_vuelo(request: Request, id_cot: int):
    if not usuario_activo(request):
        return HTMLResponse("", status_code=401)
    form = await request.form()
    aerolinea = (form.get("aerolinea") or "").strip()
    if not aerolinea:
        return _vuelos_cot_html(request, id_cot)
    df_n = obtener_datos("SELECT COALESCE(MAX(numero_tramo),0) as n FROM vuelos_cotizacion WHERE id_cotizacion=?", (id_cot,))
    num_tramo = int(df_n.iloc[0]["n"] or 0) + 1
    ejecutar_comando(
        "INSERT INTO vuelos_cotizacion (id_cotizacion, numero_tramo, aerolinea, numero_vuelo, origen, destino, fecha, hora, localizador) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        (id_cot, num_tramo, aerolinea, (form.get("numero_vuelo") or "").strip(),
         (form.get("origen") or "").strip(), (form.get("destino") or "").strip(),
         form.get("fecha") or None, form.get("hora") or None, (form.get("localizador") or "").strip())
    )
    return _vuelos_cot_html(request, id_cot)


@app.post("/cotizaciones/{id_cot}/vuelos/{id_vuelo}/eliminar", response_class=HTMLResponse)
async def cotizacion_eliminar_vuelo(request: Request, id_cot: int, id_vuelo: int):
    if not usuario_activo(request):
        return HTMLResponse("", status_code=401)
    ejecutar_comando("DELETE FROM vuelos_cotizacion WHERE id_vuelo=? AND id_cotizacion=?", (id_vuelo, id_cot))
    return _vuelos_cot_html(request, id_cot)


@app.post("/cotizaciones/{id_cot}/hoteles/agregar", response_class=HTMLResponse)
async def cotizacion_agregar_hotel(request: Request, id_cot: int):
    if not usuario_activo(request):
        return HTMLResponse("", status_code=401)
    form = await request.form()
    nombre = (form.get("nombre_hotel") or "").strip()
    if not nombre:
        return _hoteles_cot_html(request, id_cot)
    df_n = obtener_datos("SELECT COALESCE(MAX(numero_orden),0) as n FROM hoteles_cotizacion WHERE id_cotizacion=?", (id_cot,))
    num_orden = int(df_n.iloc[0]["n"] or 0) + 1
    ejecutar_comando(
        "INSERT INTO hoteles_cotizacion (id_cotizacion, numero_orden, ciudad_destino, nombre_hotel, localizador, fecha_checkin, fecha_checkout) "
        "VALUES (?,?,?,?,?,?,?)",
        (id_cot, num_orden, (form.get("ciudad_destino") or "").strip(), nombre,
         (form.get("localizador") or "").strip(), form.get("fecha_checkin") or None, form.get("fecha_checkout") or None)
    )
    return _hoteles_cot_html(request, id_cot)


@app.post("/cotizaciones/{id_cot}/hoteles/{id_hotel_itin}/eliminar", response_class=HTMLResponse)
async def cotizacion_eliminar_hotel(request: Request, id_cot: int, id_hotel_itin: int):
    if not usuario_activo(request):
        return HTMLResponse("", status_code=401)
    ejecutar_comando("DELETE FROM hoteles_cotizacion WHERE id_hotel_itin=? AND id_cotizacion=?", (id_hotel_itin, id_cot))
    ejecutar_comando("DELETE FROM habitaciones_cotizacion WHERE id_hotel_itin=?", (id_hotel_itin,))
    return _hoteles_cot_html(request, id_cot)


@app.post("/cotizaciones/{id_cot}/hoteles/{id_hotel_itin}/habitaciones/agregar", response_class=HTMLResponse)
async def cotizacion_agregar_habitacion_hotel(request: Request, id_cot: int, id_hotel_itin: int):
    if not usuario_activo(request):
        return HTMLResponse("", status_code=401)
    form = await request.form()
    tipo = (form.get("tipo_habitacion") or "").strip()
    if not tipo:
        return _hoteles_cot_html(request, id_cot)
    try: personas = int(form.get("num_personas") or 1)
    except Exception: personas = 1
    ejecutar_comando(
        "INSERT INTO habitaciones_cotizacion (id_cotizacion, id_hotel_itin, tipo_habitacion, num_personas, hora_checkin, descripcion) VALUES (?,?,?,?,?,?)",
        (id_cot, id_hotel_itin, tipo, personas,
         (form.get("hora_checkin") or "15:00").strip() or "15:00", (form.get("descripcion") or "").strip() or None)
    )
    return _hoteles_cot_html(request, id_cot)


@app.post("/cotizaciones/{id_cot}/hoteles/{id_hotel_itin}/habitaciones/{id_habitacion}/eliminar", response_class=HTMLResponse)
async def cotizacion_eliminar_habitacion_hotel(request: Request, id_cot: int, id_hotel_itin: int, id_habitacion: int):
    if not usuario_activo(request):
        return HTMLResponse("", status_code=401)
    ejecutar_comando("DELETE FROM habitaciones_cotizacion WHERE id_habitacion=? AND id_hotel_itin=?", (id_habitacion, id_hotel_itin))
    return _hoteles_cot_html(request, id_cot)


# ─── Check-list operativo (HTMX) ─────────────────────────────────────────────

@app.post("/bitacora/{id_reserva}/checklist", response_class=HTMLResponse)
async def actualizar_checklist(request: Request, id_reserva: int):
    if not usuario_activo(request):
        return HTMLResponse("", status_code=401)
    form = await request.form()
    checkin_ida      = 1 if form.get("checkin_ida")      else 0
    checkin_regreso  = 1 if form.get("checkin_regreso")  else 0
    ejecutar_comando(
        "UPDATE reservas SET checkin_ida=?, checkin_regreso=? WHERE id_reserva=?",
        (checkin_ida, checkin_regreso, id_reserva)
    )
    registrar_cambio(id_reserva, "CHECKLIST",
        f"Check-in ida={'✅' if checkin_ida else '⬜'} · regreso={'✅' if checkin_regreso else '⬜'}",
        usuario=usuario_activo(request))
    df_r = obtener_datos(
        "SELECT checkin_ida, checkin_regreso FROM reservas WHERE id_reserva=?",
        (id_reserva,)
    )
    r = df_r.iloc[0].to_dict() if not df_r.empty else {}
    return templates.TemplateResponse(request, "checklist_section.html", {
        "request": request, "id_reserva": id_reserva, "reserva": r
    })


# ─── Gestión de estado (terminar / cancelar / incobrable) ────────────────────

@app.post("/bitacora/{id_reserva}/estado/terminar")
async def terminar_reserva(request: Request, id_reserva: int):
    if not usuario_activo(request):
        return RedirectResponse(url="/login")
    form = await request.form()
    if (form.get("confirmar") or "") != "SI":
        return RedirectResponse(url=f"/bitacora/{id_reserva}", status_code=303)
    ejecutar_comando("UPDATE reservas SET estado='TERMINADO' WHERE id_reserva=?", (id_reserva,))
    registrar_cambio(id_reserva, "TERMINADO", "Reserva marcada como terminada", usuario=usuario_activo(request))
    return RedirectResponse(url=f"/bitacora/{id_reserva}", status_code=303)


@app.post("/bitacora/{id_reserva}/estado/cancelar")
async def cancelar_reserva(request: Request, id_reserva: int):
    if not usuario_activo(request):
        return RedirectResponse(url="/login")
    if request.session.get("rol") != "admin":
        return RedirectResponse(url=f"/bitacora/{id_reserva}", status_code=303)
    form   = await request.form()
    motivo = (form.get("motivo") or "").strip()
    if not motivo or (form.get("confirmar") or "") != "SI":
        return RedirectResponse(url=f"/bitacora/{id_reserva}", status_code=303)

    def _f(k, d=0.0):
        try: return float(form.get(k) or d)
        except: return d

    reembolso = _f("reembolso")
    perdida   = _f("perdida")
    credito_aerolinea = _f("credito_aerolinea_generado")
    df_r = obtener_datos("SELECT moneda FROM reservas WHERE id_reserva=?", (id_reserva,))
    moneda = df_r.iloc[0]["moneda"] if not df_r.empty else "MXN"

    ops = [("UPDATE reservas SET estado='CANCELADO', monto_reembolsado=?, perdida_cancelacion=?, notas_cancelacion=? WHERE id_reserva=?",
            (reembolso, perdida, motivo, id_reserva))]
    if reembolso > 0:
        # Categoría propia y dedicada ('Reembolso a Cliente'), separada de 'Papelería
        # y Otros' — esa categoría también se usa para calcular el semáforo de
        # "Adicionales pagados" del itinerario, y un reembolso ahí lo inflaba.
        # sincronizar_cobrado_cliente() ya sabe restar esta categoría — no se toca
        # cobrado_cliente a mano.
        ops.append(("INSERT INTO flujo_caja (id_reserva, tipo_movimiento, tipo_egreso, categoria, concepto, monto, moneda, fecha_pago, fecha_creacion) "
                    "VALUES (?, 'EGRESO', 'COSTO DIRECTO VIAJE', 'Reembolso a Cliente', ?, ?, ?, ?, ?)",
                    (id_reserva, f"[Cancelación] Reembolso — {motivo}", reembolso, moneda, str(now_local().date()), str(now_local().date()))))
    ejecutar_transaccion(ops)
    if reembolso > 0:
        from database import sincronizar_cobrado_cliente
        sincronizar_cobrado_cliente(id_reserva)
    if credito_aerolinea > 0:
        _credito_aerolinea_registrar_generado(id_reserva, f"Cancelación — {motivo}", credito_aerolinea, usuario_activo(request))
    registrar_cambio(id_reserva, "CANCELADO",
        f"Motivo: {motivo} — Reembolso: ${reembolso:,.2f} — Pérdida: ${perdida:,.2f}"
        + (f" — Crédito aerolínea generado: ${credito_aerolinea:,.2f}" if credito_aerolinea > 0 else ""),
        usuario=usuario_activo(request))
    return RedirectResponse(url=f"/bitacora/{id_reserva}", status_code=303)


@app.post("/bitacora/{id_reserva}/estado/incobrable")
async def incobrable_reserva(request: Request, id_reserva: int):
    if not usuario_activo(request):
        return RedirectResponse(url="/login")
    if request.session.get("rol") != "admin":
        return RedirectResponse(url=f"/bitacora/{id_reserva}", status_code=303)
    form   = await request.form()
    motivo = (form.get("motivo") or "").strip()
    if not motivo or (form.get("confirmar") or "") != "SI":
        return RedirectResponse(url=f"/bitacora/{id_reserva}", status_code=303)

    df_r = obtener_datos("SELECT venta_total, cobrado_cliente, moneda FROM reservas WHERE id_reserva=?", (id_reserva,))
    if df_r.empty:
        return RedirectResponse(url=f"/bitacora/{id_reserva}", status_code=303)
    saldo_inc = float(df_r.iloc[0]["venta_total"]) - float(df_r.iloc[0]["cobrado_cliente"])
    moneda    = df_r.iloc[0]["moneda"]
    usuario   = usuario_activo(request)

    ops = [("UPDATE reservas SET estado='INCOBRABLE', notas_cancelacion=? WHERE id_reserva=?",
            (f"[INCOBRABLE] {motivo}", id_reserva))]
    if saldo_inc > 0.01:
        ops.append(("INSERT INTO flujo_caja (id_reserva, tipo_movimiento, tipo_egreso, categoria, concepto, "
                    "monto, moneda, fecha_pago, estado, usuario_creador, fecha_creacion) "
                    "VALUES (?, 'EGRESO', 'COSTO DIRECTO VIAJE', 'Deuda Incobrable', ?, ?, ?, ?, 'ACTIVO', ?, ?)",
                    (id_reserva, f"Deuda incobrable — {motivo}", saldo_inc, moneda,
                     str(now_local().date()), usuario, str(now_local().date()))))
    ejecutar_transaccion(ops)
    registrar_cambio(id_reserva, "INCOBRABLE",
        f"Motivo: {motivo} — Monto: ${saldo_inc:,.2f} {moneda}", usuario=usuario)
    return RedirectResponse(url=f"/bitacora/{id_reserva}", status_code=303)


# ─── Nota manual en bitácora ─────────────────────────────────────────────────

@app.post("/bitacora/{id_reserva}/nota", response_class=HTMLResponse)
async def agregar_nota(request: Request, id_reserva: int):
    if not usuario_activo(request):
        return HTMLResponse("", status_code=401)
    form  = await request.form()
    texto = (form.get("texto") or "").strip()
    if texto:
        registrar_cambio(id_reserva, "NOTA", texto, usuario=usuario_activo(request))
    df_bit = obtener_datos(
        "SELECT usuario, fecha, accion, detalle FROM bitacora_cambios WHERE id_reserva=? ORDER BY fecha DESC LIMIT 30",
        (id_reserva,)
    )
    return templates.TemplateResponse(request, "bitacora_section.html", {
        "request": request,
        "id_reserva": id_reserva,
        "bitacora": df_bit.to_dict("records"),
        "reserva": {"id_reserva": id_reserva},
    })


# ─── PDFs ─────────────────────────────────────────────────────────────────────

from fastapi.responses import Response as FastAPIResponse

@app.get("/bitacora/{id_reserva}/pdf/itinerario")
async def pdf_itinerario(request: Request, id_reserva: int):
    if not usuario_activo(request):
        return RedirectResponse(url="/login")
    from pdf_engine import generar_itinerario_pdf
    df_r  = obtener_datos(
        "SELECT r.*, c.nombre as nombre_cliente, c.telefono FROM reservas r "
        "JOIN clientes c ON r.id_cliente=c.id_cliente WHERE r.id_reserva=?", (id_reserva,)
    )
    if df_r.empty:
        return RedirectResponse(url="/bitacora")
    exp  = df_r.iloc[0].to_dict()
    df_pax  = obtener_datos("SELECT nombre, fecha_nacimiento, parentesco FROM pasajeros_reserva WHERE id_reserva=?", (id_reserva,))
    df_plan = obtener_datos("SELECT numero_pago, monto_esperado, fecha_programada, estado FROM plan_pagos WHERE id_reserva=? ORDER BY numero_pago", (id_reserva,))
    df_hab  = obtener_datos("SELECT tipo_habitacion, num_personas, hora_checkin, descripcion FROM habitaciones_reserva WHERE id_reserva=? ORDER BY id_habitacion", (id_reserva,))
    pdf = generar_itinerario_pdf(exp, exp["nombre_cliente"], exp["telefono"], df_pax, df_plan, LOGO_PATH, df_hab)
    return FastAPIResponse(content=pdf, media_type="application/pdf",
        headers={"Content-Disposition": f"inline; filename=Itinerario_{id_reserva}_{exp['destino'].replace(' ','_')}.pdf"})


@app.get("/bitacora/{id_reserva}/pdf/itinerario-cliente")
async def pdf_itinerario_cliente(request: Request, id_reserva: int):
    if not usuario_activo(request):
        return RedirectResponse(url="/login")
    from pdf_engine import generar_itinerario_cliente_pdf
    df_r  = obtener_datos(
        "SELECT r.*, c.nombre as nombre_cliente FROM reservas r "
        "JOIN clientes c ON r.id_cliente=c.id_cliente WHERE r.id_reserva=?", (id_reserva,)
    )
    if df_r.empty:
        return RedirectResponse(url="/bitacora")
    exp = df_r.iloc[0].to_dict()
    df_hab    = obtener_datos("SELECT tipo_habitacion, num_personas, hora_checkin, descripcion FROM habitaciones_reserva WHERE id_reserva=? ORDER BY id_habitacion", (id_reserva,))
    df_extras = obtener_datos("SELECT descripcion FROM extras_viaje WHERE id_reserva=? ORDER BY fecha_registro", (id_reserva,))
    token = obtener_token_portal(id_reserva)
    portal_url = str(request.base_url) + f"portal/{token}" if token else None
    df_vuelos_itin = obtener_datos(
        "SELECT numero_tramo, aerolinea, numero_vuelo, origen, destino, fecha, hora, localizador FROM vuelos_reserva WHERE id_reserva=? ORDER BY numero_tramo",
        (id_reserva,)
    )
    df_hoteles_itin_pdf = obtener_datos(
        "SELECT numero_orden, nombre_hotel, ciudad_destino, fecha_checkin, fecha_checkout, localizador FROM hoteles_reserva WHERE id_reserva=? ORDER BY numero_orden",
        (id_reserva,)
    )
    pdf = generar_itinerario_cliente_pdf(exp, exp["nombre_cliente"], df_hab, df_extras, LOGO_PATH, portal_url,
                                          vuelos_df=df_vuelos_itin, hoteles_df=df_hoteles_itin_pdf)
    return FastAPIResponse(content=pdf, media_type="application/pdf",
        headers={"Content-Disposition": f"inline; filename=Itinerario_Cliente_{id_reserva}_{exp['destino'].replace(' ','_')}.pdf"})


@app.get("/bitacora/{id_reserva}/pdf/estado-cuenta")
async def pdf_estado_cuenta(request: Request, id_reserva: int):
    if not usuario_activo(request):
        return RedirectResponse(url="/login")
    from pdf_engine import generar_estado_cuenta_pdf
    df_r  = obtener_datos(
        "SELECT r.*, c.nombre as nombre_cliente, c.telefono, c.email FROM reservas r "
        "JOIN clientes c ON r.id_cliente=c.id_cliente WHERE r.id_reserva=?", (id_reserva,)
    )
    if df_r.empty:
        return RedirectResponse(url="/bitacora")
    exp  = df_r.iloc[0].to_dict()
    df_movs = obtener_datos(
        "SELECT tipo_movimiento, estado, fecha_pago, concepto, monto, metodo_pago "
        "FROM flujo_caja WHERE id_reserva=? ORDER BY id_movimiento ASC", (id_reserva,)
    )
    df_plan = obtener_datos("SELECT numero_pago, monto_esperado, fecha_programada, estado FROM plan_pagos WHERE id_reserva=? ORDER BY numero_pago", (id_reserva,))
    token = obtener_token_portal(id_reserva)
    portal_url = str(request.base_url) + f"portal/{token}" if token else None
    pdf = generar_estado_cuenta_pdf(exp, exp["nombre_cliente"], exp["telefono"], exp.get("email",""), df_movs, df_plan, LOGO_PATH, portal_url)
    return FastAPIResponse(content=pdf, media_type="application/pdf",
        headers={"Content-Disposition": f"inline; filename=EstadoCuenta_{id_reserva}_{exp['nombre_cliente'].replace(' ','_')}.pdf"})


# ─── Portal del cliente (público, sin login — protegido solo por el token) ────

@app.get("/portal/{token}", response_class=HTMLResponse)
async def portal_cliente(request: Request, token: str):
    exp = obtener_reserva_por_token_portal(token)
    if not exp:
        return templates.TemplateResponse(request, "portal_cliente.html", {"encontrado": False}, status_code=404)

    if exp.get("estado") in ("CANCELADO", "INCOBRABLE"):
        return templates.TemplateResponse(request, "portal_cliente.html", {
            "encontrado": True, "cancelado": True, "reserva": exp,
        })

    saldo = calcular_saldo_real(exp["id_reserva"])
    df_plan = obtener_datos(
        "SELECT numero_pago, monto_esperado, monto_pagado, fecha_programada, estado "
        "FROM plan_pagos WHERE id_reserva=? ORDER BY numero_pago",
        (exp["id_reserva"],)
    )
    df_pagos = obtener_datos(
        "SELECT fecha_pago, monto, metodo_pago FROM flujo_caja "
        "WHERE id_reserva=? AND tipo_movimiento='INGRESO' AND estado='ACTIVO' ORDER BY fecha_pago DESC",
        (exp["id_reserva"],)
    )
    proximo = df_plan[df_plan["estado"].isin(["PENDIENTE", "PARCIAL"])].sort_values("fecha_programada")
    proximo_pago = proximo.iloc[0].to_dict() if not proximo.empty else None

    return templates.TemplateResponse(request, "portal_cliente.html", {
        "encontrado": True,
        "reserva": exp,
        "saldo": saldo,
        "plan": df_plan.to_dict("records"),
        "pagos": df_pagos.to_dict("records"),
        "proximo_pago": proximo_pago,
    })


@app.post("/bitacora/{id_reserva}/aplicar-prorrateo")
async def bitacora_aplicar_prorrateo(request: Request, id_reserva: int):
    if not usuario_activo(request):
        return HTMLResponse("", status_code=401)
    usuario = usuario_activo(request)
    df_r = obtener_datos("SELECT id_grupo, num_pax FROM reservas WHERE id_reserva=?", (id_reserva,))
    if not df_r.empty and pd.notna(df_r.iloc[0]["id_grupo"]):
        id_grupo = int(df_r.iloc[0]["id_grupo"])
        num_pax = int(df_r.iloc[0]["num_pax"] or 1)
        if aplicar_prorrateo_grupo_a_reserva(id_reserva, id_grupo, num_pax):
            df_g = obtener_datos("SELECT nombre_grupo FROM grupos_viaje WHERE id_grupo=?", (id_grupo,))
            nombre_grupo = df_g.iloc[0]["nombre_grupo"] if not df_g.empty else ""
            registrar_cambio(id_reserva, "COSTOS ACTUALIZADOS", f"Costo actualizado al prorrateo del grupo '{nombre_grupo}' ({num_pax} pax).", usuario=usuario)
    return HTMLResponse("")


@app.get("/bitacora/{id_reserva}", response_class=HTMLResponse)
async def detalle_reserva(request: Request, id_reserva: int):
    if not usuario_activo(request):
        return RedirectResponse(url="/login")

    df_r = obtener_datos(
        """SELECT r.*, c.nombre as nombre_cliente, c.telefono, c.email,
                  c.fecha_nacimiento as nacimiento_cliente, c.codigo_pais
           FROM reservas r JOIN clientes c ON r.id_cliente = c.id_cliente
           WHERE r.id_reserva = ?""",
        (id_reserva,)
    )
    if df_r.empty:
        return RedirectResponse(url="/bitacora")

    reserva = df_r.iloc[0].to_dict()

    df_pasajeros = obtener_datos(
        "SELECT id_pasajero, nombre, fecha_nacimiento, parentesco FROM pasajeros_reserva WHERE id_reserva = ? ORDER BY id_pasajero",
        (id_reserva,)
    )
    # Acompañantes frecuentes del cliente (excluyendo los ya en la reserva)
    _nombres_en_reserva = set(df_pasajeros["nombre"].tolist()) if not df_pasajeros.empty else set()
    df_acomp = obtener_datos(
        "SELECT id_acompanante, nombre, fecha_nacimiento, parentesco FROM acompanantes_cliente WHERE id_cliente=? ORDER BY nombre",
        (reserva["id_cliente"],)
    )
    _nombres_frecuentes = {r["nombre"] for r in df_acomp.to_dict("records")} if not df_acomp.empty else set()
    acompanantes_frecuentes = [r for r in df_acomp.to_dict("records") if r["nombre"] not in _nombres_en_reserva]
    df_plan = obtener_datos(
        "SELECT numero_pago, monto_esperado, monto_pagado, fecha_programada, estado FROM plan_pagos WHERE id_reserva = ? ORDER BY numero_pago",
        (id_reserva,)
    )
    # Candado de consistencia (mismo criterio que _plan_ctx / el_sistema_legado app.py:1650-1658).
    candado_dif = None
    if not df_plan.empty:
        df_ant_cd = obtener_datos(
            "SELECT COALESCE(SUM(monto),0) as t FROM flujo_caja WHERE id_reserva=? AND tipo_movimiento='INGRESO' AND estado='ACTIVO' AND concepto LIKE '%Anticipo%'",
            (id_reserva,)
        )
        _anticipo_cd = float(df_ant_cd["t"].iloc[0])
        _plan_total_cd = float(df_plan["monto_esperado"].sum())
        _venta_cd = float(reserva.get("venta_total") or 0)
        _dif_cd = round(_venta_cd - (_anticipo_cd + _plan_total_cd), 2)
        if abs(_dif_cd) > 1.0:
            candado_dif = {"anticipo": _anticipo_cd, "plan_total": _plan_total_cd, "venta": _venta_cd, "dif": abs(_dif_cd), "signo": "falta" if _dif_cd > 0 else "sobra"}
    df_bitacora = obtener_datos(
        "SELECT usuario, fecha, accion, detalle FROM bitacora_cambios WHERE id_reserva = ? ORDER BY fecha DESC LIMIT 30",
        (id_reserva,)
    )
    df_ajustes = obtener_datos(
        "SELECT detalle, cobro, costo, fecha FROM ajustes_reserva WHERE id_reserva = ? ORDER BY id_ajuste",
        (id_reserva,)
    )
    df_ingresos = obtener_datos(
        """SELECT id_movimiento, concepto, monto, fecha_pago, metodo_pago FROM flujo_caja
           WHERE id_reserva = ? AND tipo_movimiento = 'INGRESO' AND estado = 'ACTIVO'
           ORDER BY id_movimiento""",
        (id_reserva,)
    )

    saldo = calcular_saldo_real(id_reserva)

    # Extras suman a venta_total en vivo (ver agregar_extra) pero NUNCA a
    # costo_total — igual que Streamlit, que siempre agrega el costo de
    # extras_viaje por fuera al calcular costo/deuda a proveedores real. Se
    # calcula aquí, antes del semáforo/Control de Caja, para que ambos lo
    # incluyan (bug detectado 2026-08-27, itinerario #97: la tarjeta
    # "Cobro·Costo·Utilidad" y el Control de Caja subestimaban costo/deuda por
    # el costo exacto de cada extra).
    df_extras_tot = obtener_datos(
        "SELECT COALESCE(SUM(monto_cobrado_cliente),0) as cob, COALESCE(SUM(monto_costo_proveedor),0) as cos "
        "FROM extras_viaje WHERE id_reserva=?", (id_reserva,)
    )
    extras_cobro_total = float(df_extras_tot.iloc[0]["cob"]) if not df_extras_tot.empty else 0.0
    extras_costo_total = float(df_extras_tot.iloc[0]["cos"]) if not df_extras_tot.empty else 0.0

    # ── Semáforo de proveedores ───────────────────────────────────────────────
    df_eg = obtener_datos(
        "SELECT categoria, COALESCE(SUM(monto),0) as pagado FROM flujo_caja "
        "WHERE tipo_movimiento='EGRESO' AND estado='ACTIVO' AND id_reserva=? GROUP BY categoria",
        (id_reserva,)
    )
    pagos_prov = {r["categoria"]: float(r["pagado"] or 0) for _, r in df_eg.iterrows()} if not df_eg.empty else {}

    es_paquete = int(reserva.get("es_paquete_global", 0) or 0) == 1
    _cc = pagos_prov.get("Comisiones Bancarias", 0.0)

    if es_paquete:
        _costo_paq = max(0.0, float(reserva.get("costo_total", 0) or 0) + extras_costo_total - _cc)
        _pag_paq   = sum(v for k, v in pagos_prov.items() if k != "Comisiones Bancarias")
        semaforo = [{
            "label": "📦 Paquete Global",
            "costo": _costo_paq, "pagado": _pag_paq,
            "estado": "ok" if _pag_paq >= _costo_paq - 0.01 else "pendiente",
        }]
    else:
        _pag_adi = (pagos_prov.get("Pago de Adicionales (Proveedor)", 0)
                    + pagos_prov.get("Pago de Ajustes/Cambios (Proveedor)", 0)
                    + pagos_prov.get("Papelería y Otros", 0))
        def _st(c, p):
            c, p = float(c or 0), float(p or 0)
            return "na" if c <= 0 else ("ok" if p >= c - 0.01 else "pendiente"), c, p
        semaforo = []
        for lbl, col_c, pag, extra_c in [
            ("✈️ Vuelo",       "costo_vuelos",     pagos_prov.get("Pago de Vuelo (Proveedor)", 0), 0.0),
            ("⛽ TUA",          "costo_tua",        pagos_prov.get("Pago de TUA (Impuesto)", 0), 0.0),
            ("🏨 Hotel",        "costo_hotel",      pagos_prov.get("Pago de Hotel (Proveedor)", 0), 0.0),
            ("🚐 Traslados",    "costo_traslados",  pagos_prov.get("Pago de Traslado (Proveedor)", 0), 0.0),
            ("🗺️ Tours",       "costo_tours",      pagos_prov.get("Pago de Tours (Proveedor)", 0), 0.0),
            ("➕ Adicionales", "costo_adicionales", _pag_adi, extras_costo_total),
        ]:
            est, c, p = _st(float(reserva.get(col_c, 0) or 0) + extra_c, pag)
            semaforo.append({"label": lbl, "costo": c, "pagado": p, "estado": est})

    # ── Control de caja: cobrado vs. entregado a proveedores ─────────────────
    # Usa las mismas 6 categorías "limpias" (sin Papelería/Ajustes-Cambios) para
    # que el total cuadre con lo que se puede auditar por categoría específica.
    if es_paquete:
        _pagado_prov_tot = _pag_paq
        _costo_servicios_tot = _costo_paq
    else:
        _pagado_prov_tot = (
            pagos_prov.get("Pago de Vuelo (Proveedor)", 0.0)
            + pagos_prov.get("Pago de TUA (Impuesto)", 0.0)
            + pagos_prov.get("Pago de Hotel (Proveedor)", 0.0)
            + pagos_prov.get("Pago de Traslado (Proveedor)", 0.0)
            + pagos_prov.get("Pago de Tours (Proveedor)", 0.0)
            + pagos_prov.get("Pago de Adicionales (Proveedor)", 0.0)
        )
        _costo_servicios_tot = float(reserva.get("costo_total", 0) or 0) + extras_costo_total - _cc
    # Se resta también la comisión bancaria (_cc): ya salió de la cuenta aunque no sea pago a
    # proveedor. Lo "retenido" se limita a lo que aún hace falta para cubrir la deuda a
    # proveedores — el excedente ya es utilidad realizada, no dinero pendiente de aplicar.
    _deuda_prov_final = max(_costo_servicios_tot - _pagado_prov_tot, 0.0)
    _retenido_bruto = saldo["cobrado_activos"] - _pagado_prov_tot - _cc
    if _retenido_bruto > 0:
        _utilidad_cc = max(_retenido_bruto - _deuda_prov_final, 0.0)
        _retenido_final = _retenido_bruto - _utilidad_cc
    else:
        _retenido_final = _retenido_bruto
        _utilidad_cc = 0.0
    control_caja = {
        "cobrado": saldo["cobrado_activos"],
        "pagado_proveedores": _pagado_prov_tot,
        "retenido": _retenido_final,
        "deuda_proveedores": _deuda_prov_final,
        "utilidad": _utilidad_cc,
    }

    # ── Reservas de grupo: el semáforo/deuda a proveedor individual se controla a
    # nivel de grupo (ver /grupos/{id}), no aquí — evita alertas falsas duplicadas
    # (una por cada pax/familia del mismo grupo) y doble contabilización de pago.
    es_de_grupo = pd.notna(reserva.get("id_grupo"))
    nombre_grupo_reserva = None
    if es_de_grupo:
        df_grp_nom = obtener_datos("SELECT nombre_grupo, cupos_bloqueados FROM grupos_viaje WHERE id_grupo=?", (int(reserva["id_grupo"]),))
        if not df_grp_nom.empty:
            nombre_grupo_reserva = df_grp_nom.iloc[0]["nombre_grupo"]
            reserva["cupos_bloqueados_grupo"] = int(df_grp_nom.iloc[0]["cupos_bloqueados"])
        semaforo = []
        control_caja["deuda_proveedores"] = 0.0

    df_extras = obtener_datos(
        "SELECT id_extra, descripcion, monto_cobrado_cliente, monto_costo_proveedor, "
        "metodo_pago, fecha_registro, tipo_registro "
        "FROM extras_viaje WHERE id_reserva=? ORDER BY fecha_registro DESC",
        (id_reserva,)
    )
    df_pend_count = obtener_datos(
        "SELECT COUNT(*) as n FROM plan_pagos WHERE id_reserva=? AND estado IN ('PENDIENTE','PARCIAL')", (id_reserva,)
    )
    hay_pend = int(df_pend_count.iloc[0]["n"]) > 0 if not df_pend_count.empty else False

    from datetime import timedelta
    hoy = now_local().date()
    df_hab = obtener_datos(
        "SELECT tipo_habitacion, num_personas, hora_checkin, descripcion FROM habitaciones_reserva WHERE id_reserva=? ORDER BY id_habitacion",
        (id_reserva,)
    )
    # Historial de Flujo y Anulaciones — TODOS los movimientos de flujo_caja de esta
    # reserva (cualquier categoría/estado), equivalente al tab "Historial de Flujo y
    # Anulaciones (Void)" de Streamlit. Complementa Cobros (solo INGRESO) y Pagos a
    # Proveedores (solo EGRESO de categorías de servicio) — aquí sí se ve, por
    # ejemplo, la Comisión Bancaria individual que ninguna de esas dos secciones
    # muestra como movimiento propio.
    df_historial_flujo = obtener_datos(
        "SELECT id_movimiento, fecha_pago, tipo_movimiento, categoria, concepto, monto, moneda, estado, motivo_anulacion "
        "FROM flujo_caja WHERE id_reserva=? ORDER BY id_movimiento DESC",
        (id_reserva,)
    )
    df_vuelos = obtener_datos(
        "SELECT id_vuelo, numero_tramo, aerolinea, numero_vuelo, origen, destino, fecha, hora, localizador, checkin "
        "FROM vuelos_reserva WHERE id_reserva=? ORDER BY numero_tramo, id_vuelo",
        (id_reserva,)
    )
    df_hoteles_itin = obtener_datos(
        "SELECT id_hotel_itin, numero_orden, ciudad_destino, nombre_hotel, localizador, fecha_checkin, fecha_checkout "
        "FROM hoteles_reserva WHERE id_reserva=? ORDER BY numero_orden, id_hotel_itin",
        (id_reserva,)
    )
    reserva_activa = reserva.get("estado") == "ACTIVO"
    return templates.TemplateResponse(request, "reserva_detalle.html", ctx(request, {
        "active": "bitacora",
        "reserva": reserva,
        "habitaciones": df_hab.to_dict("records") if not df_hab.empty else [],
        "pasajeros": df_pasajeros.to_dict("records"),
        "acompanantes": acompanantes_frecuentes,
        "nombres_frecuentes": _nombres_frecuentes,
        "plan_pagos": df_plan.to_dict("records"),
        "candado_dif": candado_dif,
        "bitacora": df_bitacora.to_dict("records"),
        "ajustes": df_ajustes.to_dict("records"),
        "ingresos": df_ingresos.to_dict("records"),
        "extras": df_extras.to_dict("records"),
        "historial_flujo": df_historial_flujo.to_dict("records"),
        "vuelos": df_vuelos.to_dict("records"),
        "hoteles_itin": df_hoteles_itin.to_dict("records"),
        "habitaciones_por_hotel": _habitaciones_por_hotel("habitaciones_reserva", "id_reserva", id_reserva),
        "reserva_activa": reserva_activa,
        "extras_cobro_total": extras_cobro_total,
        "extras_costo_total": extras_costo_total,
        "hay_parcialidades_pendientes": hay_pend,
        "saldo": saldo,
        "semaforo": semaforo,
        "control_caja": control_caja,
        "pagos_prov": _pagos_prov_ctx(id_reserva),
        "cuentas_destino": CUENTAS_SIMPLE_DEFAULT,
        "cuentas_por_metodo": CUENTAS_POR_METODO,
        "es_de_grupo": es_de_grupo,
        "nombre_grupo_reserva": nombre_grupo_reserva,
        "today":    str(hoy),
        "today_7d": str(hoy + timedelta(days=7)),
        "today_15d": str(hoy + timedelta(days=15)),
    }))


# ─── Viajes Grupales ──────────────────────────────────────────────────────────
# Réplica del modelo ya en producción en el_sistema_legado (Streamlit) — mismo esquema de
# datos, adaptado a páginas propias + HTMX en vez de tabs. Ver plan de implementación
# 2026-08-21 (memoria: project-erp-agencia-viajes / project-viajes-grupales).

_LG_TIPOS = ["Mayorista", "Aerolínea/Vuelo", "Hotel", "Traslado", "Tours"]
_LG_DATE_CFG = {
    "Aerolínea/Vuelo": {"f1": "Fecha Salida", "f2": "Fecha Regreso", "h1": "Hora Ida", "h2": "Hora Vuelta"},
    "Hotel": {"f1": "Fecha Check-in", "f2": "Fecha Check-out", "h1": "Hora Check-in", "h2": "Hora Check-out"},
    "Traslado": {"f1": "Fecha del Traslado", "f2": None, "h1": "Hora del Traslado", "h2": None},
    "Tours": {"f1": "Día del Tour", "f2": None, "h1": "Hora de Inicio", "h2": None},
}
_LG_COLS_MAP = {
    "Mayorista": ("mayorista", "localizador_global"),
    "Aerolínea/Vuelo": ("aerolinea", "itinerario_vuelo_plataforma"),
    "Hotel": ("nombre_hotel", "itinerario_hotel_plataforma"),
    "Traslado": ("proveedor_traslados", "confirmacion_proveedor_traslados"),
    "Tours": ("proveedor_tours", "confirmacion_proveedor_tours"),
}
_UPSERT_CATALOGO_MAP = {
    "Mayorista": upsert_catalogo_mayorista, "Aerolínea/Vuelo": upsert_catalogo_aerolinea,
    "Hotel": upsert_catalogo_hotel, "Traslado": upsert_catalogo_proveedor_traslados,
    "Tours": upsert_catalogo_proveedor_tours,
}


def _col_costo_por_categoria(categoria):
    c = (categoria or "").lower()
    if "vuelo" in c: return "costo_vuelos"
    if "hotel" in c: return "costo_hotel"
    if "tua" in c: return "costo_tua"
    if "traslado" in c: return "costo_traslados"
    if "tour" in c: return "costo_tours"
    return "costo_adicionales"


def _offset_pax_grupo(id_grupo, id_reserva):
    """Cuántos pax de OTRAS reservas del grupo (activas, ordenadas por id_reserva) van
    antes de esta — define en qué "asientos" del prorrateo cae esta reserva, para que
    el reparto entre todas las reservas del grupo sume exacto al presupuesto (ver
    _costos_prorrateados_grupo)."""
    df_otras = obtener_datos(
        "SELECT COALESCE(SUM(num_pax),0) as t FROM reservas "
        "WHERE id_grupo=? AND estado='ACTIVO' AND id_reserva < ?",
        (id_grupo, id_reserva)
    )
    return int(df_otras.iloc[0]["t"]) if not df_otras.empty else 0


def _costos_prorrateados_grupo(id_grupo, num_pax, offset_pax=0):
    """Costo prorrateado del presupuesto del grupo para `num_pax` personas, por columna
    de costo de `reservas`. Solo cálculo — no escribe nada.

    Usa distribuir_equitativo() por línea de presupuesto en vez de round(monto/cupos*pax,2)
    — la fórmula anterior perdía centavos: sumado sobre todas las reservas del grupo, el
    total repartido no siempre cuadraba con monto_presupuestado. `offset_pax` (vía
    _offset_pax_grupo) ubica qué "asientos" del reparto le tocan a esta reserva
    específica, para que sumado entre TODAS las reservas del grupo el total sí cuadre
    exacto."""
    df_g = obtener_datos("SELECT cupos_bloqueados FROM grupos_viaje WHERE id_grupo=?", (id_grupo,))
    if df_g.empty: return None
    cupos = max(int(df_g.iloc[0]["cupos_bloqueados"]), 1)
    df_p = obtener_datos("SELECT categoria, monto_presupuestado FROM grupo_presupuesto WHERE id_grupo=?", (id_grupo,))
    costos = {"costo_vuelos": 0.0, "costo_tua": 0.0, "costo_hotel": 0.0, "costo_traslados": 0.0, "costo_tours": 0.0, "costo_adicionales": 0.0}
    ini = max(0, min(offset_pax, cupos))
    fin = max(0, min(offset_pax + int(num_pax), cupos))
    for _, lp in df_p.iterrows():
        col = _col_costo_por_categoria(lp["categoria"])
        partes = distribuir_equitativo(float(lp["monto_presupuestado"]), cupos)
        costos[col] += round(sum(partes[ini:fin]), 2)
    return costos


def aplicar_prorrateo_grupo_a_reserva(id_reserva, id_grupo, num_pax):
    """Recalcula los costo_* de una reserva como el prorrateo actual del presupuesto
    del grupo. Acción explícita (nunca automática) — no toca extras_viaje."""
    offset_pax = _offset_pax_grupo(id_grupo, id_reserva)
    costos = _costos_prorrateados_grupo(id_grupo, num_pax, offset_pax)
    if costos is None: return False
    df_v = obtener_datos("SELECT venta_total, costo_comisiones FROM reservas WHERE id_reserva=?", (id_reserva,))
    if df_v.empty: return False
    venta = float(df_v.iloc[0]["venta_total"])
    comisiones = float(df_v.iloc[0]["costo_comisiones"] or 0)
    costo_total = round(sum(costos.values()) + comisiones, 2)
    utilidad = round(venta - costo_total, 2)
    return ejecutar_comando(
        "UPDATE reservas SET costo_vuelos=?, costo_tua=?, costo_hotel=?, costo_traslados=?, costo_tours=?, costo_adicionales=?, costo_total=?, utilidad_proyectada=? WHERE id_reserva=?",
        (costos["costo_vuelos"], costos["costo_tua"], costos["costo_hotel"], costos["costo_traslados"], costos["costo_tours"], costos["costo_adicionales"], costo_total, utilidad, id_reserva)
    )


@app.get("/api/grupos/prefill", response_class=HTMLResponse)
async def api_grupos_prefill(request: Request, id_grupo: str = "", num_pax: str = "1"):
    if not usuario_activo(request):
        return HTMLResponse("")
    if not id_grupo:
        return HTMLResponse('<input type="hidden" name="num_pax" value="1">')
    try:
        id_grupo_i = int(id_grupo)
        num_pax_i = max(int(num_pax or 1), 1)
    except ValueError:
        return HTMLResponse("")
    grupo = _grupo_ctx_base(id_grupo_i)
    if grupo is None:
        return HTMLResponse("")
    ocupados_sin_esta = max(grupo["pax_ocupados"], 0)
    ocupados_con_esta = ocupados_sin_esta + num_pax_i
    cupo_html = ""
    if ocupados_con_esta > grupo["cupos_bloqueados"]:
        cupo_html = f'<p style="color:var(--danger); font-size:0.8rem; margin:8px 0 0;">⚠️ Excede el cupo bloqueado: {ocupados_con_esta} de {grupo["cupos_bloqueados"]} pax. Coordina el costo adicional con el proveedor.</p>'
    else:
        cupo_html = f'<p style="color:var(--text2); font-size:0.8rem; margin:8px 0 0;">🟢 Cupo del grupo: {ocupados_con_esta} de {grupo["cupos_bloqueados"]} pax.</p>'
    if grupo.get("descripcion_paquete"):
        cupo_html += f'<p style="color:var(--text2); font-size:0.8rem; margin:4px 0 0;">📦 Paquete incluye: {_esc(str(grupo["descripcion_paquete"]))}</p>'

    df_l = obtener_datos("SELECT tipo, nombre, confirmacion, detalle FROM grupo_logistica WHERE id_grupo=? ORDER BY tipo, id_logistica", (id_grupo_i,))
    logist = {}
    multiples = []
    if not df_l.empty:
        for tipo in df_l["tipo"].unique():
            opts = df_l[df_l["tipo"] == tipo]
            logist[tipo] = opts.iloc[0].to_dict()
            if len(opts) > 1:
                multiples.append(tipo)
    if multiples:
        cupo_html += f'<p style="color:var(--warning); font-size:0.78rem; margin:6px 0 0;">ℹ️ Este grupo tiene varias opciones de: {", ".join(multiples)}. Se prellenó la primera — ajusta abajo si aplica otra.</p>'

    def _v(tipo, campo):
        return _esc(str(logist.get(tipo, {}).get(campo) or ""))

    costos = _costos_prorrateados_grupo(id_grupo_i, num_pax_i) or {}
    venta_sugerida = round(float(grupo["precio_paquete_base"]) * num_pax_i, 2)

    campos_grupo = f"""
<div class="form-field" style="margin-top:8px;">
  <label class="field-label">Número de pax que cubre esta reserva</label>
  <input class="field-input" type="number" name="num_pax" min="1" value="{num_pax_i}"
         hx-get="/api/grupos/prefill" hx-include="#sel-grupo,this" hx-trigger="change"
         hx-target="#campos-grupo" hx-swap="innerHTML">
</div>
{cupo_html}
"""

    oob = f"""
<input class="field-input" type="text" name="aerolinea" list="dl_aerolineas" id="f-aerolinea" value="{_v('Aerolínea/Vuelo','nombre')}" hx-swap-oob="true">
<input class="field-input" type="text" name="itinerario_vuelo_plataforma" id="f-loc-vuelo" value="{_v('Aerolínea/Vuelo','confirmacion')}" hx-swap-oob="true">
<input class="field-input" type="text" name="mayorista" list="dl_mayoristas" id="f-mayorista" value="{_v('Mayorista','nombre')}" hx-swap-oob="true">
<input class="field-input" type="text" name="localizador_global" id="f-loc-mayorista" value="{_v('Mayorista','confirmacion')}" hx-swap-oob="true">
<input class="field-input" type="text" name="nombre_hotel" list="dl_hoteles" id="f-hotel" value="{_v('Hotel','nombre')}" hx-swap-oob="true">
<input class="field-input" type="text" name="itinerario_hotel_plataforma" id="f-loc-hotel" value="{_v('Hotel','confirmacion')}" hx-swap-oob="true">
<input class="field-input" type="text" name="proveedor_traslados" list="dl_prov_traslados" id="f-prov-traslados" value="{_v('Traslado','nombre')}" hx-swap-oob="true">
<input class="field-input" type="text" name="confirmacion_proveedor_traslados" id="f-conf-traslados" value="{_v('Traslado','confirmacion')}" hx-swap-oob="true">
<input class="field-input" type="text" name="proveedor_tours" list="dl_prov_tours" id="f-prov-tours" value="{_v('Tours','nombre')}" hx-swap-oob="true">
<input class="field-input" type="text" name="confirmacion_proveedor_tours" id="f-conf-tours" value="{_v('Tours','confirmacion')}" hx-swap-oob="true">
<input class="field-input field-money" type="number" step="0.01" min="0" id="cobro_hotel" name="cobro_hotel" data-calc="cobro" value="{venta_sugerida}" hx-swap-oob="true">
<input class="field-input field-money" type="number" step="0.01" min="0" id="costo_vuelos" name="costo_vuelos" data-calc="costo" value="{costos.get('costo_vuelos', 0)}" hx-swap-oob="true">
<input class="field-input field-money" type="number" step="0.01" min="0" id="costo_tua" name="costo_tua" data-calc="costo" value="{costos.get('costo_tua', 0)}" hx-swap-oob="true">
<input class="field-input field-money" type="number" step="0.01" min="0" id="costo_hotel" name="costo_hotel" data-calc="costo" value="{costos.get('costo_hotel', 0)}" hx-swap-oob="true">
<input class="field-input field-money" type="number" step="0.01" min="0" id="costo_traslados" name="costo_traslados" data-calc="costo" value="{costos.get('costo_traslados', 0)}" hx-swap-oob="true">
<input class="field-input field-money" type="number" step="0.01" min="0" id="costo_tours" name="costo_tours" data-calc="costo" value="{costos.get('costo_tours', 0)}" hx-swap-oob="true">
<input class="field-input field-money" type="number" step="0.01" min="0" id="costo_adicionales" name="costo_adicionales" data-calc="costo" value="{costos.get('costo_adicionales', 0)}" hx-swap-oob="true">
<script>recalcular();</script>
"""
    return HTMLResponse(campos_grupo + oob)


def _grupo_ctx_base(id_grupo):
    """Datos generales del grupo + agregados de cupo/presupuesto/venta, reutilizado
    por el detalle y por el helper de refresco tras cada acción HTMX."""
    df_g = obtener_datos("SELECT * FROM grupos_viaje WHERE id_grupo=?", (id_grupo,))
    if df_g.empty: return None
    grupo = df_g.iloc[0].to_dict()
    df_ocup = obtener_datos("SELECT COALESCE(SUM(num_pax),0) as n FROM reservas WHERE id_grupo=? AND estado='ACTIVO'", (id_grupo,))
    grupo["pax_ocupados"] = int(df_ocup.iloc[0]["n"]) if not df_ocup.empty else 0
    df_agg = obtener_datos(
        "SELECT COALESCE(SUM(venta_total),0) as venta, COALESCE(SUM(cobrado_cliente),0) as cobrado "
        "FROM reservas WHERE id_grupo=? AND estado='ACTIVO'", (id_grupo,)
    )
    grupo["venta_total_grupo"] = float(df_agg.iloc[0]["venta"]) if not df_agg.empty else 0.0
    grupo["cobrado_total_grupo"] = float(df_agg.iloc[0]["cobrado"]) if not df_agg.empty else 0.0
    df_p = obtener_datos("SELECT id_presupuesto, categoria, descripcion, monto_presupuestado FROM grupo_presupuesto WHERE id_grupo=? ORDER BY id_presupuesto", (id_grupo,))
    df_pag = obtener_datos("SELECT id_presupuesto, COALESCE(SUM(monto),0) as pagado FROM flujo_caja WHERE id_grupo=? AND estado='ACTIVO' AND tipo_movimiento='EGRESO' GROUP BY id_presupuesto", (id_grupo,))
    pagado_map = {int(r["id_presupuesto"]): float(r["pagado"]) for _, r in df_pag.iterrows()} if not df_pag.empty else {}
    presupuesto_total = float(df_p["monto_presupuestado"].sum()) if not df_p.empty else 0.0
    pagado_total = sum(pagado_map.values())
    grupo["presupuesto_total"] = presupuesto_total
    grupo["pagado_total"] = pagado_total
    grupo["pendiente_total"] = round(presupuesto_total - pagado_total, 2)
    grupo["utilidad_real"] = round(grupo["venta_total_grupo"] - pagado_total, 2)
    cupo_pax = max(int(grupo["cupos_bloqueados"]), 1)
    costo_pax = round(presupuesto_total / cupo_pax, 2)
    margen = float(grupo["margen_deseado_pct"] or 0.0)
    grupo["venta_sugerida_pax"] = round(costo_pax / (1 - margen / 100.0), 2) if margen > 0 else costo_pax
    return grupo


def _presupuesto_grupo_html(request, id_grupo):
    df_p = obtener_datos("SELECT id_presupuesto, categoria, descripcion, monto_presupuestado FROM grupo_presupuesto WHERE id_grupo=? ORDER BY id_presupuesto", (id_grupo,))
    df_pag = obtener_datos("SELECT id_presupuesto, COALESCE(SUM(monto),0) as pagado FROM flujo_caja WHERE id_grupo=? AND estado='ACTIVO' AND tipo_movimiento='EGRESO' GROUP BY id_presupuesto", (id_grupo,))
    pagado_map = {int(r["id_presupuesto"]): float(r["pagado"]) for _, r in df_pag.iterrows()} if not df_pag.empty else {}
    desc_map = {int(r["id_presupuesto"]): f"{r['categoria']} — {r['descripcion'] or 's/desc'}" for _, r in df_p.iterrows()}
    lineas = []
    for _, r in df_p.iterrows():
        pagado = pagado_map.get(int(r["id_presupuesto"]), 0.0)
        d = r.to_dict()
        d["pagado"] = pagado
        d["pendiente"] = round(float(r["monto_presupuestado"]) - pagado, 2)
        d["tiene_pagos"] = pagado > 0
        lineas.append(d)

    # Pagos a proveedor ya registrados para este grupo — con acción de anular, para
    # poder corregir una captura equivocada (línea/monto/método) sin tocar la BD a mano.
    # Antes esto no existía: la única acción disponible era borrar la LÍNEA de presupuesto,
    # bloqueada mientras tuviera pagos — dejaba a la usuaria sin forma de corregir un error
    # de captura.
    df_pagos = obtener_datos(
        "SELECT id_movimiento, id_presupuesto, monto, metodo_pago, fecha_pago, estado, motivo_anulacion "
        "FROM flujo_caja WHERE id_grupo=? AND tipo_movimiento='EGRESO' ORDER BY id_movimiento DESC",
        (id_grupo,)
    )
    pagos = []
    for _, r in df_pagos.iterrows():
        d = r.to_dict()
        d["linea_desc"] = desc_map.get(int(r["id_presupuesto"]), "Línea eliminada") if pd.notna(r["id_presupuesto"]) else "—"
        pagos.append(d)

    return templates.TemplateResponse(request, "presupuesto_grupo_section.html", {
        "request": request, "id_grupo": id_grupo, "lineas": lineas, "pagos": pagos,
        "categorias": ["Vuelos", "Hotel", "TUA", "Traslados", "Tours", "Publicidad", "Guías", "Propinas", "Viáticos de Grupo", "Otro"],
    })


def _logistica_grupo_html(request, id_grupo):
    df_l = obtener_datos("SELECT id_logistica, tipo, nombre, confirmacion, detalle, fecha_salida, fecha_regreso, hora_ida, hora_vuelta FROM grupo_logistica WHERE id_grupo=? ORDER BY tipo, id_logistica", (id_grupo,))
    df_res = obtener_datos("SELECT r.id_reserva, c.nombre FROM reservas r JOIN clientes c ON r.id_cliente=c.id_cliente WHERE r.id_grupo=? AND r.estado='ACTIVO' ORDER BY r.id_reserva", (id_grupo,))
    return templates.TemplateResponse(request, "logistica_grupo_section.html", {
        "request": request, "id_grupo": id_grupo, "lineas": df_l.to_dict("records"),
        "tipos": _LG_TIPOS, "date_cfg": _LG_DATE_CFG,
        "reservas": df_res.to_dict("records"),
    })


@app.get("/grupos")
async def grupos_lista(request: Request, q: str = "", estado: str = "ACTIVO"):
    if not usuario_activo(request):
        return RedirectResponse(url="/login")
    where = []
    params = []
    if estado != "Todos":
        where.append("estado = ?"); params.append(estado)
    if q.strip():
        where.append("(nombre_grupo LIKE ? OR destino LIKE ?)")
        params += [f"%{q.strip()}%", f"%{q.strip()}%"]
    where_sql = ("WHERE " + " AND ".join(where)) if where else ""
    df = obtener_datos(f"SELECT id_grupo, nombre_grupo, destino, fecha_salida, fecha_regreso, cupos_bloqueados, estado FROM grupos_viaje {where_sql} ORDER BY fecha_salida DESC", tuple(params))
    grupos = []
    for _, g in df.iterrows():
        df_ocup = obtener_datos("SELECT COALESCE(SUM(num_pax),0) as n FROM reservas WHERE id_grupo=? AND estado='ACTIVO'", (int(g["id_grupo"]),))
        d = g.to_dict()
        d["pax_ocupados"] = int(df_ocup.iloc[0]["n"]) if not df_ocup.empty else 0
        grupos.append(d)
    return templates.TemplateResponse(request, "grupos_lista.html", ctx(request, {
        "active": "grupos", "grupos": grupos, "q": q, "estado": estado,
    }))


@app.get("/grupos/nuevo")
async def grupo_nuevo_form(request: Request):
    if not usuario_activo(request):
        return RedirectResponse(url="/login")
    return templates.TemplateResponse(request, "grupo_form.html", ctx(request, {
        "active": "grupos", "today": str(now_local().date()),
        "catalogo_destinos": get_catalogo_destinos(),
    }))


@app.post("/grupos/nuevo")
async def grupo_nuevo_post(request: Request):
    if not usuario_activo(request):
        return RedirectResponse(url="/login")
    form = await request.form()
    usuario = usuario_activo(request)
    nombre = (form.get("nombre_grupo") or "").strip()
    destino = upsert_catalogo_destino((form.get("destino") or "").strip())
    if not nombre or not destino:
        return templates.TemplateResponse(request, "grupo_form.html", ctx(request, {
            "active": "grupos", "today": str(now_local().date()),
            "error": "El nombre y el destino son obligatorios.",
            "catalogo_destinos": get_catalogo_destinos(),
        }))
    id_new = ejecutar_insert(
        "INSERT INTO grupos_viaje (nombre_grupo, destino, origen, fecha_salida, fecha_regreso, moneda, cupos_bloqueados, descripcion_paquete, precio_paquete_base, margen_deseado_pct, usuario_creador, fecha_creacion) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (nombre, destino, (form.get("origen") or "Monterrey").strip(), form.get("fecha_salida"), form.get("fecha_regreso"),
         form.get("moneda") or "MXN", int(form.get("cupos_bloqueados") or 0), (form.get("descripcion_paquete") or "").strip(),
         float(form.get("precio_paquete_base") or 0), float(form.get("margen_deseado_pct") or 0), usuario, str(now_local().date()))
    )
    return RedirectResponse(url=f"/grupos/{id_new}", status_code=303)


@app.get("/grupos/{id_grupo}")
async def grupo_detalle(request: Request, id_grupo: int):
    if not usuario_activo(request):
        return RedirectResponse(url="/login")
    grupo = _grupo_ctx_base(id_grupo)
    if grupo is None:
        return RedirectResponse(url="/grupos")
    df_res = obtener_datos(
        "SELECT r.id_reserva, c.nombre, r.num_pax, r.venta_total, r.costo_total, "
        "ROUND(r.venta_total - r.costo_total, 2) as utilidad, r.cobrado_cliente, r.estado "
        "FROM reservas r JOIN clientes c ON r.id_cliente=c.id_cliente WHERE r.id_grupo=? ORDER BY r.id_reserva",
        (id_grupo,)
    )
    resp_p = _presupuesto_grupo_html(request, id_grupo)
    resp_l = _logistica_grupo_html(request, id_grupo)
    return templates.TemplateResponse(request, "grupo_detalle.html", ctx(request, {
        "active": "grupos", "grupo": grupo,
        "reservas": df_res.to_dict("records"),
        "lineas_presupuesto": resp_p.context["lineas"], "categorias": resp_p.context["categorias"],
        "lineas_logistica": resp_l.context["lineas"], "tipos_logistica": _LG_TIPOS, "date_cfg": _LG_DATE_CFG,
        "today": str(now_local().date()),
        "catalogo_destinos": get_catalogo_destinos(),
    }))


@app.post("/grupos/{id_grupo}/editar")
async def grupo_editar(request: Request, id_grupo: int):
    if not usuario_activo(request):
        return RedirectResponse(url="/login")
    form = await request.form()
    ejecutar_comando(
        "UPDATE grupos_viaje SET nombre_grupo=?, origen=?, destino=?, fecha_salida=?, fecha_regreso=?, moneda=?, cupos_bloqueados=?, estado=?, descripcion_paquete=?, precio_paquete_base=?, margen_deseado_pct=? WHERE id_grupo=?",
        ((form.get("nombre_grupo") or "").strip(), (form.get("origen") or "Monterrey").strip(), upsert_catalogo_destino((form.get("destino") or "").strip()),
         form.get("fecha_salida"), form.get("fecha_regreso"), form.get("moneda") or "MXN", int(form.get("cupos_bloqueados") or 0),
         form.get("estado") or "ACTIVO", (form.get("descripcion_paquete") or "").strip(),
         float(form.get("precio_paquete_base") or 0), float(form.get("margen_deseado_pct") or 0), id_grupo)
    )
    return RedirectResponse(url=f"/grupos/{id_grupo}", status_code=303)


# — Presupuesto —

@app.post("/grupos/{id_grupo}/presupuesto/agregar", response_class=HTMLResponse)
async def presupuesto_agregar(request: Request, id_grupo: int):
    if not usuario_activo(request):
        return HTMLResponse("", status_code=401)
    form = await request.form()
    monto = float(form.get("monto_presupuestado") or 0)
    if monto > 0:
        ejecutar_comando(
            "INSERT INTO grupo_presupuesto (id_grupo, categoria, descripcion, monto_presupuestado) VALUES (?,?,?,?)",
            (id_grupo, form.get("categoria") or "Otro", (form.get("descripcion") or "").strip(), monto)
        )
    return _presupuesto_grupo_html(request, id_grupo)


@app.post("/grupos/{id_grupo}/presupuesto/{id_presupuesto}/editar", response_class=HTMLResponse)
async def presupuesto_editar(request: Request, id_grupo: int, id_presupuesto: int):
    if not usuario_activo(request):
        return HTMLResponse("", status_code=401)
    form = await request.form()
    ejecutar_comando(
        "UPDATE grupo_presupuesto SET categoria=?, descripcion=?, monto_presupuestado=? WHERE id_presupuesto=?",
        (form.get("categoria") or "Otro", (form.get("descripcion") or "").strip(), float(form.get("monto_presupuestado") or 0), id_presupuesto)
    )
    return _presupuesto_grupo_html(request, id_grupo)


@app.post("/grupos/{id_grupo}/presupuesto/{id_presupuesto}/eliminar", response_class=HTMLResponse)
async def presupuesto_eliminar(request: Request, id_grupo: int, id_presupuesto: int):
    if not usuario_activo(request):
        return HTMLResponse("", status_code=401)
    df_pag = obtener_datos("SELECT COALESCE(SUM(monto),0) as t FROM flujo_caja WHERE id_presupuesto=? AND estado='ACTIVO'", (id_presupuesto,))
    if df_pag.empty or float(df_pag.iloc[0]["t"]) <= 0:
        ejecutar_comando("DELETE FROM grupo_presupuesto WHERE id_presupuesto=?", (id_presupuesto,))
    return _presupuesto_grupo_html(request, id_grupo)


# — Logística —

@app.post("/grupos/{id_grupo}/logistica/agregar", response_class=HTMLResponse)
async def logistica_agregar(request: Request, id_grupo: int):
    if not usuario_activo(request):
        return HTMLResponse("", status_code=401)
    form = await request.form()
    tipo = form.get("tipo") or "Otro"
    nombre = (form.get("nombre") or "").strip()
    if nombre:
        if tipo in _UPSERT_CATALOGO_MAP:
            nombre = _UPSERT_CATALOGO_MAP[tipo](nombre)
        ejecutar_comando(
            "INSERT INTO grupo_logistica (id_grupo, tipo, nombre, confirmacion, detalle, fecha_salida, fecha_regreso, hora_ida, hora_vuelta) VALUES (?,?,?,?,?,?,?,?,?)",
            (id_grupo, tipo, nombre, (form.get("confirmacion") or "").strip(), (form.get("detalle") or "").strip(),
             form.get("fecha_salida") or None, form.get("fecha_regreso") or None, form.get("hora_ida") or None, form.get("hora_vuelta") or None)
        )
    return _logistica_grupo_html(request, id_grupo)


@app.post("/grupos/{id_grupo}/logistica/{id_logistica}/editar", response_class=HTMLResponse)
async def logistica_editar(request: Request, id_grupo: int, id_logistica: int):
    if not usuario_activo(request):
        return HTMLResponse("", status_code=401)
    form = await request.form()
    ejecutar_comando(
        "UPDATE grupo_logistica SET nombre=?, confirmacion=?, detalle=?, fecha_salida=?, fecha_regreso=?, hora_ida=?, hora_vuelta=? WHERE id_logistica=?",
        ((form.get("nombre") or "").strip(), (form.get("confirmacion") or "").strip(), (form.get("detalle") or "").strip(),
         form.get("fecha_salida") or None, form.get("fecha_regreso") or None, form.get("hora_ida") or None, form.get("hora_vuelta") or None, id_logistica)
    )
    return _logistica_grupo_html(request, id_grupo)


@app.post("/grupos/{id_grupo}/logistica/{id_logistica}/eliminar", response_class=HTMLResponse)
async def logistica_eliminar(request: Request, id_grupo: int, id_logistica: int):
    if not usuario_activo(request):
        return HTMLResponse("", status_code=401)
    ejecutar_comando("DELETE FROM grupo_logistica WHERE id_logistica=?", (id_logistica,))
    return _logistica_grupo_html(request, id_grupo)


@app.post("/grupos/{id_grupo}/logistica/{id_logistica}/aplicar", response_class=HTMLResponse)
async def logistica_aplicar(request: Request, id_grupo: int, id_logistica: int):
    if not usuario_activo(request):
        return HTMLResponse("", status_code=401)
    form = await request.form()
    ids_reserva = [int(x) for x in form.getlist("id_reserva")]
    usuario = usuario_activo(request)
    df_l = obtener_datos("SELECT tipo, nombre, confirmacion FROM grupo_logistica WHERE id_logistica=?", (id_logistica,))
    if not df_l.empty and df_l.iloc[0]["tipo"] in _LG_COLS_MAP:
        tipo = df_l.iloc[0]["tipo"]; nombre = df_l.iloc[0]["nombre"]; confirmacion = df_l.iloc[0]["confirmacion"]
        col_nom, col_conf = _LG_COLS_MAP[tipo]
        df_grupo = obtener_datos("SELECT nombre_grupo FROM grupos_viaje WHERE id_grupo=?", (id_grupo,))
        nombre_grupo = df_grupo.iloc[0]["nombre_grupo"] if not df_grupo.empty else ""
        for id_r in ids_reserva:
            ejecutar_comando(f"UPDATE reservas SET {col_nom}=?, {col_conf}=? WHERE id_reserva=?", (nombre, confirmacion, id_r))
            registrar_cambio(id_r, "MODIFICACIÓN", f"{tipo} actualizado desde el grupo '{nombre_grupo}': {nombre}" + (f" — Conf: {confirmacion}" if confirmacion else ""), usuario=usuario)
    return _logistica_grupo_html(request, id_grupo)


# — Pago a proveedor del grupo / vincular reserva / prorrateo en bloque —

@app.post("/grupos/{id_grupo}/pago-proveedor/registrar")
async def grupo_pago_proveedor(request: Request, id_grupo: int):
    if not usuario_activo(request):
        return RedirectResponse(url="/login")
    form = await request.form()
    usuario = usuario_activo(request)
    id_presupuesto = int(form.get("id_presupuesto") or 0)
    monto = round(float(form.get("monto") or 0), 2)
    if id_presupuesto and monto > 0:
        df_p = obtener_datos("SELECT categoria FROM grupo_presupuesto WHERE id_presupuesto=?", (id_presupuesto,))
        df_g = obtener_datos("SELECT nombre_grupo, moneda FROM grupos_viaje WHERE id_grupo=?", (id_grupo,))
        categoria = df_p.iloc[0]["categoria"] if not df_p.empty else "Otro"
        nombre_grupo = df_g.iloc[0]["nombre_grupo"] if not df_g.empty else ""
        moneda = df_g.iloc[0]["moneda"] if not df_g.empty else "MXN"
        metodo = form.get("metodo_pago") or "Transferencia"
        ejecutar_comando(
            "INSERT INTO flujo_caja (id_reserva, id_grupo, id_presupuesto, tipo_movimiento, tipo_egreso, categoria, concepto, monto, moneda, fecha_pago, usuario_creador, fecha_creacion, metodo_pago) VALUES (NULL,?,?,'EGRESO','COSTO DIRECTO VIAJE',?,?,?,?,?,?,?,?)",
            (id_grupo, id_presupuesto, f"Pago de {categoria} (Grupo)", f"[{metodo}] Pago proveedor grupo — {nombre_grupo}", monto, moneda, str(now_local().date()), usuario, str(now_local().date()), metodo)
        )
    return RedirectResponse(url=f"/grupos/{id_grupo}", status_code=303)


@app.post("/grupos/{id_grupo}/pago-proveedor/{id_movimiento}/anular", response_class=HTMLResponse)
async def grupo_pago_proveedor_anular(request: Request, id_grupo: int, id_movimiento: int):
    if not usuario_activo(request):
        return HTMLResponse("", status_code=401)
    form = await request.form()
    motivo = (form.get("motivo") or "").strip()
    usuario = usuario_activo(request)
    if not motivo:
        return _presupuesto_grupo_html(request, id_grupo)

    df_mov = obtener_datos(
        "SELECT id_movimiento, monto, concepto, estado FROM flujo_caja "
        "WHERE id_movimiento=? AND id_grupo=? AND tipo_movimiento='EGRESO'",
        (id_movimiento, id_grupo)
    )
    # Candado: si ya está cancelado (doble clic/doble submit) o no pertenece a este
    # grupo, no se vuelve a correr nada — igual que anular_cobro().
    if df_mov.empty or df_mov.iloc[0]["estado"] != "ACTIVO":
        return _presupuesto_grupo_html(request, id_grupo)

    monto_mov = round(float(df_mov.iloc[0]["monto"] or 0), 2)
    concepto_mov = str(df_mov.iloc[0]["concepto"] or "")

    ejecutar_comando(
        "UPDATE flujo_caja SET estado='CANCELADO', motivo_anulacion=? WHERE id_movimiento=?",
        (motivo, id_movimiento)
    )
    ejecutar_comando(
        "INSERT INTO anulaciones_audit (id_movimiento, tipo_movimiento, monto_anulado, usuario_anulo, fecha_anulacion, razon_anulacion, movimiento_original) VALUES (?,?,?,?,?,?,?)",
        (id_movimiento, "EGRESO", monto_mov, usuario, str(now_local().date()), motivo, concepto_mov)
    )
    return _presupuesto_grupo_html(request, id_grupo)


@app.post("/grupos/{id_grupo}/vincular")
async def grupo_vincular_reserva(request: Request, id_grupo: int):
    if not usuario_activo(request):
        return RedirectResponse(url="/login")
    form = await request.form()
    try:
        id_reserva = int(form.get("id_reserva") or 0)
        num_pax = int(form.get("num_pax") or 1)
    except ValueError:
        return RedirectResponse(url=f"/grupos/{id_grupo}", status_code=303)
    df_chk = obtener_datos("SELECT id_reserva FROM reservas WHERE id_reserva=?", (id_reserva,))
    if not df_chk.empty:
        ejecutar_comando("UPDATE reservas SET id_grupo=?, num_pax=? WHERE id_reserva=?", (id_grupo, num_pax, id_reserva))
    return RedirectResponse(url=f"/grupos/{id_grupo}", status_code=303)


@app.post("/grupos/{id_grupo}/aplicar-prorrateo")
async def grupo_aplicar_prorrateo(request: Request, id_grupo: int):
    if not usuario_activo(request):
        return RedirectResponse(url="/login")
    form = await request.form()
    usuario = usuario_activo(request)
    ids_reserva = [int(x) for x in form.getlist("id_reserva")]
    df_g = obtener_datos("SELECT nombre_grupo FROM grupos_viaje WHERE id_grupo=?", (id_grupo,))
    nombre_grupo = df_g.iloc[0]["nombre_grupo"] if not df_g.empty else ""
    for id_r in ids_reserva:
        df_r = obtener_datos("SELECT num_pax FROM reservas WHERE id_reserva=?", (id_r,))
        if df_r.empty: continue
        num_pax = int(df_r.iloc[0]["num_pax"] or 1)
        if aplicar_prorrateo_grupo_a_reserva(id_r, id_grupo, num_pax):
            registrar_cambio(id_r, "COSTOS ACTUALIZADOS", f"Costo actualizado al prorrateo del grupo '{nombre_grupo}' ({num_pax} pax).", usuario=usuario)
    return RedirectResponse(url=f"/grupos/{id_grupo}", status_code=303)


# ─── Cotizaciones ────────────────────────────────────────────────────────────

def _auto_expirar_cotizaciones():
    ejecutar_comando(
        "UPDATE cotizaciones SET estado='EXPIRADA' WHERE estado IN ('PENDIENTE','ENVIADA') AND fecha_vencimiento < DATE('now')"
    )

@app.get("/cotizaciones")
async def cotizaciones_lista(request: Request, q: str = "", estado: str = ""):
    if not usuario_activo(request):
        return RedirectResponse(url="/login")
    _auto_expirar_cotizaciones()
    df = obtener_datos(
        """SELECT cot.id_cotizacion, c.nombre as cliente, cot.destino, cot.fecha_salida,
                  cot.fecha_regreso, cot.venta_total, cot.moneda, cot.estado,
                  cot.fecha_cotizacion, cot.fecha_vencimiento,
                  cot.num_adultos, cot.num_menores, cot.convertida_a_reserva
           FROM cotizaciones cot JOIN clientes c ON cot.id_cliente = c.id_cliente
           ORDER BY cot.id_cotizacion DESC"""
    )
    registros = df.to_dict("records") if not df.empty else []
    return templates.TemplateResponse(request, "cotizaciones.html", ctx(request, {
        "active": "cotizaciones",
        "cotizaciones": registros,
        "q": q,
        "filtro_estado": estado,
        "today": str(now_local().date()),
    }))


@app.get("/cotizaciones/nueva")
async def cotizacion_nueva_form(request: Request):
    if not usuario_activo(request):
        return RedirectResponse(url="/login")
    clientes = obtener_datos("SELECT id_cliente, nombre FROM clientes ORDER BY nombre").to_dict("records")
    hoy = str(now_local().date())
    return templates.TemplateResponse(request, "cotizacion_form.html", ctx(request, {
        "active": "cotizaciones",
        "titulo": "Nueva Cotización",
        "accion": "/cotizaciones/nueva",
        "cot": None,
        "clientes": clientes,
        "today": hoy,
        "error": None,
        "habitaciones": [],
        **ctx_catalogos_cotizacion(),
    }))


@app.post("/cotizaciones/nueva")
async def cotizacion_nueva_post(request: Request):
    if not usuario_activo(request):
        return RedirectResponse(url="/login")
    form = dict(await request.form())
    usuario = usuario_activo(request)
    clientes = obtener_datos("SELECT id_cliente, nombre FROM clientes ORDER BY nombre").to_dict("records")
    hoy = now_local().date()

    new_id, error = crear_cotizacion(form, usuario)
    if error:
        return templates.TemplateResponse(request, "cotizacion_form.html", ctx(request, {
            "active": "cotizaciones", "titulo": "Nueva Cotización", "accion": "/cotizaciones/nueva",
            "cot": form, "clientes": clientes, "today": str(hoy), "error": error,
            "habitaciones": [],
            **ctx_catalogos_cotizacion(),
        }))

    request.session["flash"] = {"tipo": "ok", "texto": f"✅ Cotización COT-{hoy.year}-{new_id:04d} guardada."}
    return RedirectResponse(url=f"/cotizaciones/{new_id}", status_code=303)


@app.get("/cotizaciones/{id_cot}/editar")
async def cotizacion_editar_form(request: Request, id_cot: int):
    if not usuario_activo(request):
        return RedirectResponse(url="/login")
    df = obtener_datos("SELECT * FROM cotizaciones WHERE id_cotizacion=?", (id_cot,))
    if df.empty:
        return RedirectResponse(url="/cotizaciones")
    cot = df.iloc[0].to_dict()
    if cot["estado"] not in ("PENDIENTE", "ENVIADA"):
        request.session["flash"] = {"tipo": "error", "texto": "Solo se pueden editar cotizaciones PENDIENTE o ENVIADA."}
        return RedirectResponse(url=f"/cotizaciones/{id_cot}", status_code=303)
    clientes = obtener_datos("SELECT id_cliente, nombre FROM clientes ORDER BY nombre").to_dict("records")
    df_hab = obtener_datos(
        "SELECT id_habitacion, tipo_habitacion, num_personas, hora_checkin, descripcion FROM habitaciones_cotizacion WHERE id_cotizacion=? ORDER BY id_habitacion",
        (id_cot,)
    )
    return templates.TemplateResponse(request, "cotizacion_form.html", ctx(request, {
        "active": "cotizaciones",
        "titulo": f"Editar Cotización COT-{str(cot['fecha_cotizacion'])[:4]}-{id_cot:04d}",
        "accion": f"/cotizaciones/{id_cot}/editar",
        "cot": cot, "clientes": clientes, "today": str(now_local().date()), "error": None,
        "habitaciones": df_hab.to_dict("records") if not df_hab.empty else [],
        **ctx_catalogos_cotizacion(),
    }))


@app.post("/cotizaciones/{id_cot}/editar")
async def cotizacion_editar_post(request: Request, id_cot: int):
    if not usuario_activo(request):
        return RedirectResponse(url="/login")
    form = dict(await request.form())
    hoy = now_local().date()

    def _f(k): return float(form.get(k) or 0)
    def _i(k): return int(form.get(k) or 0)
    def _s(k): return (form.get(k) or "").strip()

    venta = _f("cobro_vuelos") + _f("cobro_tua") + _f("cobro_hotel") + _f("cobro_traslados") + _f("cobro_tours") + _f("cobro_adicionales")
    costo = _f("costo_vuelos") + _f("costo_tua") + _f("costo_hotel") + _f("costo_traslados") + _f("costo_tours") + _f("costo_adicionales")
    vigencia = _i("dias_vigencia") or 7
    fecha_venc = str(hoy + __import__("datetime").timedelta(days=vigencia))

    # Mismas validaciones que crear_cotizacion() / Streamlit (app.py:3938-3942) —
    # sin esto se podían guardar ediciones con fechas inconsistentes.
    _f_salida, _f_regreso = _s("fecha_salida"), _s("fecha_regreso")
    _f_limite_prov, _f_limite_pago = _s("fecha_limite_proveedor"), _s("fecha_limite_pago")
    _error_edit = None
    if venta <= 0:
        _error_edit = "El total de la cotización debe ser mayor a cero."
    elif _f_salida and _f_regreso and _f_regreso < _f_salida:
        _error_edit = "La fecha de regreso no puede ser anterior a la fecha de salida."
    elif _f_limite_prov and _f_salida and _f_limite_prov > _f_salida:
        _error_edit = "La fecha límite de pago al proveedor no puede ser posterior a la fecha de salida del viaje."
    elif _f_limite_pago and _f_salida and _f_limite_pago > _f_salida:
        _error_edit = "La fecha límite de pago del cliente no puede ser posterior a la fecha de salida del viaje."
    elif _f_limite_pago and _f_limite_prov and _f_limite_pago > _f_limite_prov:
        _error_edit = "La fecha límite de pago del cliente no puede ser posterior a la del proveedor."
    elif form.get("incluye_vuelo") and not (_s("fecha_vuelo_ida") and _s("hora_vuelo_ida") and _s("fecha_vuelo_vuelta") and _s("hora_vuelo_vuelta")):
        _error_edit = "Marcaste que incluye vuelo — debes capturar fecha y hora de ida y vuelta."
    if _error_edit:
        request.session["flash"] = {"tipo": "error", "texto": f"⚠️ {_error_edit}"}
        return RedirectResponse(url=f"/cotizaciones/{id_cot}/editar", status_code=303)

    # Si la cotización ya había sido enviada al cliente, editarla la regresa a
    # PENDIENTE para forzar un reenvío — igual que Streamlit (app.py:3933-3935):
    # de lo contrario queda marcada "ENVIADA" con datos que el cliente nunca vio.
    _estado_actual = obtener_datos("SELECT estado FROM cotizaciones WHERE id_cotizacion=?", (id_cot,))
    _nuevo_estado = "PENDIENTE" if (not _estado_actual.empty and _estado_actual.iloc[0]["estado"] == "ENVIADA") else None

    v_nombre_hotel = upsert_catalogo_hotel(_s("nombre_hotel"))
    v_aerolinea = upsert_catalogo_aerolinea(_s("aerolinea"))
    v_mayorista = upsert_catalogo_mayorista(_s("mayorista"))
    v_prov_traslados = upsert_catalogo_proveedor_traslados(_s("proveedor_traslados"))
    v_prov_tours = upsert_catalogo_proveedor_tours(_s("proveedor_tours"))
    v_equipaje = upsert_catalogo_equipaje(_s("detalle_equipaje"))
    v_hotel_op2 = upsert_catalogo_hotel(_s("hotel_op2_nombre"))
    v_hotel_op3 = upsert_catalogo_hotel(_s("hotel_op3_nombre"))

    ejecutar_comando("""
        UPDATE cotizaciones SET
            id_cliente=?, destino=?, origen=?, fecha_salida=?, fecha_regreso=?, moneda=?,
            num_adultos=?, num_menores=?,
            incluye_vuelo=?, incluye_tua=?, incluye_hotel=?, incluye_traslado=?,
            cobro_vuelos=?, cobro_tua=?, cobro_hotel=?, cobro_traslados=?, cobro_tours=?, cobro_adicionales=?,
            especificar_adicionales=?, venta_total=?,
            nombre_hotel=?, aerolinea=?, mayorista=?, proveedor_traslados=?, proveedor_tours=?,
            anticipo_requerido=?, tipo_plan=?, dia_mensual=?,
            fecha_limite_pago=?, fecha_limite_proveedor=?, notas=?, dias_vigencia=?, fecha_vencimiento=?,
            costo_vuelos=?, costo_tua=?, costo_hotel=?, costo_traslados=?, costo_tours=?, costo_adicionales=?,
            costo_total=?, utilidad_proyectada=?,
            hotel_op2_nombre=?, hotel_op2_cobro=?, hotel_op2_costo=?,
            hotel_op3_nombre=?, hotel_op3_cobro=?, hotel_op3_costo=?,
            fecha_vuelo_ida=?, hora_vuelo_ida=?, fecha_vuelo_vuelta=?, hora_vuelo_vuelta=?, detalle_equipaje=?
        WHERE id_cotizacion=?
    """, (
        _s("id_cliente"), upsert_catalogo_destino(_s("destino")), _s("origen") or "Monterrey",
        _s("fecha_salida"), _s("fecha_regreso"), form.get("moneda") or "MXN",
        _i("num_adultos") or 1, _i("num_menores"),
        1 if form.get("incluye_vuelo") else 0, 1 if form.get("incluye_tua") else 0,
        1, 1 if form.get("incluye_traslado") else 0,
        _f("cobro_vuelos"), _f("cobro_tua"), _f("cobro_hotel"), _f("cobro_traslados"),
        _f("cobro_tours"), _f("cobro_adicionales"), _s("especificar_adicionales") or None, venta,
        v_nombre_hotel or None, v_aerolinea or None, v_mayorista or None,
        v_prov_traslados or None, v_prov_tours or None,
        _f("anticipo_requerido"), form.get("tipo_plan") or "Sin Plan (Libre)", _i("dia_mensual") or 5,
        _s("fecha_limite_pago") or None, _s("fecha_limite_proveedor") or None,
        _s("notas") or None, vigencia, fecha_venc,
        _f("costo_vuelos"), _f("costo_tua"), _f("costo_hotel"), _f("costo_traslados"),
        _f("costo_tours"), _f("costo_adicionales"), costo, venta - costo,
        v_hotel_op2 or '', _f("hotel_op2_cobro"), _f("hotel_op2_costo"),
        v_hotel_op3 or '', _f("hotel_op3_cobro"), _f("hotel_op3_costo"),
        _s("fecha_vuelo_ida") or '', _s("hora_vuelo_ida") or '',
        _s("fecha_vuelo_vuelta") or '', _s("hora_vuelo_vuelta") or '',
        v_equipaje or None,
        id_cot,
    ))
    if _nuevo_estado:
        ejecutar_comando("UPDATE cotizaciones SET estado=? WHERE id_cotizacion=?", (_nuevo_estado, id_cot))

    ejecutar_comando("DELETE FROM habitaciones_cotizacion WHERE id_cotizacion=?", (id_cot,))
    try:
        n_hab = int(form.get("hab_n") or 0)
    except Exception:
        n_hab = 0
    for i in range(1, n_hab + 1):
        h_tipo = (form.get(f"hab_tipo_{i}") or "").strip()
        if not h_tipo:
            continue
        try: h_pers = int(form.get(f"hab_personas_{i}") or 1)
        except Exception: h_pers = 1
        ejecutar_comando(
            "INSERT INTO habitaciones_cotizacion (id_cotizacion, tipo_habitacion, num_personas, hora_checkin, descripcion) VALUES (?,?,?,?,?)",
            (id_cot, h_tipo, h_pers, (form.get(f"hab_checkin_{i}") or "15:00").strip() or "15:00", (form.get(f"hab_descripcion_{i}") or "").strip() or None)
        )

    request.session["flash"] = {"tipo": "ok", "texto": "✅ Cotización actualizada."}
    return RedirectResponse(url=f"/cotizaciones/{id_cot}", status_code=303)


@app.post("/cotizaciones/{id_cot}/estado")
async def cotizacion_cambiar_estado(request: Request, id_cot: int):
    if not usuario_activo(request):
        return RedirectResponse(url="/login")
    form = dict(await request.form())
    nuevo_estado = form.get("estado", "").upper()
    if nuevo_estado in ("ENVIADA", "ACEPTADA", "RECHAZADA"):
        ejecutar_comando("UPDATE cotizaciones SET estado=? WHERE id_cotizacion=?", (nuevo_estado, id_cot))
    return RedirectResponse(url=f"/cotizaciones/{id_cot}", status_code=303)


@app.get("/cotizaciones/{id_cot}/pdf")
async def cotizacion_pdf(request: Request, id_cot: int):
    if not usuario_activo(request):
        return RedirectResponse(url="/login")
    from fastapi.responses import Response
    from pdf_engine import generar_cotizacion_pdf
    from datetime import datetime as _dt, timedelta

    df_cot = obtener_datos("SELECT * FROM cotizaciones WHERE id_cotizacion=?", (id_cot,))
    if df_cot.empty:
        return RedirectResponse(url="/cotizaciones")
    cot = df_cot.iloc[0].to_dict()

    df_cli = obtener_datos(
        "SELECT c.nombre, c.telefono, c.email FROM clientes c JOIN cotizaciones co ON c.id_cliente=co.id_cliente WHERE co.id_cotizacion=?",
        (id_cot,)
    )
    if df_cli.empty:
        return RedirectResponse(url=f"/cotizaciones/{id_cot}")
    cli = df_cli.iloc[0]

    # Reconstruir plan de pagos
    plan_fechas = []
    if cot["tipo_plan"] != "Sin Plan (Libre)" and cot.get("fecha_limite_pago"):
        try:
            saldo = float(cot["venta_total"]) - float(cot["anticipo_requerido"] or 0)
            f_limite = _dt.strptime(str(cot["fecha_limite_pago"])[:10], "%Y-%m-%d").date()
            hoy = now_local().date()
            fechas = _calcular_plan_fechas(cot["tipo_plan"], int(cot["dia_mensual"] or 5), hoy, f_limite)
            if fechas:
                n_pdf = len(fechas)
                monto_base_pdf = round(saldo / n_pdf, 2)
                residuo_pdf = round(saldo - monto_base_pdf * n_pdf, 2)
                plan_fechas = [(f.strftime("%Y-%m-%d"), monto_base_pdf + (residuo_pdf if i == 0 else 0))
                               for i, f in enumerate(fechas)]
        except Exception:
            pass

    df_vuelos_pdf = obtener_datos(
        "SELECT numero_tramo, aerolinea, numero_vuelo, origen, destino, fecha, hora FROM vuelos_cotizacion WHERE id_cotizacion=? ORDER BY numero_tramo",
        (id_cot,)
    )
    df_hoteles_pdf = obtener_datos(
        "SELECT numero_orden, nombre_hotel, ciudad_destino, fecha_checkin, fecha_checkout FROM hoteles_cotizacion WHERE id_cotizacion=? ORDER BY numero_orden",
        (id_cot,)
    )
    pdf_bytes = generar_cotizacion_pdf(
        cot, cli["nombre"], cli["telefono"], cli["email"],
        plan_fechas, "static/img/logo.png",
        operador=cot.get("usuario_creador") or usuario_activo(request),
        vuelos_df=df_vuelos_pdf, hoteles_df=df_hoteles_pdf
    )
    folio = f"COT-{str(cot['fecha_cotizacion'])[:4]}-{id_cot:04d}"
    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={"Content-Disposition": f'inline; filename="{folio}_{cot["destino"]}.pdf"'}
    )


@app.post("/cotizaciones/{id_cot}/convertir")
async def cotizacion_convertir(request: Request, id_cot: int):
    if not usuario_activo(request):
        return RedirectResponse(url="/login")
    usuario = usuario_activo(request)

    form_conv = dict(await request.form())
    id_reserva, error = convertir_cotizacion(id_cot, form_conv.get("hotel_opcion_elegida") or 1, usuario)
    if error:
        return RedirectResponse(url="/cotizaciones")

    request.session["flash"] = {"tipo": "ok", "texto": f"✅ Cotización convertida — Reserva #{id_reserva} creada con plan de pagos."}
    return RedirectResponse(url=f"/bitacora/{id_reserva}", status_code=303)


@app.get("/cotizaciones/{id_cot}")
async def cotizacion_detalle(request: Request, id_cot: int):
    if not usuario_activo(request):
        return RedirectResponse(url="/login")
    _auto_expirar_cotizaciones()
    df = obtener_datos(
        """SELECT cot.*, c.nombre as nombre_cliente, c.telefono, c.email
           FROM cotizaciones cot JOIN clientes c ON cot.id_cliente = c.id_cliente
           WHERE cot.id_cotizacion=?""",
        (id_cot,)
    )
    if df.empty:
        return RedirectResponse(url="/cotizaciones")
    cot = df.iloc[0].to_dict()

    # Reconstruir preview del plan
    plan_preview = []
    plans_por_opcion = []
    from datetime import datetime as _dt
    anticipo = float(cot.get("anticipo_requerido") or 0)
    _es_multi = float(cot.get("hotel_op2_cobro") or 0) > 0

    def _construir_plan(total_venta):
        filas = []
        if cot["tipo_plan"] != "Sin Plan (Libre)" and cot.get("fecha_limite_pago"):
            try:
                saldo = total_venta - anticipo
                f_limite = _dt.strptime(str(cot["fecha_limite_pago"])[:10], "%Y-%m-%d").date()
                hoy_p = now_local().date()
                fechas = _calcular_plan_fechas(cot["tipo_plan"], int(cot["dia_mensual"] or 5), hoy_p, f_limite)
                if fechas:
                    n_prev = len(fechas)
                    monto_base_prev = round(saldo / n_prev, 2)
                    residuo_prev = round(saldo - monto_base_prev * n_prev, 2)
                else:
                    monto_base_prev = round(saldo, 2)
                    residuo_prev = 0
                if anticipo > 0:
                    filas.append({"concepto": "Anticipo al confirmar", "fecha": "Al firmar", "monto": anticipo})
                for i, f in enumerate(fechas):
                    monto_fila = monto_base_prev + (residuo_prev if i == 0 else 0)
                    filas.append({"concepto": f"Parcialidad #{i+1}", "fecha": str(f), "monto": monto_fila})
            except Exception:
                pass
        return filas

    _base = (float(cot.get("cobro_vuelos") or 0) + float(cot.get("cobro_tua") or 0) +
             float(cot.get("cobro_traslados") or 0) + float(cot.get("cobro_tours") or 0) +
             float(cot.get("cobro_adicionales") or 0))

    if _es_multi:
        for _lbl, _cobro_h in [
            ("Opción 1", float(cot.get("cobro_hotel") or 0)),
            ("Opción 2", float(cot.get("hotel_op2_cobro") or 0)),
            ("Opción 3", float(cot.get("hotel_op3_cobro") or 0)),
        ]:
            if _cobro_h > 0 or _lbl == "Opción 1":
                _total = _base + _cobro_h
                _plan = _construir_plan(_total)
                if _plan:
                    plans_por_opcion.append((_lbl, _total, _plan))
        plan_preview = plans_por_opcion[0][2] if plans_por_opcion else []
    else:
        plan_preview = _construir_plan(float(cot["venta_total"]))

    folio = f"COT-{str(cot['fecha_cotizacion'])[:4]}-{id_cot:04d}"
    df_vuelos_cot = obtener_datos(
        "SELECT id_vuelo, numero_tramo, aerolinea, numero_vuelo, origen, destino, fecha, hora, localizador "
        "FROM vuelos_cotizacion WHERE id_cotizacion=? ORDER BY numero_tramo, id_vuelo",
        (id_cot,)
    )
    df_hoteles_cot = obtener_datos(
        "SELECT id_hotel_itin, numero_orden, ciudad_destino, nombre_hotel, localizador, fecha_checkin, fecha_checkout "
        "FROM hoteles_cotizacion WHERE id_cotizacion=? ORDER BY numero_orden, id_hotel_itin",
        (id_cot,)
    )
    cot_activa = cot.get("estado") not in ("ACEPTADA", "RECHAZADA", "EXPIRADA")
    return templates.TemplateResponse(request, "cotizacion_detalle.html", ctx(request, {
        "active": "cotizaciones",
        "cot": cot,
        "folio": folio,
        "plan_preview": plan_preview,
        "plans_por_opcion": plans_por_opcion if _es_multi else [],
        "today": str(now_local().date()),
        "vuelos_cot": df_vuelos_cot.to_dict("records"),
        "hoteles_cot": df_hoteles_cot.to_dict("records"),
        "habitaciones_por_hotel": _habitaciones_por_hotel("habitaciones_cotizacion", "id_cotizacion", id_cot),
        "cot_activa": cot_activa,
    }))


# ─── Acompañantes del cliente (HTMX partials) ────────────────────────────────

def _acompanantes_html(request, id_cliente: str):
    df = obtener_datos(
        "SELECT id_acompanante, nombre, fecha_nacimiento, parentesco FROM acompanantes_cliente WHERE id_cliente=? ORDER BY nombre",
        (id_cliente,)
    )
    return templates.TemplateResponse(request, "acompanantes_section.html", {
        "request": request,
        "id_cliente": id_cliente,
        "acompanantes": df.to_dict("records"),
    })


@app.post("/clientes/{id_cliente}/acompanantes/agregar", response_class=HTMLResponse)
async def acompanante_agregar(request: Request, id_cliente: str):
    if not usuario_activo(request):
        return HTMLResponse("", status_code=401)
    form = await request.form()
    nombre = (form.get("nombre") or "").strip()
    if nombre:
        ejecutar_comando(
            "INSERT INTO acompanantes_cliente (id_cliente, nombre, fecha_nacimiento, parentesco) VALUES (?,?,?,?)",
            (id_cliente, nombre, form.get("fecha_nacimiento") or None, form.get("parentesco") or None)
        )
    return _acompanantes_html(request, id_cliente)


@app.post("/clientes/{id_cliente}/acompanantes/{id_acompanante}/eliminar", response_class=HTMLResponse)
async def acompanante_eliminar(request: Request, id_cliente: str, id_acompanante: int):
    if not usuario_activo(request):
        return HTMLResponse("", status_code=401)
    ejecutar_comando("DELETE FROM acompanantes_cliente WHERE id_acompanante=? AND id_cliente=?",
                     (id_acompanante, id_cliente))
    return _acompanantes_html(request, id_cliente)


# ─── Clientes ────────────────────────────────────────────────────────────────

def _generar_id_cliente() -> str:
    df = obtener_datos("SELECT MAX(CAST(SUBSTR(id_cliente,4) AS INTEGER)) as n FROM clientes WHERE id_cliente LIKE 'AC-%'")
    n = int(df.iloc[0]["n"]) if not df.empty and df.iloc[0]["n"] else 0
    return f"AC-{n+1:03d}"

def _formatear_tel(raw: str) -> str:
    digits = "".join(c for c in raw if c.isdigit())
    if len(digits) == 10:
        return f"{digits[:3]}-{digits[3:6]}-{digits[6:]}"
    return raw.strip()

def _clientes_rows_html(request, q: str = ""):
    filtro = f"%{q}%"
    df = obtener_datos(
        """SELECT c.id_cliente, c.nombre, c.telefono, c.email, c.fecha_nacimiento, c.codigo_pais,
                  COUNT(r.id_reserva) as total_viajes,
                  SUM(CASE WHEN r.estado='ACTIVO' THEN 1 ELSE 0 END) as viajes_activos
           FROM clientes c
           LEFT JOIN reservas r ON r.id_cliente = c.id_cliente AND r.estado != 'CANCELADO'
           WHERE c.nombre LIKE ? OR c.telefono LIKE ? OR c.email LIKE ? OR c.id_cliente LIKE ?
           GROUP BY c.id_cliente
           ORDER BY c.nombre ASC""",
        (filtro, filtro, filtro, filtro)
    )
    return templates.TemplateResponse(request, "clientes_rows.html", {
        "request": request,
        "clientes": df.to_dict("records"),
        "q": q,
    })


@app.get("/clientes")
async def clientes_lista(request: Request):
    if not usuario_activo(request):
        return RedirectResponse(url="/login")
    resp = _clientes_rows_html(request)
    clientes = resp.context["clientes"]
    return templates.TemplateResponse(request, "clientes.html", ctx(request, {
        "active": "clientes",
        "clientes": clientes,
        "q": "",
    }))


@app.get("/clientes/rows")
async def clientes_rows(request: Request, q: str = ""):
    if not usuario_activo(request):
        return HTMLResponse("", status_code=401)
    return _clientes_rows_html(request, q)


@app.get("/clientes/exportar")
async def clientes_exportar(request: Request):
    if not usuario_activo(request):
        return RedirectResponse(url="/login")
    hoy = now_local().date()
    df = obtener_datos(
        "SELECT id_cliente, nombre, codigo_pais, telefono, email, fecha_nacimiento "
        "FROM clientes ORDER BY nombre"
    )
    df.columns = ["ID Cliente", "Nombre Completo", "Código País", "Teléfono", "Correo", "Fecha Nacimiento"]
    return _excel_response(df, f"Clientes_{hoy}.xlsx")


@app.get("/clientes/nuevo")
async def cliente_nuevo_form(request: Request):
    if not usuario_activo(request):
        return RedirectResponse(url="/login")
    return templates.TemplateResponse(request, "cliente_form.html", ctx(request, {
        "active": "clientes",
        "titulo": "Nuevo Cliente",
        "accion": "/clientes/nuevo",
        "cliente": None,
        "next_id": _generar_id_cliente(),
        "error": None,
        "aviso_dup": None,
    }))


@app.post("/clientes/nuevo")
async def cliente_nuevo_post(request: Request):
    if not usuario_activo(request):
        return RedirectResponse(url="/login")
    usuario = usuario_activo(request)
    form = dict(await request.form())
    forzar = form.get("forzar") == "1"

    # Detección de duplicados (solo si no viene confirmado) — mismo criterio que Asistente_IA
    if not forzar:
        avisos_raw = buscar_duplicados_cliente(form.get("nombre", ""), form.get("telefono", ""), form.get("email", ""))
        if avisos_raw:
            iconos = {"Teléfono": "📞", "Email": "✉️", "Nombre": "👤"}
            avisos = [f"{iconos.get(a.split(' ')[0], '•')} {_esc(a)}" for a in avisos_raw]
            return templates.TemplateResponse(request, "cliente_form.html", ctx(request, {
                "active": "clientes", "titulo": "Nuevo Cliente", "accion": "/clientes/nuevo",
                "cliente": form, "next_id": _generar_id_cliente(),
                "error": None, "aviso_dup": avisos,
            }))

    nuevo_id, error = crear_cliente(form, usuario)
    if error:
        return templates.TemplateResponse(request, "cliente_form.html", ctx(request, {
            "active": "clientes", "titulo": "Nuevo Cliente", "accion": "/clientes/nuevo",
            "cliente": form, "next_id": _generar_id_cliente(),
            "error": error, "aviso_dup": None,
        }))
    request.session["flash"] = {"tipo": "ok", "texto": f"✅ Cliente {form.get('nombre','').strip()} registrado con ID {nuevo_id}."}
    return RedirectResponse(url=f"/clientes/{nuevo_id}", status_code=303)


@app.get("/clientes/{id_cliente}/editar")
async def cliente_editar_form(request: Request, id_cliente: str):
    if not usuario_activo(request):
        return RedirectResponse(url="/login")
    df = obtener_datos("SELECT * FROM clientes WHERE id_cliente=?", (id_cliente,))
    if df.empty:
        return RedirectResponse(url="/clientes")
    return templates.TemplateResponse(request, "cliente_form.html", ctx(request, {
        "active": "clientes",
        "titulo": f"Editar Cliente — {id_cliente}",
        "accion": f"/clientes/{id_cliente}/editar",
        "cliente": df.iloc[0].to_dict(),
        "next_id": None,
        "error": None,
        "aviso_dup": None,
    }))


@app.post("/clientes/{id_cliente}/editar")
async def cliente_editar_post(request: Request, id_cliente: str):
    if not usuario_activo(request):
        return RedirectResponse(url="/login")
    form = dict(await request.form())

    nombre = (form.get("nombre") or "").strip()
    tel_raw = (form.get("telefono") or "").strip()
    email = (form.get("email") or "").strip().lower()
    fnac = (form.get("fecha_nacimiento") or "").strip() or None
    cod_pais = form.get("codigo_pais") or "+52"

    if not nombre:
        df = obtener_datos("SELECT * FROM clientes WHERE id_cliente=?", (id_cliente,))
        return templates.TemplateResponse(request, "cliente_form.html", ctx(request, {
            "active": "clientes",
            "titulo": f"Editar Cliente — {id_cliente}",
            "accion": f"/clientes/{id_cliente}/editar",
            "cliente": df.iloc[0].to_dict() if not df.empty else form,
            "next_id": None,
            "error": "El nombre es obligatorio.", "aviso_dup": None,
        }))

    tel = _formatear_tel(tel_raw)
    digits = "".join(c for c in tel_raw if c.isdigit())
    if tel_raw and len(digits) != 10:
        df = obtener_datos("SELECT * FROM clientes WHERE id_cliente=?", (id_cliente,))
        return templates.TemplateResponse(request, "cliente_form.html", ctx(request, {
            "active": "clientes",
            "titulo": f"Editar Cliente — {id_cliente}",
            "accion": f"/clientes/{id_cliente}/editar",
            "cliente": df.iloc[0].to_dict() if not df.empty else form,
            "next_id": None,
            "error": "El teléfono debe tener exactamente 10 dígitos.", "aviso_dup": None,
        }))

    ejecutar_comando(
        "UPDATE clientes SET nombre=?, telefono=?, email=?, fecha_nacimiento=?, codigo_pais=? WHERE id_cliente=?",
        (nombre, tel or None, email or None, fnac, cod_pais, id_cliente)
    )
    request.session["flash"] = {"tipo": "ok", "texto": f"✅ Datos de {nombre} actualizados."}
    return RedirectResponse(url=f"/clientes/{id_cliente}", status_code=303)


@app.get("/clientes/{id_cliente}")
async def cliente_detalle(request: Request, id_cliente: str):
    if not usuario_activo(request):
        return RedirectResponse(url="/login")
    df = obtener_datos("SELECT * FROM clientes WHERE id_cliente=?", (id_cliente,))
    if df.empty:
        return RedirectResponse(url="/clientes")
    cliente = df.iloc[0].to_dict()

    df_reservas = obtener_datos(
        """SELECT r.id_reserva, r.destino, r.fecha_salida, r.fecha_regreso,
                  r.estado, r.moneda, r.venta_total, r.cobrado_cliente,
                  (r.venta_total - r.cobrado_cliente) as saldo_vivo
           FROM reservas r
           WHERE r.id_cliente = ?
           ORDER BY r.fecha_salida DESC""",
        (id_cliente,)
    )
    reservas = df_reservas.to_dict("records") if not df_reservas.empty else []

    # Estadísticas
    total_viajes = len([r for r in reservas if r["estado"] != "CANCELADO"])
    viajes_activos = len([r for r in reservas if r["estado"] == "ACTIVO"])
    gasto_total = sum(float(r["venta_total"] or 0) for r in reservas if r["estado"] != "CANCELADO")
    deuda_activa = sum(float(r["saldo_vivo"] or 0) for r in reservas if r["estado"] == "ACTIVO" and float(r["saldo_vivo"] or 0) > 0.01)

    hoy = now_local().date()
    edad = None
    if cliente.get("fecha_nacimiento"):
        try:
            from datetime import datetime as _dt
            fnac = _dt.strptime(str(cliente["fecha_nacimiento"])[:10], "%Y-%m-%d").date()
            edad = hoy.year - fnac.year - ((hoy.month, hoy.day) < (fnac.month, fnac.day))
        except Exception:
            pass

    df_ac = obtener_datos(
        "SELECT id_acompanante, nombre, fecha_nacimiento, parentesco FROM acompanantes_cliente WHERE id_cliente=? ORDER BY nombre",
        (id_cliente,)
    )
    return templates.TemplateResponse(request, "cliente_detalle.html", ctx(request, {
        "active": "clientes",
        "cliente": cliente,
        "reservas": reservas,
        "acompanantes": df_ac.to_dict("records"),
        "total_viajes": total_viajes,
        "viajes_activos": viajes_activos,
        "gasto_total": gasto_total,
        "deuda_activa": deuda_activa,
        "edad": edad,
        "today": str(hoy),
    }))


# ─── Libro Diario / Flujo de Caja ────────────────────────────────────────────

def _retenido_sin_aplicar_total(moneda):
    """Suma, sobre TODOS los itinerarios ACTIVOS en la moneda dada, cuánto dinero de
    clientes ya se cobró pero todavía no se le ha pagado a proveedores — snapshot a
    hoy, no depende de ningún filtro de período. Misma fórmula que el 'Retenido sin
    Aplicar' del export de Bitácora."""
    # Reservas de grupo (id_grupo NOT NULL) excluidas: su pago a proveedor se controla
    # a nivel de grupo (/grupos/{id}), no por id_reserva — incluirlas aquí mostraría
    # "retenido" de forma falsa (el costo nunca se paga contra su propio id_reserva).
    df_res_act = obtener_datos(
        "SELECT id_reserva, cobrado_cliente, costo_total FROM reservas WHERE estado='ACTIVO' AND moneda=? AND id_grupo IS NULL",
        (moneda,)
    )
    if df_res_act.empty:
        return 0.0
    df_eg_prov = obtener_datos(
        "SELECT id_reserva, categoria, SUM(monto) as total FROM flujo_caja "
        "WHERE tipo_movimiento='EGRESO' AND estado='ACTIVO' AND id_reserva IS NOT NULL "
        "GROUP BY id_reserva, categoria"
    )
    df_extras_cos = obtener_datos("SELECT id_reserva, SUM(monto_costo_proveedor) as costo FROM extras_viaje GROUP BY id_reserva")
    cats_prov = {"Pago de Paquete (Mayorista)", "Pago de Vuelo (Proveedor)", "Pago de TUA (Impuesto)",
                 "Pago de Hotel (Proveedor)", "Pago de Traslado (Proveedor)", "Pago de Tours (Proveedor)",
                 "Pago de Adicionales (Proveedor)"}
    pagado_prov, comisiones = {}, {}
    if not df_eg_prov.empty:
        for _, r in df_eg_prov.iterrows():
            idr = int(r["id_reserva"])
            if r["categoria"] in cats_prov:
                pagado_prov[idr] = pagado_prov.get(idr, 0.0) + float(r["total"])
            elif r["categoria"] == "Comisiones Bancarias":
                comisiones[idr] = comisiones.get(idr, 0.0) + float(r["total"])
    extras_costo = {int(r["id_reserva"]): float(r["costo"] or 0.0) for _, r in df_extras_cos.iterrows()} if not df_extras_cos.empty else {}
    total = 0.0
    for _, r in df_res_act.iterrows():
        idr = int(r["id_reserva"])
        cobrado = float(r["cobrado_cliente"] or 0.0)
        costo_full = float(r["costo_total"] or 0.0) + extras_costo.get(idr, 0.0)
        pagado = pagado_prov.get(idr, 0.0)
        comi = comisiones.get(idr, 0.0)
        deuda = max(0.0, costo_full - comi - pagado)
        retenido_bruto = cobrado - pagado - comi
        utilidad_cobrada = max(0.0, retenido_bruto - deuda)
        total += round(retenido_bruto - utilidad_cobrada, 2)
    return round(total, 2)


@app.post("/libro-diario/credito-aerolinea/ajustar")
async def credito_aerolinea_ajustar(request: Request):
    """Ajuste manual del saldo de crédito con la aerolínea — abierto a cualquier
    usuario logueado (no amarrado a admin/una persona en particular), pero siempre
    requiere motivo + checkbox de confirmación, y queda registrado con
    usuario_creador — el candado real es que nunca es silencioso, no que esté
    restringido a un rol."""
    if not usuario_activo(request):
        return RedirectResponse(url="/login")
    form = await request.form()
    tipo   = (form.get("tipo") or "").strip()
    motivo = (form.get("motivo") or "").strip()
    confirmar = (form.get("confirmar") or "") == "SI"
    try:
        monto = float(form.get("monto") or 0)
    except Exception:
        monto = 0.0
    if confirmar and motivo and monto > 0 and tipo in ("sumar", "restar"):
        _credito_aerolinea_ajustar_manual(tipo, monto, motivo, usuario_activo(request))
    return RedirectResponse(url="/libro-diario", status_code=303)


# ─── Corte de Caja: en qué cuenta/persona quedó cada peso, y cuadre contra
# el Libro Diario (custodia del dinero) ────────────────────────────────────
def _saldos_por_cuenta():
    df = obtener_datos(
        "SELECT cuenta_destino, tipo_movimiento, COALESCE(SUM(monto),0) as total "
        "FROM flujo_caja WHERE estado='ACTIVO' AND moneda='MXN' "
        "GROUP BY cuenta_destino, tipo_movimiento"
    )
    saldos = {cta: 0.0 for cta in CUENTAS_DESTINO}
    saldos[None] = 0.0  # "Sin clasificar" — movimientos históricos, o con un valor
                         # de un catálogo viejo que ya no existe.
    for _, r in df.iterrows():
        cta_raw = r["cuenta_destino"] if pd.notna(r["cuenta_destino"]) else None
        cta = cta_raw if cta_raw in CUENTAS_DESTINO else None
        signo = 1 if r["tipo_movimiento"] == "INGRESO" else -1
        saldos[cta] = saldos.get(cta, 0.0) + signo * float(r["total"])
    saldos = {k: round(v, 2) for k, v in saldos.items()}

    bancos = ["Banco (Cuenta 1)", "Banco (Cuenta 2)"]
    personas = OPERADORAS
    total_general = round(sum(saldos[c] for c in CUENTAS_DESTINO) + saldos[None], 2)

    # Cotejo contra Libro Diario: independiente del desglose por cuenta, "todo lo que
    # ha entrado" menos "todo lo que ya se aplicó/pagó" (todo el histórico, sin filtro
    # de período) SIEMPRE debe cuadrar exacto con total_general de arriba — es la
    # misma tabla sumada de dos formas distintas. Si algún día no cuadra, es señal de
    # un bug real (un movimiento que se está sumando distinto en un lado que en otro).
    df_tot = obtener_datos(
        "SELECT tipo_movimiento, COALESCE(SUM(monto),0) as total FROM flujo_caja "
        "WHERE estado='ACTIVO' AND moneda='MXN' GROUP BY tipo_movimiento"
    )
    ing_hist = float(df_tot.loc[df_tot["tipo_movimiento"] == "INGRESO", "total"].sum()) if not df_tot.empty else 0.0
    egr_hist = float(df_tot.loc[df_tot["tipo_movimiento"] == "EGRESO", "total"].sum()) if not df_tot.empty else 0.0
    cotejo_total = round(ing_hist - egr_hist, 2)
    diferencia = round(total_general - cotejo_total, 2)

    # Margen de tolerancia: $100 por caja/corte antes de considerarlo un problema real
    # (redondeos, centavos de comisión, etc. no deberían disparar una alerta roja).
    # Amarillo = sobra dinero, rojo = falta, verde = cuadra.
    MARGEN_ERROR_CAJA = 100.0
    if abs(diferencia) <= MARGEN_ERROR_CAJA:
        cotejo_estado = "cuadra"
    elif diferencia > 0:
        cotejo_estado = "sobra"
    else:
        cotejo_estado = "falta"

    return {
        "detalle": [(c, saldos[c]) for c in CUENTAS_DESTINO],
        "sin_clasificar": saldos[None],
        "total_bancos": round(sum(saldos[c] for c in bancos), 2),
        "total_efectivo": round(sum(saldos[c] for c in personas), 2),
        "total_general": total_general,
        "total_ingresos_historico": round(ing_hist, 2),
        "total_egresos_historico": round(egr_hist, 2),
        "cotejo_diferencia": diferencia,
        "cotejo_estado": cotejo_estado,
    }


@app.get("/corte-caja", response_class=HTMLResponse)
async def corte_caja_view(request: Request):
    if not usuario_activo(request):
        return RedirectResponse(url="/login")
    df_mov = obtener_datos(
        "SELECT id_movimiento, tipo_movimiento, categoria, concepto, monto, cuenta_destino, "
        "fecha_pago, usuario_creador, estado FROM flujo_caja "
        "WHERE categoria IN ('Traspaso entre Cuentas','Ajuste de Caja') ORDER BY id_movimiento DESC LIMIT 40"
    )
    flash = request.session.pop("flash", None)
    return templates.TemplateResponse(request, "corte_caja.html", ctx(request, {
        "active": "corte_caja",
        "saldos": _saldos_por_cuenta(),
        "cuentas_destino": CUENTAS_DESTINO,
        "traspasos": df_mov.to_dict("records") if not df_mov.empty else [],
        "today": str(now_local().date()),
        "flash": flash,
    }))


@app.post("/corte-caja/traspaso", response_class=HTMLResponse)
async def corte_caja_traspaso(request: Request):
    if not usuario_activo(request):
        return RedirectResponse(url="/login")
    form = dict(await request.form())
    usuario = usuario_activo(request)
    hoy = str(now_local().date())

    origen  = (form.get("cuenta_origen") or "").strip()
    destino = (form.get("cuenta_destino") or "").strip()
    motivo  = (form.get("motivo") or "").strip()
    fecha   = (form.get("fecha") or hoy).strip()
    try:
        monto = float(form.get("monto") or 0)
    except Exception:
        monto = 0.0

    if monto > 0 and origen and destino and origen != destino:
        concepto_base = f"Traspaso {origen} → {destino}" + (f" — {motivo}" if motivo else "")
        # Dos movimientos ligados (EGRESO en origen + INGRESO en destino): el total del
        # negocio no cambia, solo se mueve de dónde a dónde — mismo patrón que la comisión
        # bancaria automática (id_movimiento_vinculado + last_insert_rowid() en la misma
        # transacción) para poder anular ambos juntos si se registró mal.
        ejecutar_transaccion([
            ("INSERT INTO flujo_caja (tipo_movimiento, tipo_egreso, categoria, concepto, monto, moneda, "
             "fecha_pago, cuenta_destino, estado, usuario_creador, fecha_creacion) "
             "VALUES ('EGRESO','TRASPASO INTERNO','Traspaso entre Cuentas',?,?,'MXN',?,?,'ACTIVO',?,?)",
             (concepto_base, monto, fecha, origen, usuario, now_local().isoformat())),
            ("INSERT INTO flujo_caja (tipo_movimiento, tipo_egreso, categoria, concepto, monto, moneda, "
             "fecha_pago, cuenta_destino, estado, usuario_creador, fecha_creacion, id_movimiento_vinculado) "
             "VALUES ('INGRESO','NO APLICA','Traspaso entre Cuentas',?,?,'MXN',?,?,'ACTIVO',?,?, (SELECT last_insert_rowid()))",
             (concepto_base, monto, fecha, destino, usuario, now_local().isoformat())),
        ])
        request.session["flash"] = {"tipo": "ok", "texto": f"✅ Traspaso de ${monto:,.2f} registrado: {origen} → {destino}."}
    else:
        request.session["flash"] = {"tipo": "error", "texto": "⚠️ Revisa el monto y que origen/destino sean distintos."}
    return RedirectResponse(url="/corte-caja", status_code=303)


@app.post("/corte-caja/ajuste", response_class=HTMLResponse)
async def corte_caja_ajuste(request: Request):
    """Ajuste manual de saldo por cuenta — SOLO admin. Sirve para cuando quien
    maneja la caja da su corte real (cuánto tiene de verdad hoy) y no cuadra con lo
    que el sistema calculó a partir del histórico (que puede estar mal clasificado o
    incompleto hacia atrás). No se reescribe el histórico: se agrega un movimiento
    normal, auditable en Libro Diario, con su propia categoría ('Ajuste de Caja') —
    nunca se disfraza de un cobro o pago real."""
    redir = _require_admin(request)
    if redir:
        return redir
    form = dict(await request.form())
    usuario = usuario_activo(request)
    hoy = str(now_local().date())

    cuenta = (form.get("cuenta") or "").strip()
    tipo   = (form.get("tipo") or "").strip()
    motivo = (form.get("motivo") or "").strip()
    confirmar = form.get("confirmar") == "1"
    try:
        monto = float(form.get("monto") or 0)
    except Exception:
        monto = 0.0

    if confirmar and cuenta and motivo and monto > 0 and tipo in ("sumar", "restar"):
        tipo_mov = "INGRESO" if tipo == "sumar" else "EGRESO"
        concepto = f"[Ajuste manual de caja] {motivo}"
        ejecutar_comando(
            "INSERT INTO flujo_caja (tipo_movimiento, tipo_egreso, categoria, concepto, monto, moneda, "
            "fecha_pago, cuenta_destino, estado, usuario_creador, fecha_creacion) "
            "VALUES (?,'NO APLICA','Ajuste de Caja',?,?,'MXN',?,?,'ACTIVO',?,?)",
            (tipo_mov, concepto, monto, hoy, cuenta, usuario, now_local().isoformat())
        )
        request.session["flash"] = {"tipo": "ok", "texto": f"✅ Ajuste aplicado a {cuenta}: {'+' if tipo=='sumar' else '-'}${monto:,.2f}."}
    else:
        request.session["flash"] = {"tipo": "error", "texto": "⚠️ Faltó cuenta, monto, motivo o la confirmación."}
    return RedirectResponse(url="/corte-caja", status_code=303)


@app.post("/corte-caja/traspaso/{id_movimiento}/anular", response_class=HTMLResponse)
async def corte_caja_traspaso_anular(request: Request, id_movimiento: int):
    if not usuario_activo(request):
        return RedirectResponse(url="/login")
    usuario = usuario_activo(request)
    df = obtener_datos(
        "SELECT id_movimiento FROM flujo_caja WHERE id_movimiento=? OR id_movimiento_vinculado=?",
        (id_movimiento, id_movimiento)
    )
    ids = [int(r["id_movimiento"]) for _, r in df.iterrows()]
    for mid in ids:
        ejecutar_comando(
            "UPDATE flujo_caja SET estado='CANCELADO', motivo_anulacion=? WHERE id_movimiento=? AND estado='ACTIVO'",
            (f"Traspaso anulado por {usuario}", mid)
        )
    request.session["flash"] = {"tipo": "ok", "texto": "Traspaso anulado."}
    return RedirectResponse(url="/corte-caja", status_code=303)


@app.get("/libro-diario", response_class=HTMLResponse)
async def libro_diario(request: Request):
    if not usuario_activo(request):
        return RedirectResponse(url="/login")

    hoy = now_local().date()

    # Parámetros de filtro
    params = dict(request.query_params)
    periodo   = params.get("periodo", "mes")          # hoy, semana, mes, año, custom, todo
    anio_sel  = params.get("anio", str(hoy.year))
    filtro_tipo    = params.get("tipo", "")            # INGRESO / EGRESO
    filtro_estado  = params.get("estado", "ACTIVO")   # ACTIVO / CANCELADO / (vacío=todos)
    moneda_sel     = params.get("moneda", "MXN")
    filtro_usuario = params.get("usuario", "")
    filtro_metodo  = params.get("metodo", "")

    # Calcular rango según período (mismo helper que usan los exports, evita duplicar la lógica)
    f_ini, f_fin = _get_periodo_fechas(params, hoy)

    # Query principal de flujo_caja + nombre del cliente/reserva
    where_parts = []
    args = []

    if f_ini:
        where_parts.append("fc.fecha_pago >= ?")
        args.append(f_ini)
    if f_fin:
        where_parts.append("fc.fecha_pago <= ?")
        args.append(f_fin)
    if filtro_tipo:
        where_parts.append("fc.tipo_movimiento = ?")
        args.append(filtro_tipo)
    if filtro_estado:
        where_parts.append("fc.estado = ?")
        args.append(filtro_estado)
    if moneda_sel:
        where_parts.append("fc.moneda = ?")
        args.append(moneda_sel)
    if filtro_usuario:
        where_parts.append("fc.usuario_creador = ?")
        args.append(filtro_usuario)
    if filtro_metodo == "__SIN__":
        where_parts.append("(fc.metodo_pago IS NULL OR fc.metodo_pago = '')")
    elif filtro_metodo:
        where_parts.append("fc.metodo_pago = ?")
        args.append(filtro_metodo)

    where_sql = ("WHERE " + " AND ".join(where_parts)) if where_parts else ""

    df = obtener_datos(f"""
        SELECT fc.id_movimiento, fc.id_reserva, fc.tipo_movimiento, fc.tipo_egreso,
               fc.categoria, fc.concepto, fc.monto, fc.moneda,
               fc.fecha_pago, fc.estado, fc.motivo_anulacion,
               fc.metodo_pago, fc.usuario_creador,
               r.destino, c.nombre AS nombre_cliente
        FROM flujo_caja fc
        LEFT JOIN reservas r ON fc.id_reserva = r.id_reserva
        LEFT JOIN clientes c ON r.id_cliente = c.id_cliente
        {where_sql}
        ORDER BY fc.fecha_pago DESC, fc.id_movimiento DESC
    """, tuple(args))

    movimientos = df.to_dict("records") if not df.empty else []
    # pandas vuelve NaN cualquier NULL de una columna numérica que mezcle enteros
    # y nulos (id_reserva) — y NaN es "verdadero" en Python, así que el template
    # lo mostraba como "#nan" en vez de "—". Normalizado a None (falsy real).
    for m in movimientos:
        if m["id_reserva"] is not None and m["id_reserva"] != m["id_reserva"]:
            m["id_reserva"] = None
        else:
            m["id_reserva"] = int(m["id_reserva"]) if m["id_reserva"] is not None else None

    # KPIs del período — flujo_caja es la fuente única de verdad
    activos = [m for m in movimientos if m["estado"] == "ACTIVO"]
    total_ingresos = sum(m["monto"] for m in activos if m["tipo_movimiento"] == "INGRESO")
    total_egresos  = sum(m["monto"] for m in activos if m["tipo_movimiento"] == "EGRESO")
    balance_neto   = total_ingresos - total_egresos

    df_usu = obtener_datos(
        "SELECT DISTINCT usuario_creador FROM flujo_caja "
        "WHERE usuario_creador IS NOT NULL AND usuario_creador != '' "
        "ORDER BY usuario_creador"
    )
    usuarios_disp = df_usu["usuario_creador"].tolist() if not df_usu.empty else []

    df_met = obtener_datos(
        "SELECT DISTINCT metodo_pago FROM flujo_caja "
        "WHERE metodo_pago IS NOT NULL AND metodo_pago != '' "
        "ORDER BY metodo_pago"
    )
    metodos_disp = df_met["metodo_pago"].tolist() if not df_met.empty else []

    retenido_sin_aplicar = _retenido_sin_aplicar_total(moneda_sel)
    saldo_credito_aerolinea = _saldo_credito_aerolinea_total()

    return templates.TemplateResponse(request, "libro_diario.html", ctx(request, {
        "active": "libro",
        "movimientos": movimientos,
        "total_ingresos": total_ingresos,
        "total_egresos": total_egresos,
        "balance_neto": balance_neto,
        "retenido_sin_aplicar": retenido_sin_aplicar,
        "saldo_credito_aerolinea": saldo_credito_aerolinea,
        "periodo": periodo,
        "f_ini": f_ini,
        "f_fin": f_fin,
        "filtro_tipo": filtro_tipo,
        "filtro_estado": filtro_estado,
        "filtro_usuario": filtro_usuario,
        "filtro_metodo": filtro_metodo,
        "moneda_sel": moneda_sel,
        "usuarios_disp": usuarios_disp,
        "metodos_disp": metodos_disp,
        "today": str(hoy),
        "anio_sel": anio_sel,
        "anios_disp": [str(a) for a in range(hoy.year - 1, hoy.year + 2)],
    }))


@app.post("/libro-diario/anular-movimiento")
async def anular_movimiento_libre(request: Request):
    usuario = usuario_activo(request)
    if not usuario:
        return RedirectResponse(url="/login", status_code=303)
    form = await request.form()
    id_mov = (form.get("id_movimiento") or "").strip()
    motivo = (form.get("motivo") or "").strip()
    if not (id_mov.isdigit() and motivo):
        return RedirectResponse(url="/libro-diario", status_code=303)

    # Mismas validaciones que "Anular Movimiento General" de Streamlit: solo para
    # movimientos SIN itinerario ligado (id_reserva IS NULL) — un movimiento ligado
    # a una reserva debe anularse desde la Bitácora, o el plan_pagos/costo_total del
    # itinerario queda desincronizado de flujo_caja (mismo tipo de bug forense ya
    # encontrado en 2026-08 con fixes manuales de SQL sin pasar por la app).
    df_row = obtener_datos(
        "SELECT id_movimiento, tipo_movimiento, monto, concepto, estado, id_reserva FROM flujo_caja WHERE id_movimiento=?",
        (int(id_mov),)
    )
    if df_row.empty:
        request.session["flash"] = {"tipo": "error", "texto": f"⚠️ No existe el movimiento #{id_mov}."}
        return RedirectResponse(url="/libro-diario", status_code=303)
    fila = df_row.iloc[0]
    if fila["estado"] != "ACTIVO":
        request.session["flash"] = {"tipo": "error", "texto": f"⚠️ El movimiento #{id_mov} ya está cancelado."}
        return RedirectResponse(url="/libro-diario", status_code=303)
    if pd.notna(fila["id_reserva"]):
        request.session["flash"] = {"tipo": "error", "texto": f"⚠️ El movimiento #{id_mov} está ligado al Itinerario #{int(fila['id_reserva'])}. Anúlalo desde la Bitácora."}
        return RedirectResponse(url="/libro-diario", status_code=303)

    ejecutar_comando(
        "UPDATE flujo_caja SET estado='CANCELADO', motivo_anulacion=? WHERE id_movimiento=?",
        (motivo, int(id_mov))
    )
    ejecutar_comando(
        "INSERT INTO anulaciones_audit (id_movimiento, tipo_movimiento, monto_anulado, razon_anulacion, fecha_anulacion, usuario_anulo, movimiento_original) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (int(id_mov), fila["tipo_movimiento"], float(fila["monto"] or 0), motivo, str(now_local().date()), usuario, fila["concepto"])
    )
    request.session["flash"] = {"tipo": "ok", "texto": f"✅ Movimiento #{id_mov} anulado correctamente."}
    return RedirectResponse(url="/libro-diario", status_code=303)


# ─── Ingresos de oficina (capital / inversión) ────────────────────────────────

CATEGORIAS_INGRESO_OF = [
    "Aportación de Capital / Inversión de Socias",
    "Préstamo recibido",
    "Otro ingreso extraordinario",
]

@app.get("/ingresos", response_class=HTMLResponse)
async def ingresos_oficina(request: Request):
    if not usuario_activo(request):
        return RedirectResponse(url="/login")

    hoy = now_local().date()
    params = dict(request.query_params)
    periodo  = params.get("periodo", "mes")
    f_ini    = params.get("fecha_ini", "")
    f_fin    = params.get("fecha_fin", "")

    import datetime as _dt
    if periodo == "hoy":
        f_ini = f_fin = str(hoy)
    elif periodo == "semana":
        lunes = hoy - _dt.timedelta(days=hoy.weekday())
        f_ini = str(lunes); f_fin = str(hoy)
    elif periodo == "mes":
        f_ini = str(hoy.replace(day=1)); f_fin = str(hoy)
    elif periodo == "todo":
        f_ini = f_fin = ""

    filtro_cat = params.get("categoria", "")

    where, args = ["fc.tipo_movimiento = 'ACTIVO' OR 1=1"], []
    # Solo ingresos de oficina (sin reserva ligada); los abonos de clientes
    # viven dentro de cada itinerario y en el Libro Diario consolidado.
    # 'Crédito Aerolínea Generado' también tiene id_reserva NULL pero no es capital de
    # oficina — se audita en Libro Diario, no aquí (ver _saldo_credito_aerolinea_total).
    where = ["fc.tipo_movimiento = 'INGRESO'", "fc.id_reserva IS NULL", "fc.categoria != 'Crédito Aerolínea Generado'", "fc.categoria != 'Traspaso entre Cuentas'", "fc.categoria != 'Ajuste de Caja'"]
    if f_ini: where.append("fc.fecha_pago >= ?"); args.append(f_ini)
    if f_fin: where.append("fc.fecha_pago <= ?"); args.append(f_fin)
    if filtro_cat: where.append("fc.categoria = ?"); args.append(filtro_cat)
    where_sql = "WHERE " + " AND ".join(where)

    df = obtener_datos(f"""
        SELECT fc.id_movimiento, fc.id_reserva, fc.categoria, fc.concepto,
               fc.monto, fc.moneda, fc.fecha_pago, fc.metodo_pago,
               fc.estado, fc.motivo_anulacion, fc.usuario_creador,
               r.destino, c.nombre AS nombre_cliente
        FROM flujo_caja fc
        LEFT JOIN reservas r ON fc.id_reserva = r.id_reserva
        LEFT JOIN clientes c ON r.id_cliente = c.id_cliente
        {where_sql}
        ORDER BY fc.fecha_pago DESC, fc.id_movimiento DESC
    """, tuple(args))
    registros = df.to_dict("records") if not df.empty else []

    activos = [r for r in registros if r["estado"] == "ACTIVO"]
    total_activo = sum(r["monto"] for r in activos)

    # Desglose por categoría
    from collections import defaultdict
    por_categoria = defaultdict(float)
    for r in activos:
        por_categoria[r["categoria"] or "Sin categoría"] += float(r["monto"] or 0)
    por_categoria = sorted(por_categoria.items(), key=lambda x: -x[1])

    # Categorías únicas para el filtro
    cats_existentes = sorted({r["categoria"] for r in registros if r["categoria"]})

    flash = request.session.pop("flash", None)
    return templates.TemplateResponse(request, "ingresos_oficina.html", ctx(request, {
        "active": "ingresos",
        "registros": registros,
        "total_activo": total_activo,
        "por_categoria": por_categoria,
        "cats_existentes": cats_existentes,
        "categorias": CATEGORIAS_INGRESO_OF,
        "filtro_cat": filtro_cat,
        "periodo": periodo,
        "f_ini": f_ini,
        "f_fin": f_fin,
        "today": str(hoy),
        "flash": flash,
    }))


@app.post("/ingresos", response_class=HTMLResponse)
async def ingresos_oficina_post(request: Request):
    if not usuario_activo(request):
        return RedirectResponse(url="/login")

    form = dict(await request.form())
    usuario = usuario_activo(request)
    hoy = str(now_local().date())

    categoria = (form.get("categoria") or "").strip()
    concepto  = (form.get("concepto") or "").strip()
    monto     = float(form.get("monto") or 0)
    metodo    = (form.get("metodo_pago") or "").strip()
    fecha     = (form.get("fecha_pago") or hoy).strip()
    moneda    = (form.get("moneda") or "MXN").strip()

    if monto > 0 and categoria:
        ejecutar_comando(
            "INSERT INTO flujo_caja (tipo_movimiento, tipo_egreso, categoria, concepto, monto, moneda, fecha_pago, metodo_pago, estado, usuario_creador, fecha_creacion) VALUES ('INGRESO','NO APLICA',?,?,?,?,?,?,'ACTIVO',?,?)",
            (categoria, concepto or categoria, monto, moneda, fecha, metodo or None, usuario, now_local().isoformat())
        )
        request.session["flash"] = {"tipo": "ok", "texto": f"✅ Ingreso de ${monto:,.2f} registrado."}

    return RedirectResponse(url="/ingresos", status_code=303)


@app.post("/ingresos/{id_mov}/anular", response_class=HTMLResponse)
async def ingreso_anular(request: Request, id_mov: int):
    if not usuario_activo(request):
        return RedirectResponse(url="/login")
    form = dict(await request.form())
    motivo = (form.get("motivo") or "Sin motivo").strip()
    ejecutar_comando(
        "UPDATE flujo_caja SET estado='CANCELADO', motivo_anulacion=? WHERE id_movimiento=? AND tipo_movimiento='INGRESO'",
        (motivo, id_mov)
    )
    request.session["flash"] = {"tipo": "error", "texto": "Ingreso anulado."}
    return RedirectResponse(url="/ingresos", status_code=303)


# ─── Egresos de oficina ────────────────────────────────────────────────────────

CATEGORIAS_EGRESO_OF = [
    "Renta / Local",
    "Servicios (agua, luz, internet, teléfono)",
    "Papelería y Materiales",
    "Publicidad y Marketing",
    "Sueldos y Honorarios",
    "Comisiones a Vendedores",
    "Equipo y Tecnología",
    "Transporte / Gasolina",
    "Mantenimiento",
    "Impuestos y Contabilidad",
    "Viáticos",
    "Cortesía",
    "Otros gastos operativos",
]

@app.get("/egresos", response_class=HTMLResponse)
async def egresos_oficina_view(request: Request):
    if not usuario_activo(request):
        return RedirectResponse(url="/login")

    hoy = now_local().date()
    params = dict(request.query_params)
    periodo  = params.get("periodo", "mes")
    f_ini    = params.get("fecha_ini", "")
    f_fin    = params.get("fecha_fin", "")

    import datetime as _dt
    if periodo == "hoy":
        f_ini = f_fin = str(hoy)
    elif periodo == "semana":
        lunes = hoy - _dt.timedelta(days=hoy.weekday())
        f_ini = str(lunes); f_fin = str(hoy)
    elif periodo == "mes":
        f_ini = str(hoy.replace(day=1)); f_fin = str(hoy)
    elif periodo == "todo":
        f_ini = f_fin = ""

    where, args = ["tipo_movimiento='EGRESO'", "tipo_egreso='GASTO OPERATIVO OFICINA'"], []
    if f_ini: where.append("fecha_pago >= ?"); args.append(f_ini)
    if f_fin: where.append("fecha_pago <= ?"); args.append(f_fin)
    where_sql = "WHERE " + " AND ".join(where)

    # flujo_caja es la fuente única de verdad (igual que Libro Diario) — antes esta
    # página leía de una tabla espejo (egresos_oficina) que solo se llenaba al capturar
    # desde aquí; los egresos capturados desde otras interfaces (que solo escriben
    # flujo_caja) nunca aparecían aquí, aunque sí en el Libro Diario.
    df = obtener_datos(
        f"SELECT id_movimiento, tipo_egreso, categoria, concepto, monto, metodo_pago, cuenta_destino, "
        f"fecha_pago AS fecha_egreso, estado, usuario_creador, divisor FROM flujo_caja {where_sql} "
        f"ORDER BY fecha_pago DESC, id_movimiento DESC",
        tuple(args)
    )
    registros = df.to_dict("records") if not df.empty else []
    total_activo = round(sum(r["monto"] for r in registros if r["estado"] == "ACTIVO"), 2)

    # Agrupado por categoría (activos)
    from collections import defaultdict
    por_categoria = defaultdict(float)
    for r in registros:
        if r["estado"] == "ACTIVO":
            por_categoria[r["categoria"] or r["tipo_egreso"] or "Sin categoría"] += float(r["monto"] or 0)
    por_categoria = sorted(((c, round(t, 2)) for c, t in por_categoria.items()), key=lambda x: -x[1])

    flash = request.session.pop("flash", None)
    return templates.TemplateResponse(request, "egresos_oficina.html", ctx(request, {
        "active": "egresos",
        "registros": registros,
        "total_activo": total_activo,
        "por_categoria": por_categoria,
        "categorias": CATEGORIAS_EGRESO_OF,
        "cuentas_destino": CUENTAS_SIMPLE_DEFAULT,
        "cuentas_por_metodo": CUENTAS_POR_METODO,
        "periodo": periodo,
        "f_ini": f_ini,
        "f_fin": f_fin,
        "today": str(hoy),
        "flash": flash,
    }))


@app.post("/egresos", response_class=HTMLResponse)
async def egresos_oficina_post(request: Request):
    if not usuario_activo(request):
        return RedirectResponse(url="/login")

    form = dict(await request.form())
    usuario = usuario_activo(request)
    hoy = str(now_local().date())

    categoria      = (form.get("categoria") or "").strip()
    concepto       = (form.get("concepto") or "").strip()
    concepto_final = concepto if concepto else f"[Manual] {categoria}"
    monto          = float(form.get("monto") or 0)
    metodo         = (form.get("metodo_pago") or "").strip()
    fecha          = (form.get("fecha_egreso") or hoy).strip()
    divisor        = (form.get("divisor") or "").strip()
    cuenta_destino = _resolver_cuenta_destino(form.get("cuenta_destino"), metodo)

    if monto > 0 and categoria:
        # flujo_caja es la única tabla de escritura — ya no hay tabla espejo
        # (egresos_oficina) que pueda quedar desincronizada.
        ejecutar_comando(
            "INSERT INTO flujo_caja (tipo_movimiento, tipo_egreso, categoria, concepto, monto, moneda, fecha_pago, metodo_pago, cuenta_destino, estado, usuario_creador, divisor, fecha_creacion) VALUES ('EGRESO','GASTO OPERATIVO OFICINA',?,?,?,'MXN',?,?,?,'ACTIVO',?,?,?)",
            (categoria, concepto_final, monto, fecha, metodo or None, cuenta_destino, usuario, divisor or None, now_local().isoformat())
        )
        request.session["flash"] = {"tipo": "ok", "texto": f"✅ Egreso de ${monto:,.2f} registrado."}

    return RedirectResponse(url="/egresos", status_code=303)


@app.post("/egresos/{id_movimiento}/anular", response_class=HTMLResponse)
async def egreso_anular(request: Request, id_movimiento: int):
    if not usuario_activo(request):
        return RedirectResponse(url="/login")
    form = dict(await request.form())
    motivo = (form.get("motivo") or "Sin motivo").strip()
    usuario = usuario_activo(request)

    df_eg = obtener_datos(
        "SELECT monto, concepto, estado FROM flujo_caja WHERE id_movimiento=? AND tipo_egreso='GASTO OPERATIVO OFICINA'",
        (id_movimiento,)
    )
    if not df_eg.empty and df_eg.iloc[0]["estado"] == "ACTIVO":
        ejecutar_comando(
            "UPDATE flujo_caja SET estado='CANCELADO', motivo_anulacion=? WHERE id_movimiento=?",
            (motivo, id_movimiento)
        )
        ejecutar_comando(
            "INSERT INTO anulaciones_audit (id_movimiento, tipo_movimiento, monto_anulado, usuario_anulo, fecha_anulacion, razon_anulacion, movimiento_original) VALUES (?,?,?,?,?,?,?)",
            (id_movimiento, "EGRESO", float(df_eg.iloc[0]["monto"] or 0), usuario, str(now_local().date()), motivo, df_eg.iloc[0]["concepto"])
        )
    request.session["flash"] = {"tipo": "error", "texto": "Egreso anulado."}
    return RedirectResponse(url="/egresos", status_code=303)


# ─── Exportar a Excel ─────────────────────────────────────────────────────────

def _sanitize_excel(val):
    if isinstance(val, str) and val and val[0] in ('=', '+', '-', '@'):
        return "'" + val
    return val


def _excel_response(df, filename: str):
    """Genera una StreamingResponse con el DataFrame como archivo .xlsx"""
    import io
    from fastapi.responses import StreamingResponse
    # Protege contra formula injection en celdas de texto
    df_safe = df.copy()
    for col in df_safe.select_dtypes(include="object").columns:
        df_safe[col] = df_safe[col].map(_sanitize_excel)
    buf = io.BytesIO()
    wb = __import__("openpyxl").Workbook()
    ws = wb.active
    ws.title = filename.replace(".xlsx", "")[:31]
    # Encabezados
    ws.append(list(df_safe.columns))
    for cell in ws[1]:
        cell.font = __import__("openpyxl").styles.Font(bold=True, color="FFFFFF")
        cell.fill = __import__("openpyxl").styles.PatternFill("solid", fgColor="0047AB")
    # Datos
    for row in df_safe.itertuples(index=False):
        ws.append(list(row))
    # Ancho automático
    for col in ws.columns:
        max_len = max((len(str(cell.value or "")) for cell in col), default=10)
        ws.column_dimensions[col[0].column_letter].width = min(max_len + 2, 50)
    wb.save(buf)
    buf.seek(0)
    return StreamingResponse(
        buf,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'}
    )


def _get_periodo_fechas(params, hoy):
    import datetime as _dt
    periodo = params.get("periodo", "mes")
    f_ini   = params.get("fecha_ini", "")
    f_fin   = params.get("fecha_fin", "")
    if periodo == "hoy":
        return str(hoy), str(hoy)
    elif periodo == "semana":
        lunes = hoy - _dt.timedelta(days=hoy.weekday())
        return str(lunes), str(hoy)
    elif periodo == "mes":
        return str(hoy.replace(day=1)), str(hoy)
    elif periodo == "año":
        anio = params.get("anio", str(hoy.year))
        return f"{anio}-01-01", f"{anio}-12-31"
    elif periodo == "todo":
        return "", ""
    return f_ini, f_fin


@app.get("/libro-diario/exportar")
async def libro_diario_exportar(request: Request):
    if not usuario_activo(request):
        return RedirectResponse(url="/login")
    hoy = now_local().date()
    params = dict(request.query_params)
    f_ini, f_fin = _get_periodo_fechas(params, hoy)
    filtro_tipo   = params.get("tipo", "")
    filtro_estado = params.get("estado", "ACTIVO")

    where, args = [], []
    if f_ini: where.append("fc.fecha_pago >= ?"); args.append(f_ini)
    if f_fin: where.append("fc.fecha_pago <= ?"); args.append(f_fin)
    if filtro_tipo:   where.append("fc.tipo_movimiento = ?"); args.append(filtro_tipo)
    if filtro_estado: where.append("fc.estado = ?"); args.append(filtro_estado)
    where_sql = ("WHERE " + " AND ".join(where)) if where else ""

    df = obtener_datos(f"""
        SELECT fc.fecha_pago AS Fecha, fc.tipo_movimiento AS Tipo,
               fc.categoria AS Categoria, fc.concepto AS Concepto,
               fc.monto AS Monto, fc.moneda AS Moneda,
               fc.metodo_pago AS Metodo_Pago, fc.estado AS Estado,
               r.id_reserva AS ID_Reserva, c.nombre AS Cliente, r.destino AS Destino,
               r.fecha_limite_liquidacion AS Fecha_Limite_Cliente,
               fc.usuario_creador AS Usuario
        FROM flujo_caja fc
        LEFT JOIN reservas r ON fc.id_reserva = r.id_reserva
        LEFT JOIN clientes c ON r.id_cliente = c.id_cliente
        {where_sql}
        ORDER BY fc.fecha_pago DESC, fc.id_movimiento DESC
    """, tuple(args))
    periodo = params.get("periodo", "mes")
    return _excel_response(df, f"libro_diario_{periodo}_{hoy}.xlsx")


@app.get("/ingresos/exportar")
async def ingresos_exportar(request: Request):
    if not usuario_activo(request):
        return RedirectResponse(url="/login")
    hoy = now_local().date()
    params = dict(request.query_params)
    f_ini, f_fin = _get_periodo_fechas(params, hoy)
    filtro_cat = params.get("categoria", "")

    where, args = ["fc.tipo_movimiento = 'INGRESO'"], []
    if f_ini: where.append("fc.fecha_pago >= ?"); args.append(f_ini)
    if f_fin: where.append("fc.fecha_pago <= ?"); args.append(f_fin)
    if filtro_cat: where.append("fc.categoria = ?"); args.append(filtro_cat)
    where_sql = "WHERE " + " AND ".join(where)

    df = obtener_datos(f"""
        SELECT fc.fecha_pago AS Fecha, fc.categoria AS Categoria,
               fc.concepto AS Concepto, fc.monto AS Monto, fc.moneda AS Moneda,
               fc.metodo_pago AS Metodo_Pago, fc.estado AS Estado,
               r.id_reserva AS ID_Reserva, c.nombre AS Cliente, r.destino AS Destino,
               fc.usuario_creador AS Usuario
        FROM flujo_caja fc
        LEFT JOIN reservas r ON fc.id_reserva = r.id_reserva
        LEFT JOIN clientes c ON r.id_cliente = c.id_cliente
        {where_sql}
        ORDER BY fc.fecha_pago DESC, fc.id_movimiento DESC
    """, tuple(args))
    return _excel_response(df, f"ingresos_{hoy}.xlsx")


@app.get("/egresos/exportar")
async def egresos_exportar(request: Request):
    if not usuario_activo(request):
        return RedirectResponse(url="/login")
    hoy = now_local().date()
    params = dict(request.query_params)
    f_ini, f_fin = _get_periodo_fechas(params, hoy)

    where = ["tipo_movimiento='EGRESO'", "tipo_egreso='GASTO OPERATIVO OFICINA'", "estado='ACTIVO'"]
    args = []
    if f_ini: where.append("fecha_pago >= ?"); args.append(f_ini)
    if f_fin: where.append("fecha_pago <= ?"); args.append(f_fin)
    where_sql = "WHERE " + " AND ".join(where)

    df = obtener_datos(f"""
        SELECT fecha_pago AS Fecha, categoria AS Categoria, concepto AS Concepto,
               monto AS Monto, metodo_pago AS Metodo_Pago, divisor AS Divide_Entre,
               estado AS Estado, usuario_creador AS Usuario
        FROM flujo_caja {where_sql}
        ORDER BY fecha_pago DESC, id_movimiento DESC
    """, tuple(args))
    return _excel_response(df, f"egresos_oficina_{hoy}.xlsx")


# ─── Admin ────────────────────────────────────────────────────────────────────

def _require_admin(request: Request):
    if not usuario_activo(request):
        return RedirectResponse(url="/login")
    if request.session.get("rol") != "admin":
        return RedirectResponse(url="/dashboard")
    return None


METODOS_PAGO_INICIALES = [
    ("Efectivo",          "Efectivo"),
    ("Transferencia",     "Transferencia"),
    ("Depósito bancario", "Transferencia"),
    ("Tarjeta de Crédito/Débito", "Tarjeta"),
    ("Cheque",            "Cheque"),
]


def _seed_metodos_pago():
    """Inserta métodos de pago iniciales si la tabla está vacía."""
    df = obtener_datos("SELECT COUNT(*) as n FROM metodos_pago")
    if df.iloc[0]["n"] == 0:
        for nombre, cat in METODOS_PAGO_INICIALES:
            ejecutar_comando(
                "INSERT INTO metodos_pago (nombre, categoria, activo) VALUES (?,?,1)",
                (nombre, cat)
            )


@app.get("/admin", response_class=HTMLResponse)
async def admin_panel(request: Request):
    redir = _require_admin(request)
    if redir: return redir

    _seed_metodos_pago()

    usuarios_df = obtener_datos("SELECT usuario, rol, ultima_cambio_password, primer_login FROM usuarios ORDER BY rol DESC, usuario")
    metodos_df  = obtener_datos("SELECT id, nombre, categoria, activo FROM metodos_pago ORDER BY categoria, nombre")
    sesiones_df = obtener_datos("SELECT usuario, ultima_actividad FROM sesiones_activas ORDER BY ultima_actividad DESC")

    flash = request.session.pop("flash", None)
    return templates.TemplateResponse(request, "admin.html", ctx(request, {
        "active": "admin",
        "usuarios": usuarios_df.to_dict("records"),
        "metodos": metodos_df.to_dict("records"),
        "sesiones": {r["usuario"]: r["ultima_actividad"] for _, r in sesiones_df.iterrows()} if not sesiones_df.empty else {},
        "today": str(now_local().date()),
        "flash": flash,
        "tab": request.query_params.get("tab", "usuarios"),
    }))


@app.post("/admin/usuarios/nuevo", response_class=HTMLResponse)
async def admin_usuario_nuevo(request: Request):
    redir = _require_admin(request)
    if redir: return redir

    from auth import encriptar_password, validar_password, guardar_historial_password
    form = dict(await request.form())
    nuevo_user = (form.get("usuario") or "").strip().lower()
    nuevo_rol  = (form.get("rol") or "operador").strip()
    pwd        = (form.get("password") or "").strip()

    if not nuevo_user or not pwd:
        request.session["flash"] = {"tipo": "error", "texto": "Usuario y contraseña son obligatorios."}
        return RedirectResponse(url="/admin?tab=usuarios", status_code=303)

    df_exist = obtener_datos("SELECT usuario FROM usuarios WHERE usuario=?", (nuevo_user,))
    if not df_exist.empty:
        request.session["flash"] = {"tipo": "error", "texto": f"El usuario '{nuevo_user}' ya existe."}
        return RedirectResponse(url="/admin?tab=usuarios", status_code=303)

    ok, msg = validar_password(pwd, nuevo_user)
    if not ok:
        request.session["flash"] = {"tipo": "error", "texto": msg}
        return RedirectResponse(url="/admin?tab=usuarios", status_code=303)

    h = encriptar_password(pwd)
    ejecutar_comando(
        "INSERT INTO usuarios (usuario, password, rol, ultima_cambio_password, primer_login) VALUES (?,?,?,?,1)",
        (nuevo_user, h, nuevo_rol, str(now_local().date()))
    )
    guardar_historial_password(nuevo_user, h)
    request.session["flash"] = {"tipo": "ok", "texto": f"✅ Usuario '{nuevo_user}' creado como {nuevo_rol}."}
    return RedirectResponse(url="/admin?tab=usuarios", status_code=303)


@app.post("/admin/usuarios/{target}/rol", response_class=HTMLResponse)
async def admin_cambiar_rol(request: Request, target: str):
    redir = _require_admin(request)
    if redir: return redir

    yo = usuario_activo(request)
    if target == yo:
        request.session["flash"] = {"tipo": "error", "texto": "No puedes cambiar tu propio rol."}
        return RedirectResponse(url="/admin?tab=usuarios", status_code=303)

    form    = dict(await request.form())
    new_rol = (form.get("rol") or "operador").strip()
    ejecutar_comando("UPDATE usuarios SET rol=? WHERE usuario=?", (new_rol, target))
    request.session["flash"] = {"tipo": "ok", "texto": f"✅ Rol de '{target}' cambiado a {new_rol}."}
    return RedirectResponse(url="/admin?tab=usuarios", status_code=303)


@app.post("/admin/usuarios/{target}/password", response_class=HTMLResponse)
async def admin_reset_password(request: Request, target: str):
    redir = _require_admin(request)
    if redir: return redir

    from auth import encriptar_password, validar_password, guardar_historial_password
    form = dict(await request.form())
    pwd  = (form.get("password") or "").strip()

    ok, msg = validar_password(pwd, target)
    if not ok:
        request.session["flash"] = {"tipo": "error", "texto": msg}
        return RedirectResponse(url="/admin?tab=usuarios", status_code=303)

    h = encriptar_password(pwd)
    ejecutar_comando(
        "UPDATE usuarios SET password=?, ultima_cambio_password=?, primer_login=1 WHERE usuario=?",
        (h, str(now_local().date()), target)
    )
    guardar_historial_password(target, h)
    request.session["flash"] = {"tipo": "ok", "texto": f"✅ Contraseña de '{target}' restablecida. El usuario deberá cambiarla en su próximo login."}
    return RedirectResponse(url="/admin?tab=usuarios", status_code=303)


@app.post("/admin/usuarios/{target}/eliminar", response_class=HTMLResponse)
async def admin_eliminar_usuario(request: Request, target: str):
    redir = _require_admin(request)
    if redir: return redir

    yo = usuario_activo(request)
    if target == yo:
        request.session["flash"] = {"tipo": "error", "texto": "No puedes eliminarte a ti mismo."}
        return RedirectResponse(url="/admin?tab=usuarios", status_code=303)

    ejecutar_comando("DELETE FROM usuarios WHERE usuario=?", (target,))
    request.session["flash"] = {"tipo": "ok", "texto": f"Usuario '{target}' eliminado."}
    return RedirectResponse(url="/admin?tab=usuarios", status_code=303)


@app.post("/admin/metodos-pago/nuevo", response_class=HTMLResponse)
async def admin_metodo_nuevo(request: Request):
    redir = _require_admin(request)
    if redir: return redir

    form     = dict(await request.form())
    nombre   = (form.get("nombre") or "").strip()
    categoria = (form.get("categoria") or "Otro").strip()

    if nombre:
        df_exist = obtener_datos("SELECT id FROM metodos_pago WHERE nombre=?", (nombre,))
        if df_exist.empty:
            ejecutar_comando("INSERT INTO metodos_pago (nombre, categoria, activo) VALUES (?,?,1)", (nombre, categoria))
            request.session["flash"] = {"tipo": "ok", "texto": f"✅ Método '{nombre}' agregado."}
        else:
            request.session["flash"] = {"tipo": "error", "texto": f"'{nombre}' ya existe."}

    return RedirectResponse(url="/admin?tab=config", status_code=303)


@app.post("/admin/metodos-pago/{id_mp}/toggle", response_class=HTMLResponse)
async def admin_metodo_toggle(request: Request, id_mp: int):
    redir = _require_admin(request)
    if redir: return redir

    ejecutar_comando("UPDATE metodos_pago SET activo = 1 - activo WHERE id=?", (id_mp,))
    return RedirectResponse(url="/admin?tab=config", status_code=303)


@app.post("/admin/metodos-pago/{id_mp}/eliminar", response_class=HTMLResponse)
async def admin_metodo_eliminar(request: Request, id_mp: int):
    redir = _require_admin(request)
    if redir: return redir

    ejecutar_comando("DELETE FROM metodos_pago WHERE id=?", (id_mp,))
    return RedirectResponse(url="/admin?tab=config", status_code=303)


@app.post("/admin/usuarios/forzar-todos", response_class=HTMLResponse)
async def admin_forzar_todos(request: Request):
    redir = _require_admin(request)
    if redir: return redir
    yo = usuario_activo(request)
    ejecutar_comando("UPDATE usuarios SET primer_login=1 WHERE usuario != ?", (yo,))
    request.session["flash"] = {"tipo": "ok", "texto": "✅ Todos los usuarios (excepto tú) deberán cambiar su contraseña al próximo login."}
    return RedirectResponse(url="/admin?tab=usuarios", status_code=303)


@app.post("/admin/usuarios/{target}/forzar-login", response_class=HTMLResponse)
async def admin_forzar_login(request: Request, target: str):
    redir = _require_admin(request)
    if redir: return redir
    yo = usuario_activo(request)
    if target == yo:
        request.session["flash"] = {"tipo": "error", "texto": "No puedes forzarte a ti mismo."}
        return RedirectResponse(url="/admin?tab=usuarios", status_code=303)
    ejecutar_comando("UPDATE usuarios SET primer_login=1 WHERE usuario=?", (target,))
    request.session["flash"] = {"tipo": "ok", "texto": f"✅ '{target}' deberá cambiar su contraseña al próximo login."}
    return RedirectResponse(url="/admin?tab=usuarios", status_code=303)


# ─── Cambiar contraseña (propio / forzado por primer_login) ──────────────────

@app.get("/cambiar-password", response_class=HTMLResponse)
async def cambiar_password_form(request: Request):
    if not usuario_activo(request):
        return RedirectResponse(url="/login")
    yo = usuario_activo(request)
    df_u = obtener_datos("SELECT primer_login FROM usuarios WHERE usuario=?", (yo,))
    es_forzado = (not df_u.empty) and int(df_u.iloc[0]["primer_login"] or 0) == 1
    return templates.TemplateResponse(request, "cambiar_password.html", ctx(request, {
        "es_forzado": es_forzado,
    }))


@app.post("/cambiar-password", response_class=HTMLResponse)
async def cambiar_password_post(request: Request):
    if not usuario_activo(request):
        return RedirectResponse(url="/login")
    from auth import encriptar_password, validar_password, guardar_historial_password

    form      = dict(await request.form())
    pwd_act   = (form.get("password_actual") or "").strip()
    pwd_nueva = (form.get("password_nueva") or "").strip()
    pwd_conf  = (form.get("password_confirmar") or "").strip()
    yo        = usuario_activo(request)

    # Verificar contraseña actual (excepto si primer_login — admin pudo resetearla)
    df_u = obtener_datos("SELECT password, primer_login FROM usuarios WHERE usuario=?", (yo,))
    if df_u.empty:
        return RedirectResponse(url="/logout")

    from auth import verificar_password
    es_forzado = int(df_u.iloc[0]["primer_login"] or 0) == 1

    if not es_forzado:
        if not verificar_password(pwd_act, df_u.iloc[0]["password"]):
            request.session["flash"] = {"tipo": "error", "texto": "La contraseña actual es incorrecta."}
            return RedirectResponse(url="/cambiar-password", status_code=303)

    if pwd_nueva != pwd_conf:
        request.session["flash"] = {"tipo": "error", "texto": "Las contraseñas nuevas no coinciden."}
        return RedirectResponse(url="/cambiar-password", status_code=303)

    ok, msg = validar_password(pwd_nueva, yo)
    if not ok:
        request.session["flash"] = {"tipo": "error", "texto": msg}
        return RedirectResponse(url="/cambiar-password", status_code=303)

    h = encriptar_password(pwd_nueva)
    ejecutar_comando(
        "UPDATE usuarios SET password=?, ultima_cambio_password=?, primer_login=0 WHERE usuario=?",
        (h, str(now_local().date()), yo)
    )
    guardar_historial_password(yo, h)
    request.session["forzar_cambio_pwd"] = False
    request.session["flash"] = {"tipo": "ok", "texto": "✅ Contraseña actualizada correctamente."}
    return RedirectResponse(url="/dashboard", status_code=303)


# ─── Búsqueda Global ─────────────────────────────────────────────────────────

@app.get("/buscar", response_class=HTMLResponse)
async def buscar_global(request: Request, q: str = ""):
    if not usuario_activo(request):
        return RedirectResponse(url="/login")

    clientes_res, reservas_res, cotizaciones_res = [], [], []

    if q and len(q.strip()) >= 2:
        q = q.strip()
        like = f"%{q}%"

        clientes_res = obtener_datos("""
            SELECT id_cliente, nombre, telefono, email,
                   (SELECT COUNT(*) FROM reservas WHERE reservas.id_cliente = clientes.id_cliente) as n_viajes
            FROM clientes
            WHERE nombre LIKE ? OR telefono LIKE ? OR email LIKE ? OR id_cliente LIKE ?
            ORDER BY nombre LIMIT 20
        """, (like, like, like, like)).to_dict("records")

        reservas_res = obtener_datos("""
            SELECT r.id_reserva, c.nombre as nombre_cliente, r.destino,
                   r.fecha_salida, r.estado, r.venta_total, r.moneda
            FROM reservas r JOIN clientes c ON r.id_cliente = c.id_cliente
            WHERE c.nombre LIKE ? OR r.destino LIKE ? OR CAST(r.id_reserva AS TEXT) LIKE ?
            ORDER BY r.id_reserva DESC LIMIT 20
        """, (like, like, like)).to_dict("records")

        cotizaciones_res = obtener_datos("""
            SELECT cot.id_cotizacion, c.nombre as cliente, cot.destino,
                   cot.fecha_salida, cot.estado, cot.venta_total, cot.moneda,
                   cot.fecha_cotizacion
            FROM cotizaciones cot JOIN clientes c ON cot.id_cliente = c.id_cliente
            WHERE c.nombre LIKE ? OR cot.destino LIKE ?
            ORDER BY cot.id_cotizacion DESC LIMIT 20
        """, (like, like)).to_dict("records")

    return templates.TemplateResponse(request, "buscar.html", ctx(request, {
        "active": "buscar",
        "q": q,
        "clientes": clientes_res,
        "reservas": reservas_res,
        "cotizaciones": cotizaciones_res,
        "today": str(now_local().date()),
    }))


# ─── Admin: Auditoría de Anulaciones ─────────────────────────────────────────

def _mes_anio_audit_like(params):
    """Sanitiza mes/año para Auditoría de Anulaciones. mes='TODOS' = todo el año elegido."""
    mes_raw = params.get("mes", str(now_local().month).zfill(2))
    anio = _re_main.sub(r'[^0-9]', '', params.get("anio", str(now_local().year)))[:4]
    if mes_raw == "TODOS":
        return "TODOS", anio, f"{anio}%"
    mes = _re_main.sub(r'[^0-9]', '', mes_raw)[:2]
    return mes, anio, f"{anio}-{mes}%"


@app.get("/admin/auditoria", response_class=HTMLResponse)
async def admin_auditoria(request: Request):
    if not usuario_activo(request):
        return RedirectResponse(url="/login")

    params   = dict(request.query_params)
    mes, anio, like_pattern = _mes_anio_audit_like(params)
    f_usuario = params.get("usuario", "")
    f_tipo    = params.get("tipo", "")

    where, args = ["a.fecha_anulacion LIKE ?"], [like_pattern]
    if f_usuario: where.append("a.usuario_anulo = ?"); args.append(f_usuario)
    if f_tipo:    where.append("a.tipo_movimiento = ?"); args.append(f_tipo)
    where_sql = "WHERE " + " AND ".join(where)

    df = obtener_datos(
        f"SELECT a.id_anulacion, a.fecha_anulacion, a.usuario_anulo, a.tipo_movimiento, "
        f"a.monto_anulado, a.razon_anulacion, a.movimiento_original, "
        f"a.id_movimiento, f.id_reserva "
        f"FROM anulaciones_audit a LEFT JOIN flujo_caja f ON a.id_movimiento = f.id_movimiento "
        f"{where_sql} ORDER BY a.fecha_anulacion DESC",
        tuple(args)
    )
    registros = df.to_dict("records") if not df.empty else []
    for r in registros:
        v = r.get("id_reserva")
        r["id_reserva"] = int(v) if v is not None and v == v else None

    total_anulado  = sum(r["monto_anulado"] or 0 for r in registros)
    usuarios_unicos = len({r["usuario_anulo"] for r in registros if r["usuario_anulo"]})

    df_us = obtener_datos("SELECT DISTINCT usuario_anulo FROM anulaciones_audit WHERE usuario_anulo IS NOT NULL ORDER BY usuario_anulo")
    lista_usuarios = df_us["usuario_anulo"].tolist() if not df_us.empty else []

    meses = {"01":"Enero","02":"Febrero","03":"Marzo","04":"Abril","05":"Mayo","06":"Junio",
              "07":"Julio","08":"Agosto","09":"Septiembre","10":"Octubre","11":"Noviembre","12":"Diciembre"}
    anios = [str(y) for y in range(now_local().year - 1, now_local().year + 2)]

    es_admin = (obtener_datos("SELECT rol FROM usuarios WHERE usuario=?", (usuario_activo(request),)).iloc[0]["rol"] == "admin")
    return templates.TemplateResponse(request, "admin_auditoria.html", ctx(request, {
        "active": "auditoria",
        "es_admin": es_admin,
        "registros": registros,
        "total_anulado": total_anulado,
        "usuarios_unicos": usuarios_unicos,
        "lista_usuarios": lista_usuarios,
        "mes": mes,
        "anio": anio,
        "meses": meses,
        "anios": anios,
        "f_usuario": f_usuario,
        "f_tipo": f_tipo,
    }))


@app.get("/admin/auditoria/exportar")
async def admin_auditoria_exportar(request: Request):
    if not usuario_activo(request):
        return RedirectResponse(url="/login")
    params = dict(request.query_params)
    mes, anio, like_pattern = _mes_anio_audit_like(params)
    f_usuario = params.get("usuario", "")
    f_tipo    = params.get("tipo", "")
    where, args = ["a.fecha_anulacion LIKE ?"], [like_pattern]
    if f_usuario: where.append("a.usuario_anulo = ?"); args.append(f_usuario)
    if f_tipo:    where.append("a.tipo_movimiento = ?"); args.append(f_tipo)
    where_sql = "WHERE " + " AND ".join(where)
    df   = obtener_datos(
        f"SELECT a.fecha_anulacion AS Fecha, a.usuario_anulo AS Usuario, a.tipo_movimiento AS Tipo, "
        f"a.monto_anulado AS Monto, a.razon_anulacion AS Razon, a.movimiento_original AS Concepto_Original, "
        f"a.id_movimiento AS ID_Movimiento, f.id_reserva AS Itinerario "
        f"FROM anulaciones_audit a LEFT JOIN flujo_caja f ON a.id_movimiento = f.id_movimiento "
        f"{where_sql} ORDER BY a.fecha_anulacion DESC",
        tuple(args)
    )
    return _excel_response(df, f"auditoria_anulaciones_{anio}_{mes}.xlsx")


# ─── Admin: Rentabilidad por Operadora ───────────────────────────────────────

@app.get("/admin/rentabilidad", response_class=HTMLResponse)
async def admin_rentabilidad(request: Request):
    redir = _require_admin(request)
    if redir: return redir

    params = dict(request.query_params)
    mes    = params.get("mes", "")
    anio   = params.get("anio", str(now_local().year))
    filtro = f"{anio}-{mes}%" if mes else f"{anio}-%"
    like_f = filtro

    df_itin = obtener_datos("""
        SELECT usuario_creador AS Operadora,
               COUNT(*) AS Itinerarios,
               COALESCE(SUM(venta_total),0) AS Venta_Total,
               COALESCE(SUM(utilidad_proyectada),0) AS Utilidad,
               COALESCE(SUM(cobrado_cliente),0) AS Cobrado
        FROM reservas
        WHERE estado != 'CANCELADO' AND (fecha_salida LIKE ? OR fecha_creacion LIKE ?)
        GROUP BY usuario_creador ORDER BY Venta_Total DESC
    """, (like_f, like_f))

    df_cobros = obtener_datos("""
        SELECT usuario_creador AS Operadora,
               COUNT(*) AS Movimientos,
               COALESCE(SUM(CASE WHEN tipo_movimiento='INGRESO' THEN monto ELSE 0 END),0) AS Ingresos,
               COALESCE(SUM(CASE WHEN tipo_movimiento='EGRESO' THEN monto ELSE 0 END),0) AS Egresos
        FROM flujo_caja
        WHERE estado='ACTIVO' AND fecha_pago LIKE ?
        GROUP BY usuario_creador ORDER BY Ingresos DESC
    """, (like_f,))

    df_cot = obtener_datos("""
        SELECT usuario_creador AS Operadora,
               COUNT(*) AS Cotizaciones,
               SUM(CASE WHEN estado='ACEPTADA' THEN 1 ELSE 0 END) AS Convertidas,
               COALESCE(SUM(CASE WHEN estado='ACEPTADA' THEN venta_total ELSE 0 END),0) AS Venta_Convertida
        FROM cotizaciones
        WHERE fecha_cotizacion LIKE ?
        GROUP BY usuario_creador ORDER BY Cotizaciones DESC
    """, (like_f,))

    meses_dict = {"":"Todos","01":"Enero","02":"Febrero","03":"Marzo","04":"Abril","05":"Mayo","06":"Junio",
                  "07":"Julio","08":"Agosto","09":"Septiembre","10":"Octubre","11":"Noviembre","12":"Diciembre"}
    anios = [str(y) for y in range(now_local().year - 1, now_local().year + 2)]

    return templates.TemplateResponse(request, "admin_rentabilidad.html", ctx(request, {
        "active": "admin",
        "itin":   df_itin.to_dict("records") if not df_itin.empty else [],
        "cobros": df_cobros.to_dict("records") if not df_cobros.empty else [],
        "cot":    df_cot.to_dict("records") if not df_cot.empty else [],
        "mes": mes, "anio": anio,
        "meses_dict": meses_dict,
        "anios": anios,
    }))


# ─── Calendario Visual ────────────────────────────────────────────────────────

@app.get("/calendario", response_class=HTMLResponse)
async def calendario(request: Request):
    if not usuario_activo(request):
        return RedirectResponse(url="/login")

    import calendar as _cal
    from datetime import datetime as _dt2, date as _date, timedelta as _td

    params = dict(request.query_params)
    hoy_d  = now_local().date()

    try:
        mes  = int(params.get("mes", hoy_d.month))
        anio = int(params.get("anio", hoy_d.year))
        mes  = max(1, min(12, mes))
    except (ValueError, TypeError):
        mes, anio = hoy_d.month, hoy_d.year

    # Navegación prev / next
    if mes == 1:   mes_prev, anio_prev = 12, anio - 1
    else:          mes_prev, anio_prev = mes - 1, anio
    if mes == 12:  mes_next, anio_next = 1, anio + 1
    else:          mes_next, anio_next = mes + 1, anio

    mes_inicio = _date(anio, mes, 1)
    mes_fin    = _date(anio, mes, _cal.monthrange(anio, mes)[1])

    # Viajes activos que se solapan con el mes
    df_viajes = obtener_datos("""
        SELECT r.id_reserva, c.nombre, r.destino, r.fecha_salida, r.fecha_regreso,
               r.venta_total, r.cobrado_cliente, r.moneda,
               r.checkin_ida, r.nombre_hotel
        FROM reservas r JOIN clientes c ON r.id_cliente = c.id_cliente
        WHERE r.estado = 'ACTIVO'
          AND r.fecha_regreso  >= ? AND r.fecha_salida <= ?
    """, (str(mes_inicio), str(mes_fin)))

    # Pagos programados pendientes del mes
    df_pagos = obtener_datos("""
        SELECT p.fecha_programada, c.nombre, ROUND(p.monto_esperado - p.monto_pagado, 2) as monto_esperado, r.moneda
        FROM plan_pagos p
        JOIN reservas r ON p.id_reserva = r.id_reserva
        JOIN clientes c ON r.id_cliente = c.id_cliente
        WHERE p.estado IN ('PENDIENTE','PARCIAL') AND r.estado = 'ACTIVO'
          AND p.fecha_programada LIKE ?
    """, (f"{anio}-{mes:02d}%",))

    # Procesar viajes
    viajes = []
    if not df_viajes.empty:
        for _, v in df_viajes.iterrows():
            try:
                sal = _dt2.strptime(str(v["fecha_salida"]), "%Y-%m-%d").date()
                reg = _dt2.strptime(str(v["fecha_regreso"]), "%Y-%m-%d").date()
            except Exception:
                continue
            saldo = float(v["venta_total"] or 0) - float(v["cobrado_cliente"] or 0)
            nombre_corto = " ".join(str(v["nombre"]).split()[:2])
            viajes.append({
                "id": int(v["id_reserva"]),
                "nombre": str(v["nombre"]),
                "nombre_corto": nombre_corto,
                "destino": str(v["destino"]),
                "salida": sal,
                "regreso": reg,
                "saldo": saldo,
                "moneda": str(v["moneda"]),
                "checkin": int(v["checkin_ida"] or 0),
            })

    # Procesar pagos en dict por fecha
    pagos_por_fecha: dict = {}
    if not df_pagos.empty:
        for _, p in df_pagos.iterrows():
            fstr = str(p["fecha_programada"])[:10]
            pagos_por_fecha.setdefault(fstr, []).append({
                "nombre": str(p["nombre"]).split()[0],
                "monto":  float(p["monto_esperado"] or 0),
                "moneda": str(p["moneda"]),
            })

    # Estadísticas del mes
    n_salidas  = sum(1 for v in viajes if v["salida"].month == mes and v["salida"].year == anio)
    n_regresos = sum(1 for v in viajes if v["regreso"].month == mes and v["regreso"].year == anio)
    n_pagos    = sum(len(ps) for ps in pagos_por_fecha.values())
    stats = {"n_activos": len(viajes), "n_salidas": n_salidas, "n_regresos": n_regresos, "n_pagos": n_pagos}

    # Construir semanas
    cal_weeks = []
    for week in _cal.monthcalendar(anio, mes):
        days = []
        for day_num in week:
            if day_num == 0:
                days.append({"num": 0, "es_hoy": False, "events": [], "extra": 0})
                continue
            curr = _date(anio, mes, day_num)
            es_hoy = (curr == hoy_d)
            curr_str = str(curr)
            events = []

            for v in viajes:
                if v["salida"] <= curr <= v["regreso"]:
                    dias_sal = (v["salida"] - curr).days
                    check_urg = (0 <= (v["salida"] - curr).days <= 3 and v["checkin"] == 0)
                    if check_urg:
                        cls = "rojo"
                    elif v["saldo"] > 1:
                        cls = "naranja"
                    else:
                        cls = "verde"
                    icono = "🛫" if curr == v["salida"] else ("🛬" if curr == v["regreso"] else "✈️")
                    saldo_str = "✅ Liquidado" if v["saldo"] <= 1 else f"⏳ Saldo ${v['saldo']:,.0f} {v['moneda']}"
                    events.append({
                        "cls": cls, "icono": icono,
                        "label": v["nombre_corto"],
                        "title": f"#{v['id']} · {v['nombre']} · {v['destino']} · {v['salida']} – {v['regreso']} · {saldo_str}",
                        "url": f"/bitacora/{v['id']}",
                    })

            for pg in pagos_por_fecha.get(curr_str, []):
                events.append({
                    "cls": "pago",
                    "icono": "💳",
                    "label": f"{pg['nombre']} ${pg['monto']:,.0f}",
                    "title": f"Pago programado: {pg['nombre']} — ${pg['monto']:,.2f} {pg['moneda']}",
                    "url": None,
                })

            visible  = events[:3]
            extra    = max(0, len(events) - 3)
            days.append({"num": day_num, "es_hoy": es_hoy, "events": visible, "extra": extra})
        cal_weeks.append(days)

    # Próximos 7 días con eventos
    proximos_7 = []
    for delta in range(7):
        dia = hoy_d + _td(days=delta)
        dia_str = str(dia)
        eventos_dia = []
        for v in viajes:
            _vn = _esc(str(v['nombre'])); _vd = _esc(str(v['destino']))
            if v["salida"] == dia:
                eventos_dia.append(f"🛫 <b>{_vn}</b> sale a <b>{_vd}</b>")
            if v["regreso"] == dia:
                eventos_dia.append(f"🛬 <b>{_vn}</b> regresa de <b>{_vd}</b>")
        for pg in pagos_por_fecha.get(dia_str, []):
            eventos_dia.append(f"💳 <b>{_esc(str(pg['nombre']))}</b> — pago ${pg['monto']:,.2f} {_esc(str(pg['moneda']))}")
        if eventos_dia:
            if delta == 0:    label = "HOY"
            elif delta == 1:  label = "MAÑANA"
            else:             label = dia.strftime("%A %d/%m").upper()
            proximos_7.append({"label": label, "eventos": eventos_dia})

    meses_dict = {"01":"Enero","02":"Febrero","03":"Marzo","04":"Abril","05":"Mayo","06":"Junio",
                  "07":"Julio","08":"Agosto","09":"Septiembre","10":"Octubre","11":"Noviembre","12":"Diciembre"}
    nombres_mes = {1:"Enero",2:"Febrero",3:"Marzo",4:"Abril",5:"Mayo",6:"Junio",
                   7:"Julio",8:"Agosto",9:"Septiembre",10:"Octubre",11:"Noviembre",12:"Diciembre"}

    return templates.TemplateResponse(request, "calendario.html", ctx(request, {
        "active": "calendario",
        "mes": mes, "anio": anio,
        "mes_prev": f"{mes_prev:02d}", "anio_prev": anio_prev,
        "mes_next": f"{mes_next:02d}", "anio_next": anio_next,
        "nombre_mes": nombres_mes[mes],
        "cal_weeks": cal_weeks,
        "stats": stats,
        "proximos_7": proximos_7,
        "meses_dict": meses_dict,
        "anios": list(range(anio - 1, anio + 3)),
    }))
