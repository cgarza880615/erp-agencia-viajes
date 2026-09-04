import sqlite3
import pandas as pd
import logging
import os
from io import BytesIO
from datetime import datetime, timedelta, timezone

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(SCRIPT_DIR, 'erp.db')

_TZ_CST = timezone(timedelta(hours=-6))

def now_local():
    """Hora actual en UTC-6 (Monterrey), sin tzinfo para compatibilidad con strftime/SQLite."""
    return datetime.now(_TZ_CST).replace(tzinfo=None)

def safe_strptime(value, date_format="%Y-%m-%d", default=None):
    """Parsea una fecha de forma segura, devuelve None si falla"""
    if value is None or value == "" or value == "None":
        return default
    if isinstance(value, str):
        try:
            return datetime.strptime(value, date_format).date()
        except (ValueError, TypeError, AttributeError):
            return default
    return default


def verificar_tablas():
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("PRAGMA journal_mode=WAL;")
    cursor.execute("PRAGMA synchronous=NORMAL;")
    cursor.execute("PRAGMA foreign_keys = ON;")

    # Se crea aquí (temprano) porque más abajo hay ALTER TABLE usuarios que asumen
    # que la tabla ya existe — en una instalación nueva (BD vacía) esos ALTER se
    # ejecutarían antes que el CREATE TABLE completo de más abajo y fallarían.
    cursor.execute('''CREATE TABLE IF NOT EXISTS usuarios (usuario TEXT PRIMARY KEY, password TEXT NOT NULL, rol TEXT NOT NULL)''')

    cursor.execute('''CREATE TABLE IF NOT EXISTS clientes (id_cliente TEXT PRIMARY KEY, nombre TEXT NOT NULL, telefono TEXT, email TEXT, fecha_nacimiento TEXT, codigo_pais TEXT DEFAULT '+52')''')

    cursor.execute('''
        CREATE TABLE IF NOT EXISTS reservas (
            id_reserva INTEGER PRIMARY KEY AUTOINCREMENT,
            id_cliente TEXT, destino TEXT NOT NULL, origen TEXT DEFAULT 'Monterrey',
            fecha_salida DATE NOT NULL, fecha_regreso DATE NOT NULL,
            fecha_limite_liquidacion DATE NOT NULL, fecha_limite_proveedor DATE, moneda TEXT DEFAULT 'MXN',

            cobro_vuelos REAL DEFAULT 0.0, cobro_tua REAL DEFAULT 0.0, cobro_hotel REAL DEFAULT 0.0, cobro_traslados REAL DEFAULT 0.0, cobro_tours REAL DEFAULT 0.0, cobro_adicionales REAL DEFAULT 0.0, especificar_adicionales TEXT,
            cobro_ajustes REAL DEFAULT 0.0, costo_ajustes REAL DEFAULT 0.0, detalle_ajustes TEXT,
            venta_total REAL NOT NULL,

            costo_vuelos REAL DEFAULT 0.0, costo_tua REAL DEFAULT 0.0, costo_hotel REAL DEFAULT 0.0, costo_traslados REAL DEFAULT 0.0, costo_tours REAL DEFAULT 0.0, costo_adicionales REAL DEFAULT 0.0, costo_comisiones REAL DEFAULT 0.0, costo_total REAL NOT NULL, utilidad_proyectada REAL NOT NULL,
            cobrado_cliente REAL DEFAULT 0.0,

            mayorista TEXT, aerolinea TEXT, itinerario_hotel_plataforma TEXT, itinerario_vuelo_plataforma TEXT, confirmacion_proveedor_traslados TEXT,
            es_paquete_global INTEGER DEFAULT 0, localizador_global TEXT, nombre_hotel TEXT, proveedor_traslados TEXT, proveedor_tours TEXT, confirmacion_proveedor_tours TEXT, proveedor_adicionales TEXT, confirmacion_proveedor_adicionales TEXT,

            fecha_vuelo_ida TEXT, hora_vuelo_ida TEXT, fecha_vuelo_vuelta TEXT, hora_vuelo_vuelta TEXT,
            restricciones_medicas TEXT, solicitudes_especiales TEXT, comentarios_operativos TEXT, notas_abiertas TEXT,

            checkin_ida INTEGER DEFAULT 0, checkin_regreso INTEGER DEFAULT 0, hotel_confirmado INTEGER DEFAULT 0, hotel_liquidado INTEGER DEFAULT 0,
            estado TEXT DEFAULT 'ACTIVO', monto_reembolsado REAL DEFAULT 0.0, perdida_cancelacion REAL DEFAULT 0.0, notas_cancelacion TEXT,
            FOREIGN KEY (id_cliente) REFERENCES clientes(id_cliente)
        )
    ''')

    cursor.execute('''CREATE TABLE IF NOT EXISTS pasajeros_reserva (id_pasajero INTEGER PRIMARY KEY AUTOINCREMENT, id_reserva INTEGER, nombre TEXT NOT NULL, fecha_nacimiento TEXT NOT NULL, parentesco TEXT NOT NULL, FOREIGN KEY (id_reserva) REFERENCES reservas(id_reserva))''')
    cursor.execute('''CREATE TABLE IF NOT EXISTS ajustes_reserva (id_ajuste INTEGER PRIMARY KEY AUTOINCREMENT, id_reserva INTEGER, detalle TEXT, cobro REAL, costo REAL, fecha TEXT, FOREIGN KEY (id_reserva) REFERENCES reservas(id_reserva))''')
    cursor.execute('''CREATE TABLE IF NOT EXISTS plan_pagos (id_pago INTEGER PRIMARY KEY AUTOINCREMENT, id_reserva INTEGER, numero_pago INTEGER, monto_esperado REAL, monto_pagado REAL DEFAULT 0, fecha_programada TEXT, estado TEXT DEFAULT 'PENDIENTE', FOREIGN KEY (id_reserva) REFERENCES reservas(id_reserva))''')
    cursor.execute('''CREATE TABLE IF NOT EXISTS flujo_caja (id_movimiento INTEGER PRIMARY KEY AUTOINCREMENT, id_reserva INTEGER, tipo_movimiento TEXT CHECK(tipo_movimiento IN ('INGRESO', 'EGRESO')), tipo_egreso TEXT, categoria TEXT NOT NULL, concepto TEXT NOT NULL, monto REAL NOT NULL, moneda TEXT DEFAULT 'MXN', fecha_pago DATE NOT NULL, estado TEXT DEFAULT 'ACTIVO', motivo_anulacion TEXT, usuario_creador TEXT DEFAULT 'SISTEMA', fecha_creacion TEXT, metodo_pago TEXT, FOREIGN KEY (id_reserva) REFERENCES reservas(id_reserva))''')

    cursor.execute('''CREATE TABLE IF NOT EXISTS extras_viaje (
        id_extra INTEGER PRIMARY KEY AUTOINCREMENT,
        id_reserva INTEGER NOT NULL,
        descripcion TEXT NOT NULL,
        monto_cobrado_cliente REAL NOT NULL,
        monto_costo_proveedor REAL NOT NULL,
        metodo_pago TEXT,
        fecha_registro TEXT,
        tipo_registro TEXT CHECK(tipo_registro IN ('PAGADO_MOMENTO', 'PLAN_PAGOS', 'ABONO_LIBRE')),
        usuario_creador TEXT,
        FOREIGN KEY (id_reserva) REFERENCES reservas(id_reserva)
    )''')

    # Migración: ampliar CHECK constraint de tipo_registro para incluir 'ABONO_LIBRE'
    # (SQLite no permite ALTER de un CHECK existente; se reconstruye la tabla si aún tiene el constraint viejo)
    cursor.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='extras_viaje'")
    _row_ev = cursor.fetchone()
    if _row_ev and _row_ev[0] and 'ABONO_LIBRE' not in _row_ev[0]:
        cursor.execute('''CREATE TABLE extras_viaje_new (
            id_extra INTEGER PRIMARY KEY AUTOINCREMENT,
            id_reserva INTEGER NOT NULL,
            descripcion TEXT NOT NULL,
            monto_cobrado_cliente REAL NOT NULL,
            monto_costo_proveedor REAL NOT NULL,
            metodo_pago TEXT,
            fecha_registro TEXT,
            tipo_registro TEXT CHECK(tipo_registro IN ('PAGADO_MOMENTO', 'PLAN_PAGOS', 'ABONO_LIBRE')),
            usuario_creador TEXT,
            FOREIGN KEY (id_reserva) REFERENCES reservas(id_reserva)
        )''')
        cursor.execute("INSERT INTO extras_viaje_new SELECT * FROM extras_viaje")
        cursor.execute("DROP TABLE extras_viaje")
        cursor.execute("ALTER TABLE extras_viaje_new RENAME TO extras_viaje")

    cursor.execute('''CREATE TABLE IF NOT EXISTS anulaciones_audit (
        id_anulacion INTEGER PRIMARY KEY AUTOINCREMENT,
        id_movimiento INTEGER,
        tipo_movimiento TEXT,
        monto_anulado REAL,
        usuario_anulo TEXT NOT NULL,
        fecha_anulacion TEXT,
        razon_anulacion TEXT,
        movimiento_original TEXT,
        FOREIGN KEY (id_movimiento) REFERENCES flujo_caja(id_movimiento)
    )''')

    cursor.execute('''CREATE TABLE IF NOT EXISTS egresos_oficina (
        id_egreso INTEGER PRIMARY KEY AUTOINCREMENT,
        tipo_egreso TEXT NOT NULL,
        categoria TEXT,
        monto REAL NOT NULL,
        metodo_pago TEXT,
        fecha_egreso DATE,
        concepto TEXT,
        usuario_creador TEXT,
        fecha_creacion TEXT,
        estado TEXT DEFAULT 'ACTIVO',
        divisor TEXT
    )''')

    cursor.execute('''CREATE TABLE IF NOT EXISTS metodos_pago (
        id INTEGER PRIMARY KEY,
        nombre TEXT UNIQUE NOT NULL,
        categoria TEXT,
        activo BOOLEAN DEFAULT 1
    )''')

    for col_sql in [
        "ALTER TABLE flujo_caja ADD COLUMN usuario_creador TEXT DEFAULT 'SISTEMA'",
        "ALTER TABLE flujo_caja ADD COLUMN fecha_creacion TEXT",
        "ALTER TABLE flujo_caja ADD COLUMN metodo_pago TEXT",
    ]:
        try:
            cursor.execute(col_sql)
        except Exception:
            pass

    cursor.execute('''CREATE TABLE IF NOT EXISTS acompanantes_cliente (
        id_acompanante INTEGER PRIMARY KEY AUTOINCREMENT,
        id_cliente TEXT NOT NULL,
        nombre TEXT NOT NULL,
        fecha_nacimiento TEXT,
        parentesco TEXT,
        FOREIGN KEY (id_cliente) REFERENCES clientes(id_cliente)
    )''')

    cursor.execute('''CREATE TABLE IF NOT EXISTS cotizaciones (
        id_cotizacion INTEGER PRIMARY KEY AUTOINCREMENT,
        id_cliente TEXT NOT NULL,
        destino TEXT NOT NULL,
        origen TEXT DEFAULT 'Monterrey',
        fecha_salida DATE NOT NULL,
        fecha_regreso DATE NOT NULL,
        moneda TEXT DEFAULT 'MXN',
        num_adultos INTEGER DEFAULT 1,
        num_menores INTEGER DEFAULT 0,
        incluye_vuelo INTEGER DEFAULT 0,
        incluye_tua INTEGER DEFAULT 0,
        incluye_hotel INTEGER DEFAULT 0,
        incluye_traslado INTEGER DEFAULT 0,
        incluye_tours INTEGER DEFAULT 0,
        cobro_vuelos REAL DEFAULT 0,
        cobro_tua REAL DEFAULT 0,
        cobro_hotel REAL DEFAULT 0,
        cobro_traslados REAL DEFAULT 0,
        cobro_tours REAL DEFAULT 0,
        cobro_adicionales REAL DEFAULT 0,
        especificar_adicionales TEXT,
        venta_total REAL DEFAULT 0,
        nombre_hotel TEXT,
        aerolinea TEXT,
        mayorista TEXT,
        anticipo_requerido REAL DEFAULT 0,
        tipo_plan TEXT DEFAULT 'Sin Plan (Libre)',
        dia_mensual INTEGER DEFAULT 5,
        fecha_limite_pago DATE,
        notas TEXT,
        dias_vigencia INTEGER DEFAULT 7,
        estado TEXT DEFAULT 'PENDIENTE',
        fecha_cotizacion TEXT,
        fecha_vencimiento TEXT,
        convertida_a_reserva INTEGER,
        usuario_creador TEXT,
        costo_vuelos REAL DEFAULT 0,
        costo_tua REAL DEFAULT 0,
        costo_hotel REAL DEFAULT 0,
        costo_traslados REAL DEFAULT 0,
        costo_tours REAL DEFAULT 0,
        costo_adicionales REAL DEFAULT 0,
        costo_total REAL DEFAULT 0,
        utilidad_proyectada REAL DEFAULT 0,
        FOREIGN KEY (id_cliente) REFERENCES clientes(id_cliente)
    )''')

    cursor.execute('''CREATE TABLE IF NOT EXISTS habitaciones_reserva (id_habitacion INTEGER PRIMARY KEY AUTOINCREMENT, id_reserva INTEGER, tipo_habitacion TEXT NOT NULL, num_personas INTEGER NOT NULL DEFAULT 1, hora_checkin TEXT DEFAULT '15:00', descripcion TEXT, FOREIGN KEY (id_reserva) REFERENCES reservas(id_reserva))''')
    cursor.execute('''CREATE TABLE IF NOT EXISTS habitaciones_cotizacion (id_habitacion INTEGER PRIMARY KEY AUTOINCREMENT, id_cotizacion INTEGER, tipo_habitacion TEXT NOT NULL, num_personas INTEGER NOT NULL DEFAULT 1, hora_checkin TEXT DEFAULT '15:00', descripcion TEXT, FOREIGN KEY (id_cotizacion) REFERENCES cotizaciones(id_cotizacion))''')

    # ── Vuelos por tramo y hoteles múltiples (logística — el dinero sigue siendo
    # un solo total en cobro_vuelos/costo_vuelos y cobro_hotel/costo_hotel,
    # sin importar cuántas filas haya aquí) ──────────────────────────────────
    cursor.execute('''CREATE TABLE IF NOT EXISTS vuelos_reserva (id_vuelo INTEGER PRIMARY KEY AUTOINCREMENT, id_reserva INTEGER, numero_tramo INTEGER NOT NULL DEFAULT 1, aerolinea TEXT, numero_vuelo TEXT, origen TEXT, destino TEXT, fecha TEXT, hora TEXT, localizador TEXT, checkin INTEGER DEFAULT 0, FOREIGN KEY (id_reserva) REFERENCES reservas(id_reserva))''')
    cursor.execute('''CREATE TABLE IF NOT EXISTS vuelos_cotizacion (id_vuelo INTEGER PRIMARY KEY AUTOINCREMENT, id_cotizacion INTEGER, numero_tramo INTEGER NOT NULL DEFAULT 1, aerolinea TEXT, numero_vuelo TEXT, origen TEXT, destino TEXT, fecha TEXT, hora TEXT, localizador TEXT, FOREIGN KEY (id_cotizacion) REFERENCES cotizaciones(id_cotizacion))''')
    cursor.execute('''CREATE TABLE IF NOT EXISTS hoteles_reserva (id_hotel_itin INTEGER PRIMARY KEY AUTOINCREMENT, id_reserva INTEGER, numero_orden INTEGER NOT NULL DEFAULT 1, ciudad_destino TEXT, nombre_hotel TEXT, localizador TEXT, fecha_checkin TEXT, fecha_checkout TEXT, FOREIGN KEY (id_reserva) REFERENCES reservas(id_reserva))''')
    cursor.execute('''CREATE TABLE IF NOT EXISTS hoteles_cotizacion (id_hotel_itin INTEGER PRIMARY KEY AUTOINCREMENT, id_cotizacion INTEGER, numero_orden INTEGER NOT NULL DEFAULT 1, ciudad_destino TEXT, nombre_hotel TEXT, localizador TEXT, fecha_checkin TEXT, fecha_checkout TEXT, FOREIGN KEY (id_cotizacion) REFERENCES cotizaciones(id_cotizacion))''')

    for _tv_col in [
        "ALTER TABLE reservas ADD COLUMN tipo_vuelo TEXT DEFAULT 'REDONDO'",
        "ALTER TABLE cotizaciones ADD COLUMN tipo_vuelo TEXT DEFAULT 'REDONDO'",
        "ALTER TABLE vuelos_reserva ADD COLUMN checkin INTEGER DEFAULT 0",
    ]:
        try: cursor.execute(_tv_col)
        except Exception as e:
            if "duplicate column" not in str(e).lower(): logging.error(f"ALTER ({_tv_col[:50]}): {e}")

    try: cursor.execute("ALTER TABLE clientes ADD COLUMN codigo_pais TEXT DEFAULT '+52'")
    except Exception as e:
        if "duplicate column" not in str(e).lower(): logging.error(f"ALTER clientes codigo_pais: {e}")

    try: cursor.execute("ALTER TABLE reservas ADD COLUMN usuario_creador TEXT DEFAULT 'sistema'")
    except Exception as e:
        if "duplicate column" not in str(e).lower(): logging.error(f"ALTER reservas usuario_creador: {e}")

    try: cursor.execute("ALTER TABLE reservas ADD COLUMN token_portal TEXT")
    except Exception as e:
        if "duplicate column" not in str(e).lower(): logging.error(f"ALTER reservas token_portal: {e}")
    cursor.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_reservas_token_portal ON reservas(token_portal)")

    try: cursor.execute("ALTER TABLE usuarios ADD COLUMN ultima_cambio_password TEXT")
    except Exception as e:
        if "duplicate column" not in str(e).lower() and "no such table" not in str(e).lower():
            logging.error(f"ALTER usuarios ultima_cambio_password: {e}")
    try: cursor.execute("ALTER TABLE usuarios ADD COLUMN primer_login INTEGER DEFAULT 0")
    except Exception as e:
        if "duplicate column" not in str(e).lower() and "no such table" not in str(e).lower():
            logging.error(f"ALTER usuarios primer_login: {e}")

    cursor.execute('''CREATE TABLE IF NOT EXISTS historial_passwords (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        usuario TEXT NOT NULL,
        password_hash TEXT NOT NULL,
        fecha_cambio TEXT NOT NULL
    )''')
    try: cursor.execute("UPDATE usuarios SET ultima_cambio_password = ? WHERE ultima_cambio_password IS NULL", (str(now_local().date()),))
    except Exception as e:
        if "no such table" not in str(e).lower(): logging.error(f"UPDATE usuarios ultima_cambio_password: {e}")

    for _cot_col in [
        "ALTER TABLE cotizaciones ADD COLUMN costo_vuelos REAL DEFAULT 0",
        "ALTER TABLE cotizaciones ADD COLUMN costo_tua REAL DEFAULT 0",
        "ALTER TABLE cotizaciones ADD COLUMN costo_hotel REAL DEFAULT 0",
        "ALTER TABLE cotizaciones ADD COLUMN costo_traslados REAL DEFAULT 0",
        "ALTER TABLE cotizaciones ADD COLUMN costo_tours REAL DEFAULT 0",
        "ALTER TABLE cotizaciones ADD COLUMN costo_adicionales REAL DEFAULT 0",
        "ALTER TABLE cotizaciones ADD COLUMN costo_total REAL DEFAULT 0",
        "ALTER TABLE cotizaciones ADD COLUMN utilidad_proyectada REAL DEFAULT 0",
        "ALTER TABLE cotizaciones ADD COLUMN fecha_limite_proveedor DATE",
        "ALTER TABLE cotizaciones ADD COLUMN proveedor_traslados TEXT",
        "ALTER TABLE cotizaciones ADD COLUMN proveedor_tours TEXT",
        "ALTER TABLE cotizaciones ADD COLUMN hotel_op2_nombre TEXT DEFAULT ''",
        "ALTER TABLE cotizaciones ADD COLUMN hotel_op2_cobro REAL DEFAULT 0",
        "ALTER TABLE cotizaciones ADD COLUMN hotel_op2_costo REAL DEFAULT 0",
        "ALTER TABLE cotizaciones ADD COLUMN hotel_op3_nombre TEXT DEFAULT ''",
        "ALTER TABLE cotizaciones ADD COLUMN hotel_op3_cobro REAL DEFAULT 0",
        "ALTER TABLE cotizaciones ADD COLUMN hotel_op3_costo REAL DEFAULT 0",
        "ALTER TABLE cotizaciones ADD COLUMN hotel_opcion_elegida INTEGER DEFAULT 1",
        "ALTER TABLE cotizaciones ADD COLUMN fecha_vuelo_ida TEXT DEFAULT ''",
        "ALTER TABLE cotizaciones ADD COLUMN hora_vuelo_ida TEXT DEFAULT ''",
        "ALTER TABLE cotizaciones ADD COLUMN fecha_vuelo_vuelta TEXT DEFAULT ''",
        "ALTER TABLE cotizaciones ADD COLUMN hora_vuelo_vuelta TEXT DEFAULT ''",
    ]:
        try: cursor.execute(_cot_col)
        except Exception as e:
            if "duplicate column" not in str(e).lower(): logging.error(f"ALTER cotizaciones ({_cot_col[:40]}): {e}")

    try: cursor.execute("ALTER TABLE reservas ADD COLUMN fecha_creacion DATE")
    except Exception as e:
        if "duplicate column" not in str(e).lower(): logging.error(f"ALTER reservas fecha_creacion: {e}")

    for _equip_col in [
        "ALTER TABLE reservas ADD COLUMN detalle_equipaje TEXT",
        "ALTER TABLE cotizaciones ADD COLUMN detalle_equipaje TEXT",
    ]:
        try: cursor.execute(_equip_col)
        except Exception as e:
            if "duplicate column" not in str(e).lower(): logging.error(f"ALTER ({_equip_col[:50]}): {e}")

    # ── Viajes Grupales (réplica del modelo ya en producción en el_sistema_legado/Streamlit) ──
    cursor.execute('''CREATE TABLE IF NOT EXISTS grupos_viaje (
        id_grupo INTEGER PRIMARY KEY AUTOINCREMENT,
        nombre_grupo TEXT NOT NULL, destino TEXT NOT NULL, origen TEXT DEFAULT 'Monterrey',
        fecha_salida DATE NOT NULL, fecha_regreso DATE NOT NULL, moneda TEXT DEFAULT 'MXN',
        cupos_bloqueados INTEGER NOT NULL DEFAULT 0, descripcion_paquete TEXT,
        precio_paquete_base REAL NOT NULL DEFAULT 0.0, margen_deseado_pct REAL DEFAULT 0.0,
        estado TEXT DEFAULT 'ACTIVO', usuario_creador TEXT DEFAULT 'sistema', fecha_creacion TEXT
    )''')
    cursor.execute('''CREATE TABLE IF NOT EXISTS grupo_presupuesto (
        id_presupuesto INTEGER PRIMARY KEY AUTOINCREMENT, id_grupo INTEGER NOT NULL,
        categoria TEXT NOT NULL, descripcion TEXT, monto_presupuestado REAL NOT NULL DEFAULT 0.0,
        FOREIGN KEY (id_grupo) REFERENCES grupos_viaje(id_grupo)
    )''')
    cursor.execute('''CREATE TABLE IF NOT EXISTS grupo_logistica (
        id_logistica INTEGER PRIMARY KEY AUTOINCREMENT, id_grupo INTEGER NOT NULL,
        tipo TEXT NOT NULL, nombre TEXT, confirmacion TEXT, detalle TEXT,
        fecha_salida TEXT, fecha_regreso TEXT, hora_ida TEXT, hora_vuelta TEXT,
        FOREIGN KEY (id_grupo) REFERENCES grupos_viaje(id_grupo)
    )''')
    for _grp_col in [
        "ALTER TABLE reservas ADD COLUMN id_grupo INTEGER",
        "ALTER TABLE reservas ADD COLUMN num_pax INTEGER DEFAULT 1",
        "ALTER TABLE flujo_caja ADD COLUMN id_grupo INTEGER",
        "ALTER TABLE flujo_caja ADD COLUMN id_presupuesto INTEGER",
    ]:
        try: cursor.execute(_grp_col)
        except Exception as e:
            if "duplicate column" not in str(e).lower(): logging.error(f"ALTER grupo ({_grp_col[:40]}): {e}")

    # Metadatos de "pago final" en flujo_caja: qué columna de costo se ajustó y con qué
    # valores, para poder revertir el ajuste con precisión si el pago se anula después.
    for _fc_col in [
        "ALTER TABLE flujo_caja ADD COLUMN ajuste_costo_columna TEXT",
        "ALTER TABLE flujo_caja ADD COLUMN ajuste_costo_anterior REAL",
        "ALTER TABLE flujo_caja ADD COLUMN ajuste_costo_nuevo REAL",
        # Vincula la comisión bancaria automática (EGRESO) con el abono (INGRESO) que la
        # originó, para poder cancelarla si el abono se anula después ("si se anula el
        # pago es que no se hizo" — la comisión tampoco se cobró).
        "ALTER TABLE flujo_caja ADD COLUMN id_movimiento_vinculado INTEGER",
    ]:
        try: cursor.execute(_fc_col)
        except Exception as e:
            if "duplicate column" not in str(e).lower(): logging.error(f"ALTER flujo_caja ({_fc_col[:45]}): {e}")

    # monto_esperado de plan_pagos es el compromiso FIJO original de cada parcialidad
    # (ya no se muta al recibir abonos parciales). monto_pagado acumula lo realmente cobrado
    # para esa parcialidad — ver actualizar_estado_plan_pagos().
    try: cursor.execute("ALTER TABLE plan_pagos ADD COLUMN monto_pagado REAL DEFAULT 0")
    except Exception as e:
        if "duplicate column" not in str(e).lower(): logging.error(f"ALTER plan_pagos monto_pagado: {e}")

    # Vincula un extra "Agregar al Plan" con la fila de plan_pagos dedicada que se crea
    # para él, en vez de repartir/quitar su monto entre las parcialidades existentes.
    try: cursor.execute("ALTER TABLE extras_viaje ADD COLUMN id_pago_plan INTEGER")
    except Exception as e:
        if "duplicate column" not in str(e).lower(): logging.error(f"ALTER extras_viaje id_pago_plan: {e}")

    # Vincula un movimiento de flujo_caja con el extra que lo originó, para poder anularlo
    # con precisión (por ID) en vez de buscarlo por monto.
    try: cursor.execute("ALTER TABLE flujo_caja ADD COLUMN id_extra INTEGER")
    except Exception as e:
        if "duplicate column" not in str(e).lower(): logging.error(f"ALTER flujo_caja id_extra: {e}")

    cursor.execute('''CREATE TABLE IF NOT EXISTS sesiones_activas (
        usuario TEXT PRIMARY KEY,
        rol TEXT,
        ultima_actividad TEXT
    )''')

    cursor.execute('''CREATE TABLE IF NOT EXISTS bitacora_cambios (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        id_reserva INTEGER NOT NULL,
        usuario TEXT NOT NULL,
        fecha TEXT NOT NULL,
        accion TEXT NOT NULL,
        detalle TEXT,
        FOREIGN KEY (id_reserva) REFERENCES reservas(id_reserva)
    )''')

    cursor.execute("SELECT COUNT(*) FROM acompanantes_cliente")
    if cursor.fetchone()[0] == 0:
        cursor.execute("""
            INSERT INTO acompanantes_cliente (id_cliente, nombre, fecha_nacimiento, parentesco)
            SELECT DISTINCT r.id_cliente, p.nombre, p.fecha_nacimiento, p.parentesco
            FROM pasajeros_reserva p
            JOIN reservas r ON p.id_reserva = r.id_reserva
            WHERE p.nombre IS NOT NULL AND p.nombre != ''
        """)

    cursor.execute('''CREATE TABLE IF NOT EXISTS usuarios (usuario TEXT PRIMARY KEY, password TEXT NOT NULL, rol TEXT NOT NULL)''')

    cursor.execute("SELECT COUNT(*) FROM usuarios")
    if cursor.fetchone()[0] == 0:
        from auth import encriptar_password as _enc
        _pass_hash = _enc("admin")
        cursor.execute(
            "INSERT INTO usuarios (usuario, password, rol, ultima_cambio_password, primer_login) VALUES (?, ?, ?, ?, ?)",
            ("admin", _pass_hash, "admin", str(now_local().date()), 1)
        )

    cursor.execute('''CREATE TABLE IF NOT EXISTS login_intentos (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        usuario TEXT NOT NULL,
        fecha_intento TEXT NOT NULL,
        exitoso INTEGER DEFAULT 0
    )''')

    cursor.execute('''CREATE TABLE IF NOT EXISTS wa_conversaciones (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        wa_id TEXT UNIQUE NOT NULL,
        nombre TEXT,
        telefono TEXT,
        id_cliente TEXT REFERENCES clientes(id_cliente),
        id_reserva INTEGER REFERENCES reservas(id_reserva),
        agente_asignado TEXT,
        estado TEXT DEFAULT 'NUEVO',
        ultimo_mensaje TEXT,
        ultimo_mensaje_ts DATETIME,
        fecha_creacion DATETIME DEFAULT CURRENT_TIMESTAMP
    )''')

    cursor.execute('''CREATE TABLE IF NOT EXISTS wa_mensajes (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        id_conversacion INTEGER NOT NULL REFERENCES wa_conversaciones(id),
        wa_message_id TEXT UNIQUE,
        direccion TEXT NOT NULL,
        tipo TEXT DEFAULT 'texto',
        contenido TEXT,
        media_id TEXT,
        estado TEXT DEFAULT 'enviado',
        timestamp DATETIME,
        usuario_envio TEXT,
        fecha_creacion DATETIME DEFAULT CURRENT_TIMESTAMP
    )''')

    cursor.execute('''CREATE TABLE IF NOT EXISTS wa_etapas_funnel (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        nombre TEXT NOT NULL,
        emoji TEXT DEFAULT '',
        color TEXT DEFAULT '#6b7280',
        orden INTEGER DEFAULT 0,
        activo INTEGER DEFAULT 1
    )''')

    cursor.execute('''CREATE TABLE IF NOT EXISTS wa_etiquetas (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        nombre TEXT NOT NULL,
        color TEXT DEFAULT '#3b82f6',
        orden INTEGER DEFAULT 0,
        activo INTEGER DEFAULT 1
    )''')

    cursor.execute('''CREATE TABLE IF NOT EXISTS wa_conv_etiquetas (
        id_conversacion INTEGER NOT NULL REFERENCES wa_conversaciones(id),
        id_etiqueta INTEGER NOT NULL REFERENCES wa_etiquetas(id),
        PRIMARY KEY (id_conversacion, id_etiqueta)
    )''')

    # id_etapa_funnel en wa_conversaciones
    try:
        cursor.execute("ALTER TABLE wa_conversaciones ADD COLUMN id_etapa_funnel INTEGER REFERENCES wa_etapas_funnel(id)")
    except Exception:
        pass

    # Seeds: etapas del funnel
    cursor.execute("SELECT COUNT(*) FROM wa_etapas_funnel")
    if cursor.fetchone()[0] == 0:
        etapas_seed = [
            ("Nuevo lead",   "🆕", "#6b7280", 1),
            ("Contactado",   "📞", "#3b82f6", 2),
            ("Interesado",   "💡", "#f59e0b", 3),
            ("Cotizado",     "📄", "#8b5cf6", 4),
            ("Negociando",   "🤝", "#f97316", 5),
            ("Reservado",    "✅", "#22c55e", 6),
            ("Perdido",      "❌", "#ef4444", 7),
        ]
        cursor.executemany(
            "INSERT INTO wa_etapas_funnel (nombre, emoji, color, orden) VALUES (?,?,?,?)",
            etapas_seed
        )

    # Seeds: etiquetas
    cursor.execute("SELECT COUNT(*) FROM wa_etiquetas")
    if cursor.fetchone()[0] == 0:
        etiquetas_seed = [
            ("VIP",           "#f59e0b", 1),
            ("Urgente",       "#ef4444", 2),
            ("Grupo",         "#3b82f6", 3),
            ("Recurrente",    "#22c55e", 4),
            ("Pendiente info","#f97316", 5),
        ]
        cursor.executemany(
            "INSERT INTO wa_etiquetas (nombre, color, orden) VALUES (?,?,?)",
            etiquetas_seed
        )

    cursor.executescript("""
        CREATE INDEX IF NOT EXISTS idx_wa_conv_estado ON wa_conversaciones(estado);
        CREATE INDEX IF NOT EXISTS idx_wa_conv_ts     ON wa_conversaciones(ultimo_mensaje_ts);
        CREATE INDEX IF NOT EXISTS idx_wa_msg_conv    ON wa_mensajes(id_conversacion);
        CREATE INDEX IF NOT EXISTS idx_wa_conv_etiq   ON wa_conv_etiquetas(id_conversacion);
    """)

    cursor.execute('''CREATE TABLE IF NOT EXISTS asistente_ia_conversaciones (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        usuario TEXT NOT NULL,
        fecha_creacion DATETIME DEFAULT CURRENT_TIMESTAMP,
        fecha_ultimo_mensaje DATETIME
    )''')

    cursor.execute('''CREATE TABLE IF NOT EXISTS asistente_ia_mensajes (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        id_conversacion INTEGER NOT NULL REFERENCES asistente_ia_conversaciones(id),
        role TEXT NOT NULL,
        content_json TEXT NOT NULL,
        fecha_creacion DATETIME DEFAULT CURRENT_TIMESTAMP
    )''')

    cursor.execute('''CREATE TABLE IF NOT EXISTS asistente_ia_cotizaciones_pendientes (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        id_conversacion INTEGER NOT NULL REFERENCES asistente_ia_conversaciones(id),
        payload_json TEXT NOT NULL,
        resumen_texto TEXT NOT NULL,
        estado TEXT DEFAULT 'PENDIENTE_CONFIRMACION',
        fecha_creacion DATETIME DEFAULT CURRENT_TIMESTAMP
    )''')

    cursor.execute('''CREATE TABLE IF NOT EXISTS asistente_ia_clientes_pendientes (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        id_conversacion INTEGER NOT NULL REFERENCES asistente_ia_conversaciones(id),
        payload_json TEXT NOT NULL,
        resumen_texto TEXT NOT NULL,
        estado TEXT DEFAULT 'PENDIENTE_CONFIRMACION',
        fecha_creacion DATETIME DEFAULT CURRENT_TIMESTAMP
    )''')

    try: cursor.execute("ALTER TABLE asistente_ia_clientes_pendientes ADD COLUMN id_cliente_existente TEXT")
    except Exception as e:
        if "duplicate column" not in str(e).lower(): logging.error(f"ALTER asistente_ia_clientes_pendientes: {e}")

    cursor.execute('''CREATE TABLE IF NOT EXISTS asistente_ia_uso_api (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        fecha_hora DATETIME DEFAULT CURRENT_TIMESTAMP,
        tokens_entrada INTEGER DEFAULT 0,
        tokens_salida INTEGER DEFAULT 0,
        tokens_total INTEGER DEFAULT 0
    )''')

    cursor.executescript("""
        CREATE INDEX IF NOT EXISTS idx_asistente_ia_conv_usuario ON asistente_ia_conversaciones(usuario);
        CREATE INDEX IF NOT EXISTS idx_asistente_ia_msg_conv     ON asistente_ia_mensajes(id_conversacion);
        CREATE INDEX IF NOT EXISTS idx_asistente_ia_cotpend_conv ON asistente_ia_cotizaciones_pendientes(id_conversacion);
        CREATE INDEX IF NOT EXISTS idx_asistente_ia_clipend_conv ON asistente_ia_clientes_pendientes(id_conversacion);
        CREATE INDEX IF NOT EXISTS idx_asistente_ia_uso_fecha     ON asistente_ia_uso_api(fecha_hora);
    """)

    cursor.execute('''CREATE TABLE IF NOT EXISTS destinos_catalogo (id INTEGER PRIMARY KEY AUTOINCREMENT, nombre TEXT NOT NULL UNIQUE)''')
    cursor.execute('''CREATE TABLE IF NOT EXISTS hoteles_catalogo (id INTEGER PRIMARY KEY AUTOINCREMENT, nombre TEXT NOT NULL UNIQUE)''')
    cursor.execute('''CREATE TABLE IF NOT EXISTS aerolineas_catalogo (id INTEGER PRIMARY KEY AUTOINCREMENT, nombre TEXT NOT NULL UNIQUE)''')
    cursor.execute('''CREATE TABLE IF NOT EXISTS mayoristas_catalogo (id INTEGER PRIMARY KEY AUTOINCREMENT, nombre TEXT NOT NULL UNIQUE)''')
    cursor.execute("INSERT OR IGNORE INTO mayoristas_catalogo (nombre) VALUES ('Reserva Directa (sin mayorista)')")
    cursor.execute('''CREATE TABLE IF NOT EXISTS proveedores_traslados_catalogo (id INTEGER PRIMARY KEY AUTOINCREMENT, nombre TEXT NOT NULL UNIQUE)''')
    cursor.execute('''CREATE TABLE IF NOT EXISTS proveedores_tours_catalogo (id INTEGER PRIMARY KEY AUTOINCREMENT, nombre TEXT NOT NULL UNIQUE)''')
    cursor.execute('''CREATE TABLE IF NOT EXISTS proveedores_adicionales_catalogo (id INTEGER PRIMARY KEY AUTOINCREMENT, nombre TEXT NOT NULL UNIQUE)''')
    cursor.execute('''CREATE TABLE IF NOT EXISTS equipaje_catalogo (id INTEGER PRIMARY KEY AUTOINCREMENT, nombre TEXT NOT NULL UNIQUE)''')
    for _equip_combo in [
        "Objeto personal únicamente",
        "Objeto personal + equipaje de mano 10 kg",
        "Objeto personal + equipaje de mano 15 kg",
        "Objeto personal + equipaje de mano 20 kg",
        "Objeto personal + equipaje de mano (sin límite de peso, debe caber en compartimento superior)",
        "Objeto personal + equipaje de mano + 1 documentada 15 kg",
        "Objeto personal + equipaje de mano + 1 documentada 20 kg",
        "Objeto personal + equipaje de mano + 1 documentada 23 kg",
        "Objeto personal + equipaje de mano + 1 documentada 25 kg",
        "Objeto personal + equipaje de mano + 1 documentada 32 kg",
        "Objeto personal + equipaje de mano + 2 documentadas 23 kg c/u",
    ]:
        cursor.execute("INSERT OR IGNORE INTO equipaje_catalogo (nombre) VALUES (?)", (_equip_combo,))

    cursor.executescript("""
        CREATE INDEX IF NOT EXISTS idx_reservas_estado        ON reservas(estado);
        CREATE INDEX IF NOT EXISTS idx_reservas_fecha_regreso ON reservas(fecha_regreso);
        CREATE INDEX IF NOT EXISTS idx_reservas_fecha_salida  ON reservas(fecha_salida);
        CREATE INDEX IF NOT EXISTS idx_reservas_cliente       ON reservas(id_cliente);
        CREATE INDEX IF NOT EXISTS idx_flujo_id_reserva       ON flujo_caja(id_reserva);
        CREATE INDEX IF NOT EXISTS idx_flujo_tipo_estado      ON flujo_caja(tipo_movimiento, estado);
        CREATE INDEX IF NOT EXISTS idx_flujo_fecha_pago       ON flujo_caja(fecha_pago);
        CREATE INDEX IF NOT EXISTS idx_plan_id_reserva        ON plan_pagos(id_reserva);
        CREATE INDEX IF NOT EXISTS idx_plan_estado            ON plan_pagos(estado);
        CREATE INDEX IF NOT EXISTS idx_plan_reserva_estado    ON plan_pagos(id_reserva, estado);
        CREATE INDEX IF NOT EXISTS idx_extras_id_reserva      ON extras_viaje(id_reserva);
        CREATE INDEX IF NOT EXISTS idx_bitacora_id_reserva    ON bitacora_cambios(id_reserva);
        CREATE INDEX IF NOT EXISTS idx_pasajeros_id_reserva   ON pasajeros_reserva(id_reserva);
        CREATE INDEX IF NOT EXISTS idx_ajustes_id_reserva     ON ajustes_reserva(id_reserva);
        CREATE INDEX IF NOT EXISTS idx_cot_cliente            ON cotizaciones(id_cliente);
        CREATE INDEX IF NOT EXISTS idx_cot_estado             ON cotizaciones(estado);
        CREATE INDEX IF NOT EXISTS idx_acomp_cliente          ON acompanantes_cliente(id_cliente);
        CREATE INDEX IF NOT EXISTS idx_login_usuario          ON login_intentos(usuario, exitoso);
    """)
    conn.commit()
    conn.close()


def obtener_datos(query, params=()):
    with sqlite3.connect(DB_PATH) as conn:
        df = pd.read_sql_query(query, conn, params=params)
    # Una columna de texto con NULL mezclado con valores reales llega de pandas como
    # NaN (float), no como None. Jinja no filtra NaN en {% if %} (es "truthy") y lo
    # imprime literal como el texto "nan" en {{ }} — a diferencia de Streamlit, aquí
    # no truena, pero se ve el texto roto en pantalla y en los exports a Excel. Se
    # reemplaza por "" solo en columnas de texto (dtype object); las numéricas quedan
    # intactas para no romper cálculos que esperan float/NaN.
    for col in df.select_dtypes(include=["object", "str"]).columns:
        df[col] = df[col].apply(lambda v: "" if isinstance(v, float) and v != v else v)
    return df


def exportar_excel(df, nombre_hoja="Datos"):
    """Convierte un DataFrame a bytes de Excel."""
    buf = BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as writer:
        df.to_excel(writer, index=False, sheet_name=nombre_hoja)
        ws = writer.sheets[nombre_hoja]
        for col in ws.columns:
            max_len = max((len(str(c.value)) if c.value else 0) for c in col)
            ws.column_dimensions[col[0].column_letter].width = min(max_len + 4, 50)
    return buf.getvalue()


def ejecutar_comando(query, params=()):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    try:
        cursor.execute(query, params)
        conn.commit()
        return True
    except sqlite3.Error as e:
        logging.error(f"Error DB ejecutar_comando: {e}")
        return False
    finally:
        conn.close()


_TABLAS_RECLAMABLES = {"asistente_ia_cotizaciones_pendientes", "asistente_ia_clientes_pendientes"}


def reclamar_estado(tabla: str, id_row: int, desde: str, hacia: str) -> bool:
    """UPDATE atómico con guarda de estado (`WHERE estado=desde`) — usado para que un doble
    clic en un botón de confirmación (o dos requests casi simultáneos) no ejecute la misma
    acción dos veces. Retorna True solo si esta llamada fue la que efectivamente cambió el
    estado (exactamente 1 fila afectada); False si ya lo había tomado otra petición."""
    if tabla not in _TABLAS_RECLAMABLES:
        raise ValueError(f"Tabla no permitida para reclamar_estado: {tabla}")
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    try:
        cursor.execute(
            f"UPDATE {tabla} SET estado=? WHERE id=? AND estado=?",
            (hacia, id_row, desde)
        )
        conn.commit()
        return cursor.rowcount == 1
    except sqlite3.Error as e:
        logging.error(f"Error DB reclamar_estado: {e}")
        return False
    finally:
        conn.close()


def ejecutar_insert(query, params=()):
    """INSERT que además del éxito devuelve el lastrowid (útil para encadenar operaciones
    que necesitan el id recién creado, ej. flujo_caja -> ajuste_costo_* del mismo movimiento).
    Devuelve None si falla."""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    try:
        cursor.execute(query, params)
        conn.commit()
        return cursor.lastrowid
    except sqlite3.Error as e:
        conn.rollback()
        logging.error(f"Error en ejecutar_insert: {e}")
        return None
    finally:
        conn.close()


def ejecutar_transaccion(operaciones):
    """Ejecuta una lista de (query, params) como una sola transacción atómica."""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    try:
        for query, params in operaciones:
            cursor.execute(query, params)
        conn.commit()
        return True
    except sqlite3.Error as e:
        conn.rollback()
        logging.error(f"Error en transacción: {e}")
        return False
    finally:
        conn.close()


def registrar_cambio(id_reserva, accion, detalle="", usuario="sistema"):
    ejecutar_comando(
        "INSERT INTO bitacora_cambios (id_reserva, usuario, fecha, accion, detalle) VALUES (?, ?, ?, ?, ?)",
        (id_reserva, usuario, now_local().strftime("%Y-%m-%d %H:%M"), accion, detalle)
    )


def distribuir_equitativo(total, n):
    """Reparte `total` en `n` partes de 2 decimales cuya suma da exactamente `total`
    (la primera parte absorbe el residuo de redondeo). Usar siempre que se divida un
    monto en dinero entre varias filas (parcialidades, etc.) para evitar guardar
    valores con residuo binario o que la suma no cuadre con el total por el centavo
    perdido al dividir sin corrección."""
    total = round(float(total), 2)
    n = max(1, int(n))
    base = round(total / n, 2)
    residuo = round(total - base * n, 2)
    return [round(base + (residuo if i == 0 else 0.0), 2) for i in range(n)]


_EXCEPCIONES_TITLE_CASE = {
    "VivaAerobus": "VivaAerobus",
    "Reserva Directa (sin mayorista)": "Reserva Directa (sin mayorista)",
}

# Alias conocidos (con o sin espacios, mal escritos o abreviados) que deben
# resolver siempre al mismo nombre canónico de marca/proveedor, aunque el texto
# escrito no comparta longitud/palabras con el canónico (ej. "yamevi" -> "Yamevi Travel").
# La comparación es insensible a mayúsculas y espacios.
_ALIAS_CONOCIDOS = {
    "yamevi": "Yamevi Travel",
    "yamevitravel": "Yamevi Travel",
    "yavemitravel": "Yamevi Travel",
    "yamevitravels": "Yamevi Travel",
    "azavache": "Azabache",
    "riveramaya": "Riviera Maya",
    "cdmx": "Ciudad de México",
    "francias": "Francia",
    "ny": "Nueva York",
    "la": "Los Ángeles",
}

# Palabras frecuentes en nombres de hoteles/aerolíneas/proveedores cuya ortografía
# correcta (acentos, mayúsculas internas) Title Case no produce por sí solo. Se
# aplican palabra por palabra, sin distinguir mayúsculas/minúsculas de entrada.
_CORRECCIONES_PALABRAS = {
    # Marcas de hotel
    "barcelo": "Barceló",
    "melia": "Meliá",
    "riu": "RIU",
    # Destinos / geografía
    "cancun": "Cancún",
    "cozumel": "Cozumel",
    "merida": "Mérida",
    "mazatlan": "Mazatlán",
    "leon": "León",
    "riviera": "Riviera",
    "nayarit": "Nayarit",
    "queretaro": "Querétaro",
    "cabo": "Cabo",
    "vallarta": "Vallarta",
    "yucatan": "Yucatán",
    "michoacan": "Michoacán",
    "atencion": "Atención",
    "tulum": "Tulum",
    "holbox": "Holbox",
    "bacalar": "Bacalar",
    "culiacan": "Culiacán",
    "juarez": "Juárez",
    "coba": "Cobá",
    "panama": "Panamá",
    "bogota": "Bogotá",
    "angeles": "Ángeles",
    "paris": "París",
    # Aerolíneas
    "aeromexico": "Aeroméxico",
    "avianca": "Avianca",
    "volaris": "Volaris",
    "interjet": "Interjet",
    "copa": "Copa",
    "lufthansa": "Lufthansa",
    "iberia": "Iberia",
    "jetblue": "JetBlue",
    "westjet": "WestJet",
    "klm": "KLM",
    "latam": "LATAM",
    # Proveedores de traslados/tours
    "movil": "Móvil",
    "pvr": "PVR",
}


def normalizar_catalogo(nombre):
    """Estandariza un nombre de hotel/aerolínea/mayorista/proveedor: quita espacios
    sobrantes, aplica Title Case y corrige acentos de palabras frecuentes (ver
    _CORRECCIONES_PALABRAS), salvo los alias conocidos en _ALIAS_CONOCIDOS (ej.
    "yamevi" -> "Yamevi Travel") y las marcas en _EXCEPCIONES_TITLE_CASE que ya
    vienen con su capitalización propia (ej. VivaAerobus)."""
    nombre = "" if pd.isna(nombre) else str(nombre)
    nombre = " ".join(nombre.split())
    if not nombre:
        return ""
    _sin_espacios = nombre.lower().replace(" ", "")
    if _sin_espacios in _ALIAS_CONOCIDOS:
        return _ALIAS_CONOCIDOS[_sin_espacios]
    for marca in _EXCEPCIONES_TITLE_CASE.values():
        if _sin_espacios == marca.lower().replace(" ", ""):
            return marca
    palabras = nombre.title().split(" ")
    palabras = [_CORRECCIONES_PALABRAS.get(p.lower(), p) for p in palabras]
    return " ".join(palabras)


def get_catalogo_destinos():
    """Lista de nombres de destinos ya registrados, para autocompletar."""
    conn = sqlite3.connect(DB_PATH)
    filas = conn.execute("SELECT nombre FROM destinos_catalogo ORDER BY nombre").fetchall()
    conn.close()
    return [f[0] for f in filas]


def upsert_catalogo_destino(nombre):
    """Normaliza y agrega `nombre` al catálogo de destinos si no existe. Devuelve el nombre normalizado."""
    nombre = normalizar_catalogo(nombre)
    if not nombre:
        return nombre
    conn = sqlite3.connect(DB_PATH)
    conn.execute("INSERT OR IGNORE INTO destinos_catalogo (nombre) VALUES (?)", (nombre,))
    conn.commit()
    conn.close()
    return nombre


def get_catalogo_hoteles():
    """Lista de nombres de hoteles ya registrados, para autocompletar."""
    conn = sqlite3.connect(DB_PATH)
    filas = conn.execute("SELECT nombre FROM hoteles_catalogo ORDER BY nombre").fetchall()
    conn.close()
    return [f[0] for f in filas]


def get_catalogo_aerolineas():
    """Lista de nombres de aerolíneas ya registradas, para autocompletar."""
    conn = sqlite3.connect(DB_PATH)
    filas = conn.execute("SELECT nombre FROM aerolineas_catalogo ORDER BY nombre").fetchall()
    conn.close()
    return [f[0] for f in filas]


def upsert_catalogo_hotel(nombre):
    """Normaliza y agrega `nombre` al catálogo de hoteles si no existe. Devuelve el nombre normalizado."""
    nombre = normalizar_catalogo(nombre)
    if not nombre:
        return nombre
    conn = sqlite3.connect(DB_PATH)
    conn.execute("INSERT OR IGNORE INTO hoteles_catalogo (nombre) VALUES (?)", (nombre,))
    conn.commit()
    conn.close()
    return nombre


def upsert_catalogo_aerolinea(nombre):
    """Normaliza y agrega `nombre` al catálogo de aerolíneas si no existe. Devuelve el nombre normalizado."""
    nombre = normalizar_catalogo(nombre)
    if not nombre:
        return nombre
    conn = sqlite3.connect(DB_PATH)
    conn.execute("INSERT OR IGNORE INTO aerolineas_catalogo (nombre) VALUES (?)", (nombre,))
    conn.commit()
    conn.close()
    return nombre


def get_catalogo_mayoristas():
    """Lista de mayoristas ya registrados, para autocompletar. 'Reserva Directa (sin mayorista)' siempre primero."""
    conn = sqlite3.connect(DB_PATH)
    filas = conn.execute("SELECT nombre FROM mayoristas_catalogo ORDER BY nombre").fetchall()
    conn.close()
    nombres = [f[0] for f in filas]
    fija = "Reserva Directa (sin mayorista)"
    if fija in nombres:
        nombres.remove(fija)
        nombres.insert(0, fija)
    return nombres


def upsert_catalogo_mayorista(nombre):
    """Normaliza y agrega `nombre` al catálogo de mayoristas si no existe. Devuelve el nombre normalizado."""
    nombre = normalizar_catalogo(nombre)
    if not nombre:
        return nombre
    conn = sqlite3.connect(DB_PATH)
    conn.execute("INSERT OR IGNORE INTO mayoristas_catalogo (nombre) VALUES (?)", (nombre,))
    conn.commit()
    conn.close()
    return nombre


def get_catalogo_proveedores_traslados():
    """Lista de proveedores de traslados ya registrados, para autocompletar."""
    conn = sqlite3.connect(DB_PATH)
    filas = conn.execute("SELECT nombre FROM proveedores_traslados_catalogo ORDER BY nombre").fetchall()
    conn.close()
    return [f[0] for f in filas]


def upsert_catalogo_proveedor_traslados(nombre):
    """Normaliza y agrega `nombre` al catálogo de proveedores de traslados si no existe. Devuelve el nombre normalizado."""
    nombre = normalizar_catalogo(nombre)
    if not nombre:
        return nombre
    conn = sqlite3.connect(DB_PATH)
    conn.execute("INSERT OR IGNORE INTO proveedores_traslados_catalogo (nombre) VALUES (?)", (nombre,))
    conn.commit()
    conn.close()
    return nombre


def get_catalogo_proveedores_tours():
    """Lista de proveedores de tours ya registrados, para autocompletar."""
    conn = sqlite3.connect(DB_PATH)
    filas = conn.execute("SELECT nombre FROM proveedores_tours_catalogo ORDER BY nombre").fetchall()
    conn.close()
    return [f[0] for f in filas]


def upsert_catalogo_proveedor_tours(nombre):
    """Normaliza y agrega `nombre` al catálogo de proveedores de tours si no existe. Devuelve el nombre normalizado."""
    nombre = normalizar_catalogo(nombre)
    if not nombre:
        return nombre
    conn = sqlite3.connect(DB_PATH)
    conn.execute("INSERT OR IGNORE INTO proveedores_tours_catalogo (nombre) VALUES (?)", (nombre,))
    conn.commit()
    conn.close()
    return nombre


def get_catalogo_proveedores_adicionales():
    """Lista de proveedores de adicionales ya registrados, para autocompletar."""
    conn = sqlite3.connect(DB_PATH)
    filas = conn.execute("SELECT nombre FROM proveedores_adicionales_catalogo ORDER BY nombre").fetchall()
    conn.close()
    return [f[0] for f in filas]


def upsert_catalogo_proveedor_adicionales(nombre):
    """Normaliza y agrega `nombre` al catálogo de proveedores de adicionales si no existe. Devuelve el nombre normalizado."""
    nombre = normalizar_catalogo(nombre)
    if not nombre:
        return nombre
    conn = sqlite3.connect(DB_PATH)
    conn.execute("INSERT OR IGNORE INTO proveedores_adicionales_catalogo (nombre) VALUES (?)", (nombre,))
    conn.commit()
    conn.close()
    return nombre


def get_catalogo_equipaje():
    """Lista de combinaciones de equipaje ya registradas, para autocompletar."""
    conn = sqlite3.connect(DB_PATH)
    filas = conn.execute("SELECT nombre FROM equipaje_catalogo ORDER BY nombre").fetchall()
    conn.close()
    return [f[0] for f in filas]


def upsert_catalogo_equipaje(texto):
    """Agrega `texto` al catálogo de equipaje si no existe. A diferencia de los demás
    catálogos, NO aplica Title Case (mangling con 'kg', '+', etc.) — solo limpia espacios.
    Devuelve el texto tal cual (limpio)."""
    texto = "" if pd.isna(texto) else str(texto)
    texto = " ".join(texto.split())
    if not texto:
        return texto
    conn = sqlite3.connect(DB_PATH)
    conn.execute("INSERT OR IGNORE INTO equipaje_catalogo (nombre) VALUES (?)", (texto,))
    conn.commit()
    conn.close()
    return texto


def obtener_token_portal(id_reserva):
    """Devuelve el token del portal del cliente para esta reserva — lo genera y lo
    guarda la primera vez que se pide (ej. al generar un PDF), lo reutiliza después."""
    import secrets as _secrets
    df = obtener_datos("SELECT token_portal FROM reservas WHERE id_reserva=?", (id_reserva,))
    if df.empty:
        return None
    token = df.iloc[0]["token_portal"]
    if token and str(token).strip() and str(token) != "nan":
        return token
    token = _secrets.token_urlsafe(24)
    ejecutar_comando("UPDATE reservas SET token_portal=? WHERE id_reserva=?", (token, id_reserva))
    return token


def obtener_reserva_por_token_portal(token):
    """Busca la reserva asociada a un token del portal del cliente. Devuelve un dict o None."""
    if not token:
        return None
    df = obtener_datos(
        "SELECT r.*, c.nombre as nombre_cliente FROM reservas r "
        "JOIN clientes c ON r.id_cliente=c.id_cliente WHERE r.token_portal=?",
        (token,)
    )
    if df.empty:
        return None
    return df.iloc[0].to_dict()


def calcular_saldo_real(id_reserva):
    """Calcula saldo REAL desde flujo_caja (fuente de verdad). Usa obtener_datos para aprovechar el caché."""
    df_venta = obtener_datos("SELECT venta_total FROM reservas WHERE id_reserva = ?", (id_reserva,))
    venta_total = float(df_venta['venta_total'].iloc[0]) if not df_venta.empty else 0.0

    df_cobrado = obtener_datos(
        "SELECT COALESCE(SUM(monto), 0.0) as cobrado FROM flujo_caja "
        "WHERE id_reserva = ? AND tipo_movimiento = 'INGRESO' AND estado = 'ACTIVO'",
        (id_reserva,)
    )
    cobrado_activos = float(df_cobrado['cobrado'].iloc[0]) if not df_cobrado.empty else 0.0

    return {
        'venta_total': venta_total,
        'cobrado_activos': cobrado_activos,
        'saldo_pendiente': venta_total - cobrado_activos
    }


def _calcular_plan_fechas(tipo_plan: str, dia_mensual: int, fecha_desde, fecha_limite) -> list:
    """Devuelve lista de fechas para el plan de pagos de la cotización."""
    from datetime import timedelta as _td
    import calendar as _cal
    fechas = []
    curr = fecha_desde + _td(days=1)
    while curr <= fecha_limite:
        if tipo_plan == "Semanal (Lunes)" and curr.weekday() == 0:
            fechas.append(curr)
        elif tipo_plan == "Quincenal (15 y 30)":
            ld = _cal.monthrange(curr.year, curr.month)[1]
            if curr.day == 15 or curr.day == ld:
                fechas.append(curr)
        elif tipo_plan == "Mensual" and curr.day == dia_mensual:
            fechas.append(curr)
        curr += _td(days=1)
    if not fechas:
        fechas.append(fecha_limite)
    return fechas


def _generar_id_cliente() -> str:
    df = obtener_datos("SELECT MAX(CAST(SUBSTR(id_cliente,4) AS INTEGER)) as n FROM clientes WHERE id_cliente LIKE 'AC-%'")
    n = int(df.iloc[0]["n"]) if not df.empty and df.iloc[0]["n"] else 0
    return f"AC-{n+1:03d}"


def _formatear_tel(raw: str) -> str:
    digits = "".join(c for c in raw if c.isdigit())
    if len(digits) == 10:
        return f"{digits[:3]}-{digits[3:6]}-{digits[6:]}"
    return raw.strip()


def buscar_duplicados_cliente(nombre: str, telefono: str, email: str) -> list:
    """Devuelve una lista de avisos legibles (str) de posibles duplicados por teléfono,
    email o nombre similar — mismo criterio que el formulario humano de Nuevo Cliente."""
    avisos = []
    tel = _formatear_tel(telefono) if telefono else ""
    ya_ids = set()
    if tel:
        df_dt = obtener_datos("SELECT id_cliente, nombre FROM clientes WHERE telefono=?", (tel,))
        for _, r in df_dt.iterrows():
            avisos.append(f"Teléfono ya registrado: {r['nombre']} ({r['id_cliente']})")
            ya_ids.add(r["id_cliente"])
    if email:
        df_dm = obtener_datos("SELECT id_cliente, nombre FROM clientes WHERE email=? AND email!=''", (email,))
        for _, r in df_dm.iterrows():
            avisos.append(f"Email ya registrado: {r['nombre']} ({r['id_cliente']})")
    palabras = [p for p in (nombre or "").split() if len(p) > 3]
    if palabras:
        q_like = " OR ".join(["nombre LIKE ?" for _ in palabras])
        df_dn = obtener_datos(f"SELECT id_cliente, nombre FROM clientes WHERE {q_like}",
                               tuple(f"%{p}%" for p in palabras))
        for _, r in df_dn.iterrows():
            if r["id_cliente"] not in ya_ids:
                avisos.append(f"Nombre similar: {r['nombre']} ({r['id_cliente']})")
    return avisos


def crear_cliente(datos: dict, usuario: str):
    """Crea un cliente nuevo. Retorna (id_cliente, error) — error es None si todo salió bien.
    No revisa duplicados aquí (eso es responsabilidad de quien llama, ej. el formulario humano
    o `asistente_ia_tools.preparar_cliente_nuevo`, que debe mostrarlos ANTES de llegar a esta función)."""
    nombre = (datos.get("nombre") or "").strip()
    if not nombre:
        return None, "El nombre es obligatorio."
    tel_raw = (datos.get("telefono") or "").strip()
    if tel_raw and len("".join(c for c in tel_raw if c.isdigit())) != 10:
        return None, "El teléfono debe tener exactamente 10 dígitos."
    tel = _formatear_tel(tel_raw) if tel_raw else None
    email = (datos.get("email") or "").strip().lower() or None
    fnac = (datos.get("fecha_nacimiento") or "").strip() or None
    cod_pais = datos.get("codigo_pais") or "+52"

    nuevo_id = _generar_id_cliente()
    ejecutar_comando(
        "INSERT INTO clientes (id_cliente, nombre, telefono, email, fecha_nacimiento, codigo_pais) VALUES (?,?,?,?,?,?)",
        (nuevo_id, nombre, tel, email, fnac, cod_pais)
    )
    return nuevo_id, None


def editar_cliente(id_cliente: str, datos: dict, usuario: str):
    """Actualiza los datos de un cliente ya existente. `datos` debe traer el registro
    COMPLETO (nombre, telefono, email, fecha_nacimiento, codigo_pais) — quien llama es
    responsable de fusionar los cambios solicitados sobre los datos actuales, para no
    borrar accidentalmente campos que el usuario no pidió cambiar. Retorna (True, None) o
    (False, error)."""
    if obtener_datos("SELECT 1 FROM clientes WHERE id_cliente=?", (id_cliente,)).empty:
        return False, "El cliente no existe."
    nombre = (datos.get("nombre") or "").strip()
    if not nombre:
        return False, "El nombre es obligatorio."
    tel_raw = (datos.get("telefono") or "").strip()
    if tel_raw and len("".join(c for c in tel_raw if c.isdigit())) != 10:
        return False, "El teléfono debe tener exactamente 10 dígitos."
    tel = _formatear_tel(tel_raw) if tel_raw else None
    email = (datos.get("email") or "").strip().lower() or None
    fnac = (datos.get("fecha_nacimiento") or "").strip() or None
    cod_pais = datos.get("codigo_pais") or "+52"

    ejecutar_comando(
        "UPDATE clientes SET nombre=?, telefono=?, email=?, fecha_nacimiento=?, codigo_pais=? WHERE id_cliente=?",
        (nombre, tel, email, fnac, cod_pais, id_cliente)
    )
    return True, None


def crear_cotizacion(datos: dict, usuario: str):
    """Crea una cotización a partir de un dict de campos (mismas claves que el form de
    POST /cotizaciones/nueva). Retorna (new_id, error) — error es None si todo salió bien,
    o un string legible si falta el destino o la venta total es <= 0 (no se inserta nada)."""
    def _f(k): return float(datos.get(k) or 0)
    def _i(k): return int(datos.get(k) or 0)
    def _s(k): return (datos.get(k) or "").strip()

    id_cliente = _s("id_cliente")
    if not id_cliente or obtener_datos("SELECT 1 FROM clientes WHERE id_cliente=?", (id_cliente,)).empty:
        return None, "El cliente es obligatorio y debe existir en el sistema."

    destino = upsert_catalogo_destino(_s("destino"))
    if not destino:
        return None, "El destino es obligatorio."

    venta = _f("cobro_vuelos") + _f("cobro_tua") + _f("cobro_hotel") + _f("cobro_traslados") + _f("cobro_tours") + _f("cobro_adicionales")
    if venta <= 0:
        return None, "El total de la cotización debe ser mayor a cero."

    # Mismas validaciones de fechas/vuelo/hotel que Streamlit (app.py:3565-3584) —
    # sin esto se podían guardar cotizaciones con regreso antes que salida, fechas
    # límite inconsistentes, o "incluye vuelo" sin datos de vuelo.
    f_salida  = _s("fecha_salida")
    f_regreso = _s("fecha_regreso")
    if f_salida and f_regreso and f_regreso < f_salida:
        return None, "La fecha de regreso no puede ser anterior a la fecha de salida."
    f_limite_prov = _s("fecha_limite_proveedor")
    if f_limite_prov and f_salida and f_limite_prov > f_salida:
        return None, "La fecha límite de pago al proveedor no puede ser posterior a la fecha de salida del viaje."
    f_limite_pago = _s("fecha_limite_pago")
    if f_limite_pago and f_salida and f_limite_pago > f_salida:
        return None, "La fecha límite de pago del cliente no puede ser posterior a la fecha de salida del viaje."
    if f_limite_pago and f_limite_prov and f_limite_pago > f_limite_prov:
        return None, "La fecha límite de pago del cliente no puede ser posterior a la del proveedor."
    if datos.get("incluye_vuelo") and not (_s("fecha_vuelo_ida") and _s("hora_vuelo_ida") and _s("fecha_vuelo_vuelta") and _s("hora_vuelo_vuelta")):
        return None, "Marcaste que incluye vuelo — debes capturar fecha y hora de ida y vuelta."
    if _f("cobro_hotel") > 0 or _f("costo_hotel") > 0:
        try: _n_hab_val = int(datos.get("hab_n") or 0)
        except Exception: _n_hab_val = 0
        if not any((datos.get(f"hab_tipo_{i}") or "").strip() for i in range(1, _n_hab_val + 1)):
            return None, "Registraste venta/costo de hotel — debes registrar al menos una habitación con su tipo."

    costo = _f("costo_vuelos") + _f("costo_tua") + _f("costo_hotel") + _f("costo_traslados") + _f("costo_tours") + _f("costo_adicionales")
    hoy = now_local().date()
    vigencia = _i("dias_vigencia") or 7
    fecha_venc = str(hoy + timedelta(days=vigencia))

    v_nombre_hotel = upsert_catalogo_hotel(_s("nombre_hotel"))
    v_aerolinea = upsert_catalogo_aerolinea(_s("aerolinea"))
    v_mayorista = upsert_catalogo_mayorista(_s("mayorista"))
    v_prov_traslados = upsert_catalogo_proveedor_traslados(_s("proveedor_traslados"))
    v_prov_tours = upsert_catalogo_proveedor_tours(_s("proveedor_tours"))
    v_equipaje = upsert_catalogo_equipaje(_s("detalle_equipaje"))
    v_hotel_op2 = upsert_catalogo_hotel(_s("hotel_op2_nombre"))
    v_hotel_op3 = upsert_catalogo_hotel(_s("hotel_op3_nombre"))

    ejecutar_comando("""
        INSERT INTO cotizaciones
            (id_cliente, destino, origen, fecha_salida, fecha_regreso, moneda,
             num_adultos, num_menores,
             incluye_vuelo, incluye_tua, incluye_hotel, incluye_traslado,
             cobro_vuelos, cobro_tua, cobro_hotel, cobro_traslados, cobro_tours, cobro_adicionales,
             especificar_adicionales, venta_total,
             nombre_hotel, aerolinea, mayorista, proveedor_traslados, proveedor_tours,
             anticipo_requerido, tipo_plan, dia_mensual,
             fecha_limite_pago, fecha_limite_proveedor, notas, dias_vigencia,
             estado, fecha_cotizacion, fecha_vencimiento, usuario_creador,
             costo_vuelos, costo_tua, costo_hotel, costo_traslados, costo_tours, costo_adicionales,
             costo_total, utilidad_proyectada,
             hotel_op2_nombre, hotel_op2_cobro, hotel_op2_costo,
             hotel_op3_nombre, hotel_op3_cobro, hotel_op3_costo, hotel_opcion_elegida,
             fecha_vuelo_ida, hora_vuelo_ida, fecha_vuelo_vuelta, hora_vuelo_vuelta, detalle_equipaje)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
    """, (
        _s("id_cliente"), destino, _s("origen") or "Monterrey",
        _s("fecha_salida"), _s("fecha_regreso"), datos.get("moneda") or "MXN",
        _i("num_adultos") or 1, _i("num_menores"),
        1 if datos.get("incluye_vuelo") else 0,
        1 if datos.get("incluye_tua") else 0,
        1,  # incluye_hotel siempre True (sin checkbox)
        1 if datos.get("incluye_traslado") else 0,
        _f("cobro_vuelos"), _f("cobro_tua"), _f("cobro_hotel"), _f("cobro_traslados"),
        _f("cobro_tours"), _f("cobro_adicionales"),
        _s("especificar_adicionales") or None, venta,
        v_nombre_hotel or None, v_aerolinea or None, v_mayorista or None,
        v_prov_traslados or None, v_prov_tours or None,
        _f("anticipo_requerido"), datos.get("tipo_plan") or "Sin Plan (Libre)", _i("dia_mensual") or 5,
        _s("fecha_limite_pago") or None, _s("fecha_limite_proveedor") or None,
        _s("notas") or None, vigencia,
        "PENDIENTE", str(hoy), fecha_venc, usuario,
        _f("costo_vuelos"), _f("costo_tua"), _f("costo_hotel"), _f("costo_traslados"),
        _f("costo_tours"), _f("costo_adicionales"), costo, venta - costo,
        v_hotel_op2 or '', _f("hotel_op2_cobro"), _f("hotel_op2_costo"),
        v_hotel_op3 or '', _f("hotel_op3_cobro"), _f("hotel_op3_costo"), 1,
        _s("fecha_vuelo_ida") or '', _s("hora_vuelo_ida") or '',
        _s("fecha_vuelo_vuelta") or '', _s("hora_vuelo_vuelta") or '',
        v_equipaje or None,
    ))
    df_new = obtener_datos("SELECT MAX(id_cotizacion) as n FROM cotizaciones")
    new_id = int(df_new.iloc[0]["n"])

    try:
        n_hab = int(datos.get("hab_n") or 0)
    except Exception:
        n_hab = 0
    for i in range(1, n_hab + 1):
        h_tipo = (datos.get(f"hab_tipo_{i}") or "").strip()
        if not h_tipo:
            continue
        try: h_pers = int(datos.get(f"hab_personas_{i}") or 1)
        except Exception: h_pers = 1
        ejecutar_comando(
            "INSERT INTO habitaciones_cotizacion (id_cotizacion, tipo_habitacion, num_personas, hora_checkin, descripcion) VALUES (?,?,?,?,?)",
            (new_id, h_tipo, h_pers, (datos.get(f"hab_checkin_{i}") or "15:00").strip() or "15:00", (datos.get(f"hab_descripcion_{i}") or "").strip() or None)
        )

    return new_id, None


def convertir_cotizacion(id_cotizacion: int, hotel_opcion_elegida, usuario: str):
    """Convierte una cotización ya guardada en un itinerario (reserva). Retorna (id_reserva, error)
    — error es None si todo salió bien, o un string legible si la cotización no existe."""
    df_cot = obtener_datos("SELECT * FROM cotizaciones WHERE id_cotizacion=?", (id_cotizacion,))
    if df_cot.empty:
        return None, "La cotización no existe."
    cr = df_cot.iloc[0].to_dict()

    from datetime import datetime as _dt
    hoy = str(now_local().date())
    flim_cli  = cr.get("fecha_limite_pago")  or cr["fecha_regreso"]
    flim_prov = cr.get("fecha_limite_proveedor") or cr["fecha_regreso"]

    _opcion_num = int(hotel_opcion_elegida or 1)
    if _opcion_num == 2 and float(cr.get("hotel_op2_cobro") or 0) > 0:
        _cobro_hotel = float(cr["hotel_op2_cobro"] or 0)
        _costo_hotel = float(cr["hotel_op2_costo"] or 0)
        _nombre_hotel = cr.get("hotel_op2_nombre") or cr.get("nombre_hotel") or None
    elif _opcion_num == 3 and float(cr.get("hotel_op3_cobro") or 0) > 0:
        _cobro_hotel = float(cr["hotel_op3_cobro"] or 0)
        _costo_hotel = float(cr["hotel_op3_costo"] or 0)
        _nombre_hotel = cr.get("hotel_op3_nombre") or cr.get("nombre_hotel") or None
    else:
        _cobro_hotel = float(cr["cobro_hotel"] or 0)
        _costo_hotel = float(cr["costo_hotel"] or 0)
        _nombre_hotel = cr.get("nombre_hotel") or None

    _base_cobro = float(cr["cobro_vuelos"] or 0) + float(cr["cobro_tua"] or 0) + float(cr["cobro_traslados"] or 0) + float(cr["cobro_tours"] or 0) + float(cr["cobro_adicionales"] or 0)
    _base_costo = float(cr["costo_vuelos"] or 0) + float(cr["costo_tua"] or 0) + float(cr["costo_traslados"] or 0) + float(cr["costo_tours"] or 0) + float(cr["costo_adicionales"] or 0)
    venta   = _base_cobro + _cobro_hotel
    costo   = _base_costo + _costo_hotel
    utilidad = venta - costo

    ejecutar_comando("""
        INSERT INTO reservas
            (id_cliente, destino, origen, fecha_salida, fecha_regreso,
             fecha_limite_liquidacion, fecha_limite_proveedor, moneda,
             cobro_vuelos, cobro_tua, cobro_hotel, cobro_traslados,
             cobro_tours, cobro_adicionales, especificar_adicionales, venta_total,
             costo_vuelos, costo_tua, costo_hotel, costo_traslados,
             costo_tours, costo_adicionales, costo_comisiones,
             costo_total, utilidad_proyectada,
             mayorista, aerolinea, nombre_hotel, proveedor_traslados, proveedor_tours,
             fecha_vuelo_ida, hora_vuelo_ida, fecha_vuelo_vuelta, hora_vuelo_vuelta, detalle_equipaje,
             notas_abiertas, fecha_creacion, usuario_creador, estado)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,0,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
    """, (
        cr["id_cliente"], cr["destino"], cr["origen"] or "Monterrey",
        cr["fecha_salida"], cr["fecha_regreso"],
        flim_cli, flim_prov, cr["moneda"],
        float(cr["cobro_vuelos"] or 0), float(cr["cobro_tua"] or 0),
        _cobro_hotel, float(cr["cobro_traslados"] or 0),
        float(cr["cobro_tours"] or 0), float(cr["cobro_adicionales"] or 0),
        cr.get("especificar_adicionales") or None, venta,
        float(cr["costo_vuelos"] or 0), float(cr["costo_tua"] or 0),
        _costo_hotel, float(cr["costo_traslados"] or 0),
        float(cr["costo_tours"] or 0), float(cr["costo_adicionales"] or 0),
        costo, utilidad,
        cr.get("mayorista") or None, cr.get("aerolinea") or None,
        _nombre_hotel, cr.get("proveedor_traslados") or None,
        cr.get("proveedor_tours") or None,
        cr.get("fecha_vuelo_ida") or cr["fecha_salida"],
        cr.get("hora_vuelo_ida") or None,
        cr.get("fecha_vuelo_vuelta") or cr["fecha_regreso"],
        cr.get("hora_vuelo_vuelta") or None,
        cr.get("detalle_equipaje") or None,
        f"Convertido desde COT-{str(cr.get('fecha_cotizacion',''))[:4]}-{id_cotizacion:04d} (Hotel Opción {_opcion_num}). {cr.get('notas') or ''}",
        hoy, usuario, "ACTIVO",
    ))

    df_new = obtener_datos("SELECT MAX(id_reserva) as n FROM reservas")
    id_reserva = int(df_new.iloc[0]["n"])

    df_hab_cot = obtener_datos(
        "SELECT tipo_habitacion, num_personas, hora_checkin, descripcion FROM habitaciones_cotizacion WHERE id_cotizacion=?",
        (id_cotizacion,)
    )
    for h in df_hab_cot.to_dict("records"):
        ejecutar_comando(
            "INSERT INTO habitaciones_reserva (id_reserva, tipo_habitacion, num_personas, hora_checkin, descripcion) VALUES (?,?,?,?,?)",
            (id_reserva, h["tipo_habitacion"], h["num_personas"], h["hora_checkin"] or "15:00", h.get("descripcion"))
        )

    df_cli = obtener_datos("SELECT nombre, fecha_nacimiento FROM clientes WHERE id_cliente=?", (cr["id_cliente"],))
    if not df_cli.empty:
        ejecutar_comando(
            "INSERT INTO pasajeros_reserva (id_reserva, nombre, fecha_nacimiento, parentesco) VALUES (?,?,?,?)",
            (id_reserva, df_cli.iloc[0]["nombre"], df_cli.iloc[0]["fecha_nacimiento"] or "", "Titular")
        )

    if cr["tipo_plan"] != "Sin Plan (Libre)" and cr.get("fecha_limite_pago"):
        try:
            anticipo = float(cr["anticipo_requerido"] or 0)
            saldo = venta - anticipo
            f_limite = _dt.strptime(str(cr["fecha_limite_pago"])[:10], "%Y-%m-%d").date()
            hoy_date = now_local().date()
            fechas = _calcular_plan_fechas(cr["tipo_plan"], int(cr["dia_mensual"] or 5), hoy_date, f_limite)
            if fechas and saldo > 0:
                montos_conv = distribuir_equitativo(saldo, len(fechas))
                if anticipo > 0:
                    ejecutar_comando(
                        "INSERT INTO plan_pagos (id_reserva, numero_pago, monto_esperado, fecha_programada) VALUES (?,?,?,?)",
                        (id_reserva, 1, anticipo, str(hoy_date))
                    )
                for idx, f in enumerate(fechas):
                    ejecutar_comando(
                        "INSERT INTO plan_pagos (id_reserva, numero_pago, monto_esperado, fecha_programada) VALUES (?,?,?,?)",
                        (id_reserva, (2 if anticipo > 0 else 1) + idx, montos_conv[idx], str(f))
                    )
        except Exception:
            pass

    registrar_cambio(id_reserva, "CREACIÓN", f"Convertida desde cotización COT-{str(cr['fecha_cotizacion'])[:4]}-{id_cotizacion:04d}", usuario=usuario)

    ejecutar_comando(
        "UPDATE cotizaciones SET estado='ACEPTADA', convertida_a_reserva=?, hotel_opcion_elegida=? WHERE id_cotizacion=?",
        (id_reserva, _opcion_num, id_cotizacion)
    )

    return id_reserva, None


def sincronizar_cobrado_cliente(id_reserva):
    """Recalcula cobrado_cliente desde flujo_caja y lo persiste. Fuente de verdad única.
    Resta los reembolsos a cliente (categoría 'Reembolso a Cliente', un EGRESO) para que
    cobrado_cliente refleje lo que el cliente realmente conserva pagado — nunca se ajusta
    con aritmética manual fuera de esta función, así nunca se pierde al recalcular."""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    try:
        cursor.execute(
            "SELECT COALESCE(SUM(monto), 0.0) FROM flujo_caja "
            "WHERE id_reserva = ? AND tipo_movimiento = 'INGRESO' "
            "AND estado = 'ACTIVO' AND concepto NOT LIKE '%INVERSIÓN%'",
            (id_reserva,)
        )
        cobrado = float(cursor.fetchone()[0] or 0.0)
        cursor.execute(
            "SELECT COALESCE(SUM(monto), 0.0) FROM flujo_caja "
            "WHERE id_reserva = ? AND tipo_movimiento = 'EGRESO' "
            "AND categoria = 'Reembolso a Cliente' AND estado = 'ACTIVO'",
            (id_reserva,)
        )
        reembolsado = float(cursor.fetchone()[0] or 0.0)
        cobrado = round(cobrado - reembolsado, 2)
        cursor.execute(
            "UPDATE reservas SET cobrado_cliente = ? WHERE id_reserva = ?",
            (cobrado, id_reserva)
        )
        conn.commit()
    except Exception as e:
        conn.rollback()
        logging.error(f"Error sincronizando saldo: {e}")
    finally:
        conn.close()


def generar_siguiente_id():
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("SELECT MAX(CAST(SUBSTR(id_cliente, 4) AS INTEGER)) FROM clientes WHERE id_cliente LIKE 'AC-%'")
    max_num = cursor.fetchone()[0]
    conn.close()
    return f"AC-{(max_num or 0) + 1:03d}"


def actualizar_estado_plan_pagos(id_reserva):
    """
    Recalcula monto_pagado y estado de cada parcialidad asignando el total cobrado
    (todos los ingresos válidos excepto anticipo/inversión) en cascada, en orden de
    numero_pago, SIN modificar nunca monto_esperado (el compromiso original fijo de
    cada parcialidad). Cada parcialidad recibe hasta su propio monto_esperado; lo que
    sobra pasa a la siguiente. Estado: PAGADO si se cubrió completa, PARCIAL si recibió
    algo pero no todo, PENDIENTE si no ha recibido nada.
    """
    df_plan = obtener_datos(
        "SELECT id_pago, numero_pago, monto_esperado FROM plan_pagos WHERE id_reserva = ? ORDER BY numero_pago ASC",
        (id_reserva,)
    )

    if df_plan.empty:
        return

    df_pagos_validos = obtener_datos(
            """SELECT COALESCE(SUM(monto), 0.0) as total_pagado
               FROM flujo_caja
               WHERE id_reserva = ?
               AND tipo_movimiento = 'INGRESO'
               AND estado = 'ACTIVO'
               AND concepto NOT LIKE '%Anticipo%'
               AND concepto NOT LIKE '%INVERSIÓN%'""",
        (id_reserva,)
    )

    restante = float(df_pagos_validos['total_pagado'].iloc[0] or 0.0)

    updates = []
    for _, rp in df_plan.iterrows():
        monto_esperado = round(float(rp['monto_esperado']), 2)
        pagado = round(min(monto_esperado, max(restante, 0.0)), 2)
        restante = round(restante - pagado, 2)
        if pagado >= monto_esperado - 0.01:
            est = 'PAGADO'
        elif pagado > 0.01:
            est = 'PARCIAL'
        else:
            est = 'PENDIENTE'
        updates.append((pagado, est, int(rp['id_pago'])))

    if updates:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        try:
            cursor.executemany("UPDATE plan_pagos SET monto_pagado=?, estado=? WHERE id_pago=?", updates)
            conn.commit()
        except sqlite3.Error as e:
            conn.rollback()
            logging.error(f"Error actualizando plan de pagos: {e}")
        finally:
            conn.close()


def registrar_intento_login(usuario, exitoso=False):
    conn = sqlite3.connect(DB_PATH)
    try:
        ahora = now_local().strftime("%Y-%m-%d %H:%M:%S")
        hace_24h = (now_local() - timedelta(hours=24)).strftime("%Y-%m-%d %H:%M:%S")
        conn.execute(
            "INSERT INTO login_intentos (usuario, fecha_intento, exitoso) VALUES (?, ?, ?)",
            (usuario, ahora, 1 if exitoso else 0)
        )
        conn.execute("DELETE FROM login_intentos WHERE fecha_intento < ?", (hace_24h,))
        conn.commit()
    except sqlite3.Error as e:
        logging.error(f"Error registrando intento login: {e}")
    finally:
        conn.close()


def verificar_bloqueo_login(usuario, max_intentos=5, ventana_minutos=15):
    """Devuelve (bloqueado, segundos_restantes, intentos_recientes)."""
    df = obtener_datos(
        "SELECT fecha_intento FROM login_intentos WHERE usuario=? AND exitoso=0 ORDER BY fecha_intento DESC",
        (usuario,)
    )
    if df.empty:
        return False, 0, 0
    ahora = now_local()
    corte = ahora - timedelta(minutes=ventana_minutos)
    recientes = [
        r for _, r in df.iterrows()
        if datetime.strptime(r["fecha_intento"], "%Y-%m-%d %H:%M:%S") >= corte
    ]
    n = len(recientes)
    if n >= max_intentos:
        mas_antiguo = min(
            datetime.strptime(r["fecha_intento"], "%Y-%m-%d %H:%M:%S") for r in recientes
        )
        expira = mas_antiguo + timedelta(minutes=ventana_minutos)
        segundos = max(0, int((expira - ahora).total_seconds()))
        return True, segundos, n
    return False, 0, n


def limpiar_intentos_login(usuario):
    ejecutar_comando("DELETE FROM login_intentos WHERE usuario=? AND exitoso=0", (usuario,))
