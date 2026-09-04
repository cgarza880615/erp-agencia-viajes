from reportlab.lib.pagesizes import letter
from reportlab.pdfgen import canvas
from reportlab.lib.utils import ImageReader
from reportlab.lib.colors import HexColor
from io import BytesIO
from datetime import datetime
from database import now_local

_DIAS_ES = ["lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo"]
_MESES_ES = ["", "enero", "febrero", "marzo", "abril", "mayo", "junio", "julio",
             "agosto", "septiembre", "octubre", "noviembre", "diciembre"]


def _dibujar_qr_portal(c, x, y, size, url, texto="Consulta en línea", arriba=True):
    """Dibuja un código QR (esquina inferior izquierda en x,y) que apunta al portal del
    cliente, más un texto corto junto a él. Tanto el QR como el texto quedan como link
    tocable (c.linkURL) — así funciona igual si el cliente ve el PDF desde el mismo
    celular y no puede escanear su propia pantalla. No requiere ninguna librería nueva:
    el soporte de QR ya viene incluido en ReportLab."""
    from reportlab.graphics.barcode.qr import QrCodeWidget
    from reportlab.graphics.shapes import Drawing
    from reportlab.graphics import renderPDF
    widget = QrCodeWidget(url)
    b = widget.getBounds()
    w_qr = b[2] - b[0]
    h_qr = b[3] - b[1]
    d = Drawing(size, size, transform=[size / w_qr, 0, 0, size / h_qr, 0, 0])
    d.add(widget)
    renderPDF.draw(d, c, x, y)
    c.linkURL(url, (x, y, x + size, y + size), relative=0)

    c.saveState()
    c.setFillColor(HexColor("#0066cc"))
    c.setFont("Helvetica-Bold", 6.5)
    ty = y + size + 5 if arriba else y - 6
    c.drawCentredString(x + size / 2, ty, texto)
    tw = c.stringWidth(texto, "Helvetica-Bold", 6.5)
    c.linkURL(url, (x + size / 2 - tw / 2 - 3, ty - 1.5, x + size / 2 + tw / 2 + 3, ty + 7), relative=0)
    c.restoreState()


def _fecha_larga_es(fecha_str, hora_str=None):
    """'2026-08-08' + '16:00' -> 'Domingo 8 de agosto de 2026 a las 16:00 horas'"""
    try:
        d = datetime.strptime(str(fecha_str)[:10], "%Y-%m-%d")
    except (ValueError, TypeError):
        return str(fecha_str)
    texto = f"{_DIAS_ES[d.weekday()].capitalize()} {d.day} de {_MESES_ES[d.month]} de {d.year}"
    if hora_str and str(hora_str).strip() not in ("", "None", "nan"):
        texto += f" a las {hora_str} horas"
    return texto


def generar_recibo_pdf(id_movimiento, nombre_cliente, telefono_cliente, email_cliente, monto, moneda, fecha_pago, metodo_pago, concepto, saldo_anterior, saldo_actual, operador, id_reserva, destino, logo_path, portal_url=None):
    """Genera recibo PDF con DOS copias (Cliente y Control) con mejor diseño"""

    pdf_buffer = BytesIO()
    c = canvas.Canvas(pdf_buffer, pagesize=letter)

    width, height = letter
    margen = 40
    linea_ancho = width - (2 * margen)

    azul_marca = HexColor("#0066cc")
    gris_claro = HexColor("#f5f5f5")
    negro = HexColor("#000000")

    def dibujar_encabezado(y_start):
        if logo_path:
            try:
                img = ImageReader(logo_path)
                c.drawImage(img, margen + linea_ancho/2 - 25, y_start - 50, width=50, height=50, preserveAspectRatio=True, mask='auto')
            except Exception:
                pass

        c.setFont("Helvetica-Bold", 14)
        c.drawCentredString(width/2, y_start - 75, "TU AGENCIA DE VIAJES")
        c.setFont("Helvetica-Bold", 12)
        c.drawCentredString(width/2, y_start - 92, "RECIBO DE PAGO")
        c.setFont("Helvetica", 9); c.setFillColor(HexColor("#666666"))
        c.drawCentredString(width/2, y_start - 105, "Tel: 55 0000 0000  ·  tuagencia.com")
        c.setFillColor(negro)
        return y_start - 118

    def dibujar_recibo(y_inicio, tipo_copia, portal_url=None):
        y = y_inicio

        y = dibujar_encabezado(y)

        c.setFont("Helvetica-Oblique", 10)
        c.drawCentredString(width/2, y, f"COPIA {tipo_copia}")
        y -= 12

        c.setLineWidth(1)
        c.line(margen, y, margen + linea_ancho, y)
        y -= 15

        folio = f"AGP-{str(fecha_pago)[:4]}-{int(id_movimiento):04d}"

        c.setFont("Helvetica", 10)
        datos = [
            (f"Folio: {folio}", f"Operador: {operador.capitalize()}"),
            (f"Fecha: {fecha_pago}", f"Ref. interna: #{id_movimiento}"),
            (f"Cliente: {nombre_cliente}", f"Teléfono: {telefono_cliente}"),
            (f"Itinerario Nº: {id_reserva}", f"Destino: {destino}"),
        ]

        for linea in datos:
            if linea[1]:
                c.drawString(margen, y, linea[0])
                c.drawString(margen + linea_ancho/2, y, linea[1])
            else:
                c.drawString(margen, y, linea[0])
            y -= 15

        y -= 10

        c.setFont("Helvetica-Bold", 12)
        c.drawString(margen, y, "MONTO PAGADO:")
        y -= 15
        c.setFont("Helvetica-Bold", 14)
        c.setFillColor(azul_marca)
        c.drawString(margen + 15, y, f"${monto:,.2f} {moneda}")
        c.setFillColor(negro)
        y -= 12

        c.setFont("Helvetica", 9)
        c.drawString(margen + linea_ancho/2, y + 20, f"Método: {metodo_pago}")

        y -= 15
        c.setLineWidth(0.5)
        c.line(margen, y, margen + linea_ancho, y)
        y -= 15

        c.setFont("Helvetica", 9)
        c.drawString(margen, y, f"Saldo Anterior: ${saldo_anterior:,.2f} {moneda}")
        y -= 12
        c.drawString(margen, y, f"Saldo Actual: ${saldo_actual:,.2f} {moneda}")
        y -= 12

        c.setFont("Helvetica-Bold", 9)
        c.drawString(margen, y, "CONCEPTO:")
        y -= 12
        c.setFont("Helvetica", 8)
        palabras = concepto.split()
        linea_actual = ""
        linea_height = 10
        for palabra in palabras:
            if c.stringWidth(linea_actual + " " + palabra, "Helvetica", 8) > linea_ancho - 20:
                c.drawString(margen + 10, y, linea_actual)
                y -= linea_height
                linea_actual = palabra
            else:
                linea_actual += " " + palabra if linea_actual else palabra
        if linea_actual:
            c.drawString(margen + 10, y, linea_actual)

        y -= 25

        c.setFont("Helvetica-Oblique", 8)
        c.drawString(margen, y, "Sello / Firma del Operador")
        if tipo_copia == "CLIENTE" and portal_url:
            qr_size = 32
            qr_x = margen + linea_ancho - qr_size
            qr_y = y - qr_size + 7
            _dibujar_qr_portal(c, qr_x, qr_y, qr_size, portal_url, arriba=False)
        y -= 35

        return y

    y_cliente = height - 20
    y_cliente = dibujar_recibo(y_cliente, "CLIENTE", portal_url)

    y_cliente -= 10
    c.setLineWidth(1)
    c.setDash([3, 3])
    c.line(margen, y_cliente, margen + linea_ancho, y_cliente)
    c.setDash([])

    y_control = y_cliente - 30
    y_control = dibujar_recibo(y_control, "CONTROL")

    c.save()
    return pdf_buffer.getvalue()


def generar_cotizacion_pdf(cot, nombre_cliente, telefono_cliente, email_cliente, fechas_plan, logo_path, operador="", vuelos_df=None, hoteles_df=None):
    """Genera PDF de cotización profesional."""
    buf = BytesIO()
    c = canvas.Canvas(buf, pagesize=letter)
    w, h = letter
    m = 50
    azul  = HexColor("#0066cc")
    gris  = HexColor("#f5f5f5")
    negro = HexColor("#1a1a1a")
    verde = HexColor("#27ae60")

    folio = f"COT-{str(cot['fecha_cotizacion'])[:4]}-{int(cot['id_cotizacion']):04d}"

    if logo_path:
        try:
            img = ImageReader(logo_path)
            c.drawImage(img, m, h - 90, width=70, height=70, preserveAspectRatio=True, mask='auto')
        except Exception: pass
    c.setFillColor(azul); c.setFont("Helvetica-Bold", 18)
    c.drawString(m + 80, h - 55, "TU AGENCIA DE VIAJES")
    c.setFont("Helvetica", 10); c.setFillColor(HexColor("#666666"))
    c.drawString(m + 80, h - 70, "Cotización de Viaje")

    c.setFillColor(azul); c.setFont("Helvetica-Bold", 12)
    c.drawRightString(w - m, h - 50, folio)
    c.setFont("Helvetica", 9); c.setFillColor(HexColor("#666666"))
    c.drawRightString(w - m, h - 65, f"Fecha: {cot['fecha_cotizacion']}")
    c.drawRightString(w - m, h - 78, f"Válida hasta: {cot['fecha_vencimiento']}")

    c.setStrokeColor(azul); c.setLineWidth(2)
    c.line(m, h - 100, w - m, h - 100)

    y = h - 120

    c.setFillColor(azul); c.setFont("Helvetica-Bold", 11)
    c.drawString(m, y, "DATOS DEL CLIENTE")
    y -= 18
    c.setFillColor(negro); c.setFont("Helvetica", 10)
    c.drawString(m, y, f"Nombre: {nombre_cliente}")
    c.drawString(w/2, y, f"Teléfono: {telefono_cliente}")
    y -= 14
    c.drawString(m, y, f"Email: {email_cliente}")
    if operador:
        c.drawString(w/2, y, f"Atendido por: {operador.capitalize()}")
    y -= 20

    c.setFillColor(azul); c.setFont("Helvetica-Bold", 11)
    c.drawString(m, y, "DETALLES DEL VIAJE")
    y -= 18
    c.setFillColor(negro); c.setFont("Helvetica", 10)

    _es_multi_hotel = float(cot.get('hotel_op2_cobro') or 0) > 0
    _tiene_hotel = cot.get('nombre_hotel') and str(cot['nombre_hotel']) not in ('None', 'nan', '')
    _tiene_aero  = cot.get('aerolinea') and str(cot.get('aerolinea', '')) not in ('None', 'nan', '')
    viaje_datos = [
        (f"Destino: {cot['destino']}", f"Origen: {cot['origen']}"),
        (f"Fecha de salida: {cot['fecha_salida']}", f"Fecha de regreso: {cot['fecha_regreso']}"),
        (f"Pasajeros: {int(cot['num_adultos'])} adulto(s)", f"Menores: {int(cot['num_menores'])} menor(es)"),
    ]
    if not _es_multi_hotel and _tiene_hotel:
        _aero_str = f"Aerolínea: {cot.get('aerolinea','')}" if _tiene_aero else ""
        viaje_datos.append((f"Hotel: {cot['nombre_hotel']}", _aero_str))
    elif _tiene_aero:
        viaje_datos.append((f"Aerolínea: {cot.get('aerolinea','')}", ""))

    for izq, der in viaje_datos:
        c.drawString(m, y, izq)
        if der: c.drawString(w/2, y, der)
        y -= 14
    y -= 8

    # Detalle de vuelos por tramo y hoteles múltiples — solo se dibuja si hay más
    # de 1 fila (con 1 sola, ya lo muestra la línea "Aerolínea:"/"Hotel:" de arriba).
    tipo_vuelo_lbl = "Sencillo" if cot.get('tipo_vuelo') == 'SENCILLO' else "Redondo"
    if vuelos_df is not None and len(vuelos_df) > 1:
        c.setFillColor(azul); c.setFont("Helvetica-Bold", 11)
        c.drawString(m, y, f"VUELOS ({tipo_vuelo_lbl})")
        y -= 16
        c.setFillColor(negro); c.setFont("Helvetica", 9)
        for _, vr in vuelos_df.iterrows():
            _tramo = f"{vr.get('numero_tramo','')}. {vr.get('aerolinea','')} {vr.get('numero_vuelo') or ''} — {vr.get('origen') or ''} → {vr.get('destino') or ''} — {vr.get('fecha') or ''} {vr.get('hora') or ''}"
            c.drawString(m + 5, y, _tramo[:110])
            y -= 13
        y -= 8
    if hoteles_df is not None and len(hoteles_df) > 1:
        c.setFillColor(azul); c.setFont("Helvetica-Bold", 11)
        c.drawString(m, y, "HOTELES")
        y -= 16
        c.setFillColor(negro); c.setFont("Helvetica", 9)
        for _, hr in hoteles_df.iterrows():
            _htxt = f"{hr.get('numero_orden','')}. {hr.get('nombre_hotel','')} — {hr.get('ciudad_destino') or ''} ({hr.get('fecha_checkin') or ''} a {hr.get('fecha_checkout') or ''})"
            c.drawString(m + 5, y, _htxt[:110])
            y -= 13
        y -= 8

    # Igual que Streamlit: si hay 2-3 opciones de hotel, se muestra un comparativo
    # de "OPCIONES DE HOTEL" con el total de cada una y los servicios incluidos sin
    # precio individual — de lo contrario el cliente solo veía siempre la Opción 1.
    _multi_hotel = _es_multi_hotel
    _base = float(cot.get('_base_sin_hotel') or (float(cot.get('venta_total') or 0) - float(cot.get('cobro_hotel') or 0)))
    _opts = [
        ("Opción 1", cot.get('nombre_hotel') or '', float(cot.get('cobro_hotel') or 0)),
        ("Opción 2", cot.get('hotel_op2_nombre') or '', float(cot.get('hotel_op2_cobro') or 0)),
    ]
    if float(cot.get('hotel_op3_cobro') or 0) > 0:
        _opts.append(("Opción 3", cot.get('hotel_op3_nombre') or '', float(cot.get('hotel_op3_cobro') or 0)))

    if _multi_hotel:
        c.setFillColor(azul); c.setFont("Helvetica-Bold", 11)
        c.drawString(m, y, "OPCIONES DE HOTEL")
        y -= 16
        c.setFillColor(azul); c.rect(m, y - 4, w - 2*m, 14, fill=1, stroke=0)
        c.setFillColor(HexColor("#ffffff")); c.setFont("Helvetica-Bold", 9)
        c.drawString(m + 55, y + 2, "HOTEL")
        c.drawRightString(w - m - 5, y + 2, f"TOTAL PAQUETE ({cot['moneda']})")
        y -= 16
        alt2 = True
        for _lbl, _hnombre, _hprecio in _opts:
            if alt2: c.setFillColor(gris); c.rect(m, y - 4, w - 2*m, 14, fill=1, stroke=0)
            alt2 = not alt2
            c.setFillColor(azul); c.setFont("Helvetica-Bold", 9)
            c.drawString(m + 5, y + 1, _lbl)
            c.setFillColor(negro); c.setFont("Helvetica", 9)
            c.drawString(m + 55, y + 1, str(_hnombre)[:45])
            c.drawRightString(w - m - 5, y + 1, f"${_base + _hprecio:,.2f}")
            y -= 14
        y -= 10

        c.setFillColor(azul); c.setFont("Helvetica-Bold", 11)
        c.drawString(m, y, "SERVICIOS INCLUIDOS")
        y -= 16
        servicios_inc = []
        if cot['incluye_vuelo']:
            servicios_inc.append("✓ Vuelo + TUA" if cot['incluye_tua'] else "✓ Vuelo")
        elif cot['incluye_tua']:
            servicios_inc.append("✓ TUA (impuesto aeroportuario)")
        servicios_inc.append("✓ Hotel (según opción elegida)")
        if cot['incluye_traslado']:
            servicios_inc.append("✓ Traslados aeropuerto–hotel–aeropuerto")
        if float(cot['cobro_tours'] or 0) > 0:
            servicios_inc.append("✓ Tours")
        if float(cot['cobro_adicionales'] or 0) > 0:
            desc_adi = cot.get('especificar_adicionales', 'Adicionales') or 'Adicionales'
            servicios_inc.append(f"✓ {desc_adi}")
        alt3 = True
        for _svc in servicios_inc:
            if alt3: c.setFillColor(gris); c.rect(m, y - 4, w - 2*m, 14, fill=1, stroke=0)
            alt3 = not alt3
            c.setFillColor(negro); c.setFont("Helvetica", 9)
            c.drawString(m + 5, y + 1, _svc)
            y -= 14
        y -= 8

    else:
        c.setFillColor(azul); c.setFont("Helvetica-Bold", 11)
        c.drawString(m, y, "SERVICIOS Y PRECIOS")
        y -= 18

        c.setFillColor(azul)
        c.rect(m, y - 4, w - 2*m, 16, fill=1, stroke=0)
        c.setFillColor(HexColor("#ffffff")); c.setFont("Helvetica-Bold", 9)
        c.drawString(m + 5, y + 3, "SERVICIO")
        c.drawRightString(w - m - 5, y + 3, f"PRECIO ({cot['moneda']})")
        y -= 20

        servicios = []

        if cot['incluye_vuelo']:
            precio_vuelo = float(cot['cobro_vuelos'] or 0) + float(cot['cobro_tua'] or 0)
            label_vuelo  = "✓ Vuelo + TUA" if cot['incluye_tua'] else "✓ Vuelo"
            if precio_vuelo > 0:
                servicios.append((label_vuelo, precio_vuelo, False))
            else:
                servicios.append((label_vuelo, 0, True))
        elif cot['incluye_tua']:
            if float(cot['cobro_tua'] or 0) > 0:
                servicios.append(("✓ TUA (impuesto aeroportuario)", float(cot['cobro_tua']), False))
            else:
                servicios.append(("✓ TUA (impuesto aeroportuario)", 0, True))

        if cot['incluye_hotel']:
            precio_hotel = float(cot['cobro_hotel'] or 0)
            if precio_hotel > 0:
                servicios.append(("✓ Hotel", precio_hotel, False))
            else:
                servicios.append(("✓ Hotel (incluido en el paquete)", 0, True))

        if cot['incluye_traslado']:
            precio_trasl = float(cot['cobro_traslados'] or 0)
            if precio_trasl > 0:
                servicios.append(("✓ Traslados aeropuerto–hotel–aeropuerto", precio_trasl, False))
            else:
                servicios.append(("✓ Traslados aeropuerto–hotel–aeropuerto (incluido)", 0, True))

        if float(cot['cobro_tours'] or 0) > 0:
            servicios.append(("✓ Tours", float(cot['cobro_tours']), False))
        if float(cot['cobro_adicionales'] or 0) > 0:
            desc_adi = cot.get('especificar_adicionales','Adicionales') or 'Adicionales'
            servicios.append((f"✓ {desc_adi}", float(cot['cobro_adicionales']), False))

        alt = True
        for serv, precio, solo_incluido in servicios:
            if alt: c.setFillColor(gris); c.rect(m, y - 4, w - 2*m, 14, fill=1, stroke=0)
            alt = not alt
            c.setFillColor(negro); c.setFont("Helvetica", 9)
            c.drawString(m + 5, y + 1, serv)
            if solo_incluido:
                c.setFillColor(verde); c.setFont("Helvetica-Bold", 9)
                c.drawRightString(w - m - 5, y + 1, "INCLUIDO")
                c.setFillColor(negro); c.setFont("Helvetica", 9)
            else:
                c.drawRightString(w - m - 5, y + 1, f"${precio:,.2f}")
            y -= 14

        c.setFillColor(azul); c.rect(m, y - 4, w - 2*m, 16, fill=1, stroke=0)
        c.setFillColor(HexColor("#ffffff")); c.setFont("Helvetica-Bold", 10)
        c.drawString(m + 5, y + 3, "TOTAL DEL VIAJE")
        c.drawRightString(w - m - 5, y + 3, f"${cot['venta_total']:,.2f} {cot['moneda']}")
        y -= 28

    anticipo = float(cot['anticipo_requerido'])
    _PAGE_BOTTOM = 90

    def _nueva_pagina(y_cur):
        c.setStrokeColor(HexColor("#cccccc")); c.setLineWidth(0.5)
        c.line(m, 60, w - m, 60)
        c.setFillColor(HexColor("#999999")); c.setFont("Helvetica-Oblique", 8)
        c.drawCentredString(w/2, 48, "Esta cotización tiene carácter informativo. Los precios están sujetos a disponibilidad.")
        c.drawCentredString(w/2, 36, f"Tu Agencia de Viajes  ·  tuagencia.com  ·  Tel: 55 0000 0000  ·  {folio}")
        c.showPage()
        return h - m

    def _check_y(y_cur, espacio=16):
        if y_cur - espacio < _PAGE_BOTTOM:
            return _nueva_pagina(y_cur)
        return y_cur

    def _draw_plan_header(y):
        c.setFillColor(azul); c.rect(m, y - 4, w - 2*m, 14, fill=1, stroke=0)
        c.setFillColor(HexColor("#ffffff")); c.setFont("Helvetica-Bold", 8)
        c.drawString(m + 5, y + 2, "CONCEPTO")
        c.drawString(w/2 - 20, y + 2, "FECHA")
        c.drawRightString(w - m - 5, y + 2, f"MONTO ({cot['moneda']})")
        return y - 16

    def _render_plan(y, titulo, total_opcion, fechas):
        """Recalcula el monto de cada parcialidad para el total de ESTA opción
        (necesario en multi-hotel: cada opción tiene un saldo distinto) — con
        redondeo + residuo en la primera fila para que la suma cuadre exacto."""
        y = _check_y(y, 32)
        if titulo:
            c.setFillColor(azul); c.setFont("Helvetica-Bold", 10)
            c.drawString(m, y, titulo)
            y -= 16
        y = _check_y(y, 20)
        y = _draw_plan_header(y)

        saldo = total_opcion - anticipo
        n = len(fechas)
        monto_par = round(saldo / n, 2) if n > 0 else saldo
        residuo = round(saldo - monto_par * n, 2) if n > 0 else 0.0
        pagos = []
        if anticipo > 0:
            pagos.append(("💳 Anticipo al confirmar reserva", "Al firmar", anticipo))
        for i, (fecha, _) in enumerate(fechas):
            monto_fila = round(monto_par + residuo, 2) if i == 0 else monto_par
            pagos.append((f"Parcialidad #{i+1}", fecha, monto_fila))
        pagos.append(("📊 TOTAL", "", total_opcion))

        alt = True
        for concepto, fecha, monto in pagos:
            es_total = concepto.startswith("📊")
            y = _check_y(y, 16)
            if es_total:
                c.setFillColor(azul); c.rect(m, y - 4, w - 2*m, 14, fill=1, stroke=0)
                c.setFillColor(HexColor("#ffffff")); c.setFont("Helvetica-Bold", 9)
            else:
                if alt: c.setFillColor(gris); c.rect(m, y - 4, w - 2*m, 14, fill=1, stroke=0)
                alt = not alt
                c.setFillColor(negro); c.setFont("Helvetica", 9)
            c.drawString(m + 5, y + 1, concepto)
            if fecha: c.drawString(w/2 - 20, y + 1, fecha)
            if monto: c.drawRightString(w - m - 5, y + 1, f"${monto:,.2f}")
            y -= 14
        return y

    if anticipo > 0 or fechas_plan:
        y = _check_y(y, 40)
        c.setFillColor(azul); c.setFont("Helvetica-Bold", 11)
        c.drawString(m, y, "PLAN DE PAGOS SUGERIDO")
        y -= 18

        if _multi_hotel:
            for _lbl, _, _hprecio in _opts:
                _total_op = _base + _hprecio
                y = _render_plan(y, _lbl, _total_op, fechas_plan)
                y -= 10
        else:
            y = _render_plan(y, "", float(cot['venta_total']), fechas_plan)
        y -= 6

    # ── Notas ──
    if cot.get('notas') and str(cot['notas']) not in ('None','nan',''):
        c.setFillColor(azul); c.setFont("Helvetica-Bold", 10)
        c.drawString(m, y, "NOTAS Y CONDICIONES")
        y -= 14
        c.setFillColor(negro); c.setFont("Helvetica", 9)
        notas = str(cot['notas'])
        words = notas.split()
        line = ""
        for word in words:
            test = (line + " " + word).strip()
            if c.stringWidth(test, "Helvetica", 9) < (w - 2*m - 10):
                line = test
            else:
                c.drawString(m + 5, y, line); y -= 12; line = word
        if line: c.drawString(m + 5, y, line); y -= 12
        y -= 6

    # ── Footer ──
    c.setStrokeColor(HexColor("#cccccc")); c.setLineWidth(0.5)
    c.line(m, 60, w - m, 60)
    c.setFillColor(HexColor("#999999")); c.setFont("Helvetica-Oblique", 8)
    c.drawCentredString(w/2, 48, "Esta cotización tiene carácter informativo. Los precios están sujetos a disponibilidad.")
    c.drawCentredString(w/2, 36, f"Tu Agencia de Viajes  ·  tuagencia.com  ·  Tel: 55 0000 0000  ·  {folio}")

    c.save()
    return buf.getvalue()


def generar_itinerario_pdf(exp, nombre_cliente, telefono_cliente, pasajeros_df, plan_df, logo_path, habitaciones_df=None):
    buf = BytesIO()
    c = canvas.Canvas(buf, pagesize=letter)
    w, h = letter
    m = 45
    azul  = HexColor("#0066cc")
    gris  = HexColor("#f2f2f2")
    negro = HexColor("#1a1a1a")
    verde = HexColor("#27ae60")
    rojo  = HexColor("#e74c3c")

    folio = f"ITIN-{exp['id_reserva']:04d}"
    y = h - 40

    if logo_path:
        try:
            img = ImageReader(logo_path)
            c.drawImage(img, m, y - 50, width=55, height=55, preserveAspectRatio=True, mask='auto')
        except Exception: pass
    c.setFillColor(azul); c.setFont("Helvetica-Bold", 16)
    c.drawString(m + 65, y - 20, "TU AGENCIA DE VIAJES")
    c.setFont("Helvetica", 10); c.setFillColor(HexColor("#666666"))
    c.drawString(m + 65, y - 35, "Resumen de Itinerario de Viaje")
    c.setFillColor(azul); c.setFont("Helvetica-Bold", 12)
    c.drawRightString(w - m, y - 20, folio)
    c.setFont("Helvetica", 9); c.setFillColor(HexColor("#666666"))
    c.drawRightString(w - m, y - 33, f"Generado: {now_local().strftime('%Y-%m-%d %H:%M')}")
    c.setStrokeColor(azul); c.setLineWidth(2)
    c.line(m, y - 58, w - m, y - 58)
    y -= 75

    def section_title(title):
        nonlocal y
        c.setFillColor(azul); c.setFont("Helvetica-Bold", 11)
        c.drawString(m, y, title)
        y -= 16

    def row(label, value, bold_val=False):
        nonlocal y
        c.setFillColor(negro); c.setFont("Helvetica", 9)
        c.drawString(m + 5, y, label)
        c.setFont("Helvetica-Bold" if bold_val else "Helvetica", 9)
        c.drawString(m + 170, y, str(value))
        y -= 13

    def check_page():
        nonlocal y
        if y < 80:
            c.showPage()
            y = h - 40

    section_title("DATOS DEL CLIENTE Y VIAJE")
    row("Cliente:", nombre_cliente, bold_val=True)
    row("Teléfono:", telefono_cliente)
    row("Destino:", exp['destino'], bold_val=True)
    row("Origen:", exp['origen'] or "Monterrey")
    row("Salida:", str(exp['fecha_salida']))
    row("Regreso:", str(exp['fecha_regreso']))
    if exp.get('fecha_vuelo_ida') and str(exp['fecha_vuelo_ida']) not in ('None','nan',''):
        row("Vuelo ida:", f"{exp['fecha_vuelo_ida']} {exp.get('hora_vuelo_ida','') or ''}")
    if exp.get('fecha_vuelo_vuelta') and str(exp['fecha_vuelo_vuelta']) not in ('None','nan',''):
        row("Vuelo regreso:", f"{exp['fecha_vuelo_vuelta']} {exp.get('hora_vuelo_vuelta','') or ''}")
    row("Moneda:", exp['moneda'])
    row("Estado:", exp['estado'])
    y -= 8; check_page()

    section_title("PROVEEDORES")
    if exp.get('nombre_hotel') and str(exp['nombre_hotel']) not in ('None','nan',''):
        row("Hotel:", exp['nombre_hotel'])
    if exp.get('aerolinea') and str(exp['aerolinea']) not in ('None','nan',''):
        row("Aerolínea:", exp['aerolinea'])
    if exp.get('mayorista') and str(exp['mayorista']) not in ('None','nan',''):
        row("Mayorista:", exp['mayorista'])
    if exp.get('itinerario_vuelo_plataforma') and str(exp['itinerario_vuelo_plataforma']) not in ('None','nan',''):
        row("Loc. vuelo:", exp['itinerario_vuelo_plataforma'])
    if exp.get('detalle_equipaje') and str(exp['detalle_equipaje']) not in ('None','nan',''):
        row("Equipaje:", exp['detalle_equipaje'])
    if exp.get('itinerario_hotel_plataforma') and str(exp['itinerario_hotel_plataforma']) not in ('None','nan',''):
        row("Loc. hotel:", exp['itinerario_hotel_plataforma'])
    if exp.get('proveedor_traslados') and str(exp['proveedor_traslados']) not in ('None','nan',''):
        row("Prov. traslados:", exp['proveedor_traslados'])
    if exp.get('confirmacion_proveedor_traslados') and str(exp['confirmacion_proveedor_traslados']) not in ('None','nan',''):
        row("Conf. traslados:", exp['confirmacion_proveedor_traslados'])
    if exp.get('proveedor_tours') and str(exp['proveedor_tours']) not in ('None','nan',''):
        row("Prov. tours:", exp['proveedor_tours'])
    if exp.get('confirmacion_proveedor_tours') and str(exp['confirmacion_proveedor_tours']) not in ('None','nan',''):
        row("Conf. tours:", exp['confirmacion_proveedor_tours'])
    y -= 8; check_page()

    if habitaciones_df is not None and not habitaciones_df.empty:
        section_title("HABITACIONES")
        for i, hab in enumerate(habitaciones_df.itertuples(), start=1):
            check_page()
            c.setFillColor(negro); c.setFont("Helvetica-Bold", 9)
            c.drawString(m + 5, y, f"Habitación {i}: {hab.tipo_habitacion} — {int(hab.num_personas)} personas — Check-in {hab.hora_checkin or '15:00'}")
            y -= 13
            if hab.descripcion and str(hab.descripcion).strip() not in ("", "None", "nan"):
                check_page()
                c.setFont("Helvetica", 8); c.setFillColor(HexColor("#555555"))
                c.drawString(m + 15, y, str(hab.descripcion))
                y -= 12
        y -= 8; check_page()

    section_title("RESUMEN FINANCIERO")
    saldo_pend = float(exp['venta_total']) - float(exp['cobrado_cliente'])
    row("Venta total:", f"${float(exp['venta_total']):,.2f} {exp['moneda']}", bold_val=True)
    row("Cobrado:", f"${float(exp['cobrado_cliente']):,.2f} {exp['moneda']}")
    c.setFillColor(rojo if saldo_pend > 0.01 else verde)
    c.setFont("Helvetica-Bold", 9)
    c.drawString(m + 5, y, "Saldo pendiente:")
    c.drawString(m + 170, y, f"${saldo_pend:,.2f} {exp['moneda']}")
    c.setFillColor(negro); y -= 13
    y -= 8; check_page()

    if not plan_df.empty:
        section_title("PLAN DE PAGOS")
        c.setFillColor(azul); c.rect(m, y - 4, w - 2*m, 13, fill=1, stroke=0)
        c.setFillColor(HexColor("#ffffff")); c.setFont("Helvetica-Bold", 8)
        c.drawString(m + 5, y + 1, "#")
        c.drawString(m + 25, y + 1, "Fecha")
        c.drawString(m + 120, y + 1, "Monto")
        c.drawRightString(w - m - 5, y + 1, "Estado")
        y -= 14
        alt = True
        for _, pp in plan_df.iterrows():
            check_page()
            if alt: c.setFillColor(gris); c.rect(m, y - 3, w - 2*m, 12, fill=1, stroke=0)
            alt = not alt
            _est = str(pp.get('estado',''))
            c.setFillColor(verde if _est == 'PAGADO' else rojo); c.setFont("Helvetica-Bold", 8)
            c.drawRightString(w - m - 5, y + 1, _est)
            c.setFillColor(negro); c.setFont("Helvetica", 8)
            c.drawString(m + 5, y + 1, str(pp.get('numero_pago','')))
            c.drawString(m + 25, y + 1, str(pp.get('fecha_programada','')))
            c.drawString(m + 120, y + 1, f"${float(pp.get('monto_esperado',0)):,.2f}")
            y -= 13
        y -= 8; check_page()

    if not pasajeros_df.empty:
        section_title("PASAJEROS")
        for _, pax in pasajeros_df.iterrows():
            check_page()
            c.setFillColor(negro); c.setFont("Helvetica", 9)
            _fnac_pax = pax.get('fecha_nacimiento')
            _fnac_pax = '' if _fnac_pax is None or str(_fnac_pax) in ('None', 'nan', 'NaT') else str(_fnac_pax)
            c.drawString(m + 5, y, f"• {pax.get('nombre','')}  —  Nac: {_fnac_pax}")
            y -= 13
        y -= 8; check_page()

    if exp.get('notas_abiertas') and str(exp['notas_abiertas']) not in ('None','nan',''):
        section_title("NOTAS")
        notas = str(exp['notas_abiertas'])
        c.setFont("Helvetica", 8); c.setFillColor(negro)
        words = notas.split()
        line = ""
        for word in words:
            test = (line + " " + word).strip()
            if c.stringWidth(test, "Helvetica", 8) < (w - 2*m - 10):
                line = test
            else:
                check_page()
                c.drawString(m + 5, y, line); y -= 11; line = word
        if line:
            check_page(); c.drawString(m + 5, y, line); y -= 11

    if portal_url:
        qr_size = 52
        qr_x = w - m - qr_size
        qr_y = 52
        _dibujar_qr_portal(c, qr_x, qr_y, qr_size, portal_url)

    c.setStrokeColor(HexColor("#cccccc")); c.setLineWidth(0.5)
    c.line(m, 45, w - m, 45)
    c.setFillColor(HexColor("#999999")); c.setFont("Helvetica-Oblique", 7)
    c.drawString(m, 33, f"Tu Agencia de Viajes  ·  tuagencia.com  ·  Tel: 55 0000 0000  ·  {folio}")

    c.save()
    return buf.getvalue()


def generar_itinerario_cliente_pdf(exp, nombre_cliente, habitaciones_df, extras_df, logo_path, portal_url=None, vuelos_df=None, hoteles_df=None):
    """Itinerario 'boutique' orientado al cliente final: banner de portada, timeline de fechas y
    tarjetas de color por sección. Sin localizador de mayorista ni datos financieros."""
    buf = BytesIO()
    c = canvas.Canvas(buf, pagesize=letter)
    W, H = letter
    m = 45

    AZUL   = HexColor("#0066cc")
    NEGRO  = HexColor("#1a1a1a")
    GRIS   = HexColor("#6b7280")
    BLANCO = HexColor("#ffffff")

    ESTILOS = {
        "vuelos":     (HexColor("#0891b2"), HexColor("#e3f6f9")),
        "hospedaje":  (HexColor("#7c3aed"), HexColor("#f2ecfe")),
        "traslados":  (HexColor("#ea580c"), HexColor("#fdece0")),
        "tours":      (HexColor("#16a34a"), HexColor("#e7f8ee")),
        "extras":     (HexColor("#be185d"), HexColor("#fbe9f1")),
    }

    folio = f"ITIN-{exp['id_reserva']:04d}"

    def _tiene(campo):
        v = exp.get(campo)
        return v is not None and str(v).strip() not in ("", "None", "nan")

    def _wrap_lines(texto, font, size, max_w):
        words = str(texto).split()
        lines, line = [], ""
        for word in words:
            test = (line + " " + word).strip()
            if c.stringWidth(test, font, size) <= max_w:
                line = test
            else:
                if line: lines.append(line)
                line = word
        if line: lines.append(line)
        return lines or [""]

    # ---------- Banner de portada ----------
    BANNER_H = 76
    c.setFillColor(AZUL)
    c.rect(0, H - BANNER_H, W, BANNER_H, fill=1, stroke=0)
    if logo_path:
        try:
            img = ImageReader(logo_path)
            c.saveState()
            c.setFillColor(BLANCO)
            c.circle(m + 27, H - BANNER_H / 2, 27, fill=1, stroke=0)
            c.drawImage(img, m + 4, H - BANNER_H / 2 - 23, width=46, height=46, preserveAspectRatio=True, mask='auto')
            c.restoreState()
        except Exception:
            pass
    c.setFillColor(BLANCO); c.setFont("Helvetica-Bold", 18)
    c.drawString(m + 65, H - 38, "TU AGENCIA DE VIAJES")
    c.setFont("Helvetica", 10.5); c.setFillColor(HexColor("#d6e8ff"))
    c.drawString(m + 65, H - 55, "Itinerario de tu Viaje")
    c.setFillColor(BLANCO)
    c.roundRect(W - m - 112, H - 42, 112, 20, 10, fill=1, stroke=0)
    c.setFillColor(AZUL); c.setFont("Helvetica-Bold", 10)
    c.drawCentredString(W - m - 56, H - 36, folio)
    c.setFillColor(HexColor("#cfe3ff")); c.setFont("Helvetica", 7.5)
    c.drawRightString(W - m, H - 58, f"Generado: {now_local().strftime('%Y-%m-%d %H:%M')}")

    y = H - BANNER_H - 20

    # ---------- Destino + mini timeline salida/regreso ----------
    c.setFillColor(NEGRO); c.setFont("Helvetica-Bold", 20)
    c.drawString(m, y, str(exp['destino']))
    c.setFillColor(GRIS); c.setFont("Helvetica", 10)
    c.drawString(m, y - 14, f"{exp['origen'] or 'Monterrey'}  →  {exp['destino']}")
    y -= 34

    tl_x1, tl_x2 = m + 4, W - m - 4
    c.setStrokeColor(AZUL); c.setLineWidth(1.6)
    c.line(tl_x1, y, tl_x2, y)
    c.setFillColor(AZUL)
    c.circle(tl_x1, y, 4, fill=1, stroke=0)
    c.circle(tl_x2, y, 4, fill=1, stroke=0)
    c.setFont("Helvetica-Bold", 8)
    c.drawString(tl_x1, y + 9, "SALIDA")
    c.drawRightString(tl_x2, y + 9, "REGRESO")
    c.setFont("Helvetica", 8); c.setFillColor(NEGRO)
    c.drawString(tl_x1, y - 13, _fecha_larga_es(exp['fecha_salida']))
    c.drawRightString(tl_x2, y - 13, _fecha_larga_es(exp['fecha_regreso']))
    y -= 26

    # ---------- Agradecimiento ----------
    for line in _wrap_lines(
        "¡Gracias por elegir Tu Agencia de Viajes para vivir esta experiencia! Preparamos cada "
        "detalle de tu viaje pensando en ti — a continuación encontrarás tu itinerario completo. "
        "¡Que disfrutes cada momento!", "Helvetica-Oblique", 9.5, W - 2*m - 5):
        c.setFillColor(NEGRO); c.setFont("Helvetica-Oblique", 9.5)
        c.drawString(m + 5, y, line)
        y -= 11.5
    y -= 4

    # ---------- Sistema de tarjetas ----------
    def _measure_or_draw(items, content_w, cy, draw):
        for kind_i, *rest in items:
            if kind_i == "row":
                label, value, bold = rest
                if draw:
                    c.setFillColor(NEGRO); c.setFont("Helvetica", 9)
                    c.drawString(m + 16, cy, label)
                    c.setFont("Helvetica-Bold" if bold else "Helvetica", 9)
                    c.drawString(m + 150, cy, str(value))
                cy -= 12.5
            elif kind_i == "text":
                content, bold = rest
                if draw:
                    c.setFillColor(NEGRO); c.setFont("Helvetica-Bold" if bold else "Helvetica", 9)
                    c.drawString(m + 16, cy, str(content))
                cy -= 12.5
            elif kind_i == "sub":
                (content,) = rest
                if draw:
                    c.setFont("Helvetica", 8.5); c.setFillColor(HexColor("#444444"))
                    c.drawString(m + 24, cy, str(content))
                cy -= 10.5
            elif kind_i == "bullet":
                (content,) = rest
                if draw:
                    c.setFillColor(NEGRO); c.setFont("Helvetica", 9)
                    c.drawString(m + 16, cy, f"•  {content}")
                cy -= 12.5
            elif kind_i == "para":
                text, font_, size_ = rest
                for line in _wrap_lines(text, font_, size_, content_w):
                    if draw:
                        c.setFont(font_, size_); c.setFillColor(HexColor("#444444"))
                        c.drawString(m + 16, cy, line)
                    cy -= 10
            elif kind_i == "space":
                cy -= rest[0]
        return cy

    def _draw_card(kind, title, items):
        nonlocal y
        accent, bg = ESTILOS[kind]
        content_w = W - 2*m - 32
        TITLE_BLOCK = 29
        BOTTOM_PAD = 9
        body_height = -_measure_or_draw(items, content_w, 0, draw=False)
        h_card = TITLE_BLOCK + body_height + BOTTOM_PAD
        if y - h_card < 70:
            c.showPage()
            y = H - 40
        top = y
        c.setFillColor(bg)
        c.roundRect(m, top - h_card, W - 2*m, h_card, 10, fill=1, stroke=0)
        c.setFillColor(accent)
        c.roundRect(m, top - h_card, 5, h_card, 2.5, fill=1, stroke=0)
        c.roundRect(m + 16, top - 22, 9, 9, 2, fill=1, stroke=0)
        c.setFillColor(accent); c.setFont("Helvetica-Bold", 11)
        c.drawString(m + 32, top - 20, title)
        _measure_or_draw(items, content_w, top - TITLE_BLOCK, draw=True)
        y = top - h_card - 9

    _hay_vuelos_tramo = vuelos_df is not None and len(vuelos_df) > 0
    if _hay_vuelos_tramo or _tiene('fecha_vuelo_ida') or _tiene('fecha_vuelo_vuelta'):
        items = []
        if _hay_vuelos_tramo:
            _tipo_v_lbl = "Vuelo sencillo" if exp.get('tipo_vuelo') == 'SENCILLO' else "Vuelo redondo"
            items.append(("row", "Tipo:", _tipo_v_lbl, False))
            for _, vr in vuelos_df.iterrows():
                _lbl_tramo = f"Tramo {vr.get('numero_tramo','')}:"
                _val_tramo = f"{vr.get('aerolinea','')} {vr.get('numero_vuelo') or ''} — {vr.get('origen') or ''} → {vr.get('destino') or ''} — {_fecha_larga_es(vr.get('fecha'), vr.get('hora'))}"
                items.append(("row", _lbl_tramo, _val_tramo, False))
                if vr.get('localizador'):
                    items.append(("row", "  Localizador:", vr['localizador'], False))
        else:
            if _tiene('aerolinea'):
                items.append(("row", "Aerolínea:", exp['aerolinea'], False))
            if _tiene('fecha_vuelo_ida'):
                items.append(("row", "Vuelo ida:", _fecha_larga_es(exp['fecha_vuelo_ida'], exp.get('hora_vuelo_ida')), False))
            if _tiene('fecha_vuelo_vuelta'):
                items.append(("row", "Vuelo regreso:", _fecha_larga_es(exp['fecha_vuelo_vuelta'], exp.get('hora_vuelo_vuelta')), False))
            if _tiene('itinerario_vuelo_plataforma'):
                items.append(("row", "Localizador:", exp['itinerario_vuelo_plataforma'], False))
        if _tiene('detalle_equipaje'):
            items.append(("row", "Equipaje:", exp['detalle_equipaje'], False))
        items.append(("space", 4))
        items.append(("para",
            "Los pases de abordaje se otorgan 48 horas antes del vuelo, a excepción de grupos "
            "que incluyen menores de edad, cuyo check-in debe realizarse directamente en el "
            "mostrador de la aerolínea. Te recomendamos llegar al aeropuerto al menos 2 horas "
            "antes de la salida de tu vuelo nacional y 3 horas antes de la salida de tu vuelo "
            "internacional.", "Helvetica-Oblique", 8))
        _draw_card("vuelos", "VUELOS", items)

    _hay_hoteles_multi = hoteles_df is not None and len(hoteles_df) > 0
    if _hay_hoteles_multi:
        items = []
        for _, hr in hoteles_df.iterrows():
            _lbl_h = f"{hr.get('ciudad_destino') or 'Hotel'}:" if hr.get('ciudad_destino') else "Hotel:"
            items.append(("row", _lbl_h, hr.get('nombre_hotel',''), True))
            _fechas_h = f"{hr.get('fecha_checkin') or ''} a {hr.get('fecha_checkout') or ''}".strip()
            if _fechas_h != "a":
                items.append(("row", "  Fechas:", _fechas_h, False))
            if hr.get('localizador'):
                items.append(("row", "  Confirmación:", hr['localizador'], False))
        _draw_card("hospedaje", "HOSPEDAJE", items)
    elif _tiene('nombre_hotel'):
        items = [("row", "Hotel:", exp['nombre_hotel'], True)]
        if not _tiene('mayorista') and _tiene('itinerario_hotel_plataforma'):
            items.append(("row", "Confirmación:", exp['itinerario_hotel_plataforma'], False))
        if habitaciones_df is not None and not habitaciones_df.empty:
            items.append(("space", 2))
            for i, hab in enumerate(habitaciones_df.itertuples(), start=1):
                items.append(("text", f"Habitación {i}: {hab.tipo_habitacion}", True))
                items.append(("sub", f"{int(hab.num_personas)} personas   ·   Check-in: {hab.hora_checkin or '15:00'} horas"))
                if hab.descripcion and str(hab.descripcion).strip() not in ("", "None", "nan"):
                    items.append(("sub", str(hab.descripcion)))
        items.append(("space", 4))
        items.append(("para",
            "Impuestos locales, tasas ecológicas o de saneamiento, o cargos por servicio de "
            "resort (Resort Fees) no están incluidos en el paquete. Se pagan directamente en "
            "el hotel al hacer check-in, conforme a la legislación del destino.", "Helvetica-Oblique", 8))
        _draw_card("hospedaje", "HOSPEDAJE", items)

    if _tiene('proveedor_traslados') or (exp.get('cobro_traslados') or 0) > 0:
        items = []
        if _tiene('proveedor_traslados'):
            items.append(("row", "Proveedor:", exp['proveedor_traslados'], False))
        if _tiene('confirmacion_proveedor_traslados'):
            items.append(("row", "Confirmación:", exp['confirmacion_proveedor_traslados'], False))
        _draw_card("traslados", "TRASLADOS", items)

    if _tiene('proveedor_tours'):
        items = [("row", "Proveedor:", exp['proveedor_tours'], False)]
        if _tiene('confirmacion_proveedor_tours'):
            items.append(("row", "Confirmación:", exp['confirmacion_proveedor_tours'], False))
        _draw_card("tours", "TOURS", items)

    if extras_df is not None and not extras_df.empty:
        items = [("bullet", str(extra.descripcion)) for extra in extras_df.itertuples()]
        _draw_card("extras", "SERVICIOS ADICIONALES", items)

    if y < 95:
        c.showPage(); y = H - 40
    c.setStrokeColor(HexColor("#dddddd")); c.setLineWidth(0.5)
    c.line(m, y, W - m, y)
    y -= 13
    for line in _wrap_lines(
        "Fue un placer acompañarte a planear este viaje. Esperamos que cada momento supere tus "
        "expectativas y que nos permitas ser parte de tu próxima aventura. Si necesitas cualquier "
        "cosa durante tu viaje, estamos a un mensaje de distancia: 55 0000 0000. ¡Buen viaje!",
        "Helvetica-Oblique", 9, W - 2*m - 5):
        c.setFillColor(NEGRO); c.setFont("Helvetica-Oblique", 9)
        c.drawString(m + 5, y, line)
        y -= 11.5

    if portal_url:
        qr_size = 52
        qr_x = W - m - qr_size
        qr_y = 52
        _dibujar_qr_portal(c, qr_x, qr_y, qr_size, portal_url)

    c.setStrokeColor(HexColor("#cccccc")); c.setLineWidth(0.5)
    c.line(m, 45, W - m, 45)
    c.setFillColor(HexColor("#999999")); c.setFont("Helvetica-Oblique", 7)
    c.drawString(m, 33, f"Tu Agencia de Viajes  ·  tuagencia.com  ·  Tel: 55 0000 0000  ·  {folio}")

    c.save()
    return buf.getvalue()


def generar_estado_cuenta_pdf(exp, nombre_cliente, telefono_cliente, email_cliente, movimientos_df, plan_df, logo_path, portal_url=None):
    buf = BytesIO()
    c = canvas.Canvas(buf, pagesize=letter)
    w, h = letter
    m = 45
    azul  = HexColor("#0066cc")
    gris  = HexColor("#f2f2f2")
    negro = HexColor("#1a1a1a")
    verde = HexColor("#27ae60")
    rojo  = HexColor("#e74c3c")
    naranja = HexColor("#e67e22")

    folio = f"EDC-{exp['id_reserva']:04d}"
    moneda = exp['moneda']
    venta_total = float(exp['venta_total'])
    cobrado = float(exp['cobrado_cliente'])
    saldo = venta_total - cobrado
    y = h - 40

    if logo_path:
        try:
            img = ImageReader(logo_path)
            c.drawImage(img, m, y - 55, width=60, height=60, preserveAspectRatio=True, mask='auto')
        except Exception: pass
    c.setFillColor(azul); c.setFont("Helvetica-Bold", 16)
    c.drawString(m + 70, y - 18, "TU AGENCIA DE VIAJES")
    c.setFont("Helvetica", 10); c.setFillColor(HexColor("#666666"))
    c.drawString(m + 70, y - 32, "Estado de Cuenta del Cliente")
    c.drawString(m + 70, y - 44, "Tel: 55 0000 0000  ·  tuagencia.com")
    c.setFillColor(azul); c.setFont("Helvetica-Bold", 13)
    c.drawRightString(w - m, y - 18, folio)
    c.setFont("Helvetica", 9); c.setFillColor(HexColor("#666666"))
    c.drawRightString(w - m, y - 31, f"Emitido: {now_local().strftime('%Y-%m-%d %H:%M')}")
    c.setStrokeColor(azul); c.setLineWidth(2)
    c.line(m, y - 62, w - m, y - 62)
    y -= 78

    def check_page():
        nonlocal y
        if y < 80:
            c.showPage()
            y = h - 50
            c.setStrokeColor(HexColor("#cccccc")); c.setLineWidth(0.5)
            c.line(m, h - 30, w - m, h - 30)

    def section_title(title):
        nonlocal y
        check_page()
        c.setFillColor(azul); c.setFont("Helvetica-Bold", 11)
        c.drawString(m, y, title)
        y -= 16

    def row2(label, value, color=None):
        nonlocal y
        check_page()
        c.setFillColor(negro); c.setFont("Helvetica", 9)
        c.drawString(m + 5, y, label)
        if color: c.setFillColor(color)
        c.setFont("Helvetica-Bold" if color else "Helvetica", 9)
        c.drawString(m + 175, y, str(value))
        c.setFillColor(negro)
        y -= 13

    section_title("DATOS DEL CLIENTE")
    row2("Cliente:", nombre_cliente)
    row2("Teléfono:", telefono_cliente)
    if email_cliente and str(email_cliente) not in ('None','nan',''):
        row2("Email:", email_cliente)
    y -= 6

    section_title("DETALLES DEL VIAJE")
    row2("Destino:", exp['destino'])
    row2("Origen:", exp['origen'] or "Monterrey")
    row2("Salida:", str(exp['fecha_salida']))
    row2("Regreso:", str(exp['fecha_regreso']))
    if exp.get('nombre_hotel') and str(exp['nombre_hotel']) not in ('None','nan',''):
        row2("Hotel:", exp['nombre_hotel'])
    if exp.get('aerolinea') and str(exp['aerolinea']) not in ('None','nan',''):
        row2("Aerolínea:", exp['aerolinea'])
    if exp.get('itinerario_vuelo_plataforma') and str(exp['itinerario_vuelo_plataforma']) not in ('None','nan',''):
        row2("Localizador vuelo:", exp['itinerario_vuelo_plataforma'])
    y -= 6

    section_title("RESUMEN FINANCIERO")
    row2("Total del viaje:", f"${venta_total:,.2f} {moneda}")
    row2("Total abonado:", f"${cobrado:,.2f} {moneda}", color=verde)
    row2("Saldo pendiente:", f"${saldo:,.2f} {moneda}", color=rojo if saldo > 0.01 else verde)
    y -= 6

    section_title("HISTORIAL DE PAGOS REALIZADOS")
    movs = movimientos_df[movimientos_df['tipo_movimiento'] == 'INGRESO'] if not movimientos_df.empty else movimientos_df
    movs = movs[movs['estado'] == 'ACTIVO'] if not movs.empty else movs

    if movs.empty:
        c.setFillColor(negro); c.setFont("Helvetica-Oblique", 9)
        c.drawString(m + 5, y, "Sin pagos registrados.")
        y -= 13
    else:
        check_page()
        c.setFillColor(azul); c.rect(m, y - 4, w - 2*m, 14, fill=1, stroke=0)
        c.setFillColor(HexColor("#ffffff")); c.setFont("Helvetica-Bold", 8)
        c.drawString(m + 5, y + 2, "FECHA")
        c.drawString(m + 65, y + 2, "CONCEPTO")
        c.drawString(m + 260, y + 2, "MÉTODO")
        c.drawRightString(w - m - 5, y + 2, f"MONTO ({moneda})")
        y -= 15

        alt = True
        total_pagado = 0.0
        for _, mv in movs.iterrows():
            check_page()
            if alt: c.setFillColor(gris); c.rect(m, y - 3, w - 2*m, 13, fill=1, stroke=0)
            alt = not alt
            c.setFillColor(negro); c.setFont("Helvetica", 8)
            c.drawString(m + 5, y + 1, str(mv.get('fecha_pago',''))[:10])
            concepto_txt = str(mv.get('concepto',''))[:42]
            c.drawString(m + 65, y + 1, concepto_txt)
            _met_raw = mv.get('metodo_pago','')
            metodo_txt = '' if (not _met_raw or str(_met_raw) in ('nan','None','')) else str(_met_raw)[:14]
            c.drawString(m + 260, y + 1, metodo_txt)
            monto_mv = float(mv.get('monto', 0))
            total_pagado += monto_mv
            c.setFont("Helvetica-Bold", 8)
            c.drawRightString(w - m - 5, y + 1, f"${monto_mv:,.2f}")
            c.setFont("Helvetica", 8)
            y -= 13

        check_page()
        c.setFillColor(verde); c.rect(m, y - 4, w - 2*m, 14, fill=1, stroke=0)
        c.setFillColor(HexColor("#ffffff")); c.setFont("Helvetica-Bold", 9)
        c.drawString(m + 5, y + 2, f"TOTAL ABONADO")
        c.drawRightString(w - m - 5, y + 2, f"${total_pagado:,.2f} {moneda}")
        y -= 20

    if not plan_df.empty:
        pendientes = plan_df[plan_df['estado'] == 'PENDIENTE']
        if not pendientes.empty:
            y -= 4; section_title("PLAN DE PAGOS PENDIENTE")
            check_page()
            c.setFillColor(naranja); c.rect(m, y - 4, w - 2*m, 14, fill=1, stroke=0)
            c.setFillColor(HexColor("#ffffff")); c.setFont("Helvetica-Bold", 8)
            c.drawString(m + 5, y + 2, "#")
            c.drawString(m + 30, y + 2, "FECHA LÍMITE")
            c.drawRightString(w - m - 5, y + 2, f"MONTO ({moneda})")
            y -= 15

            alt = True
            for _, pp in pendientes.iterrows():
                check_page()
                if alt: c.setFillColor(gris); c.rect(m, y - 3, w - 2*m, 13, fill=1, stroke=0)
                alt = not alt
                c.setFillColor(negro); c.setFont("Helvetica", 9)
                c.drawString(m + 5, y + 1, str(pp.get('numero_pago','')))
                c.drawString(m + 30, y + 1, str(pp.get('fecha_programada','')))
                c.setFont("Helvetica-Bold", 9)
                c.drawRightString(w - m - 5, y + 1, f"${float(pp.get('monto_esperado',0)):,.2f}")
                y -= 13

            check_page()
            c.setFillColor(rojo if saldo > 0.01 else verde)
            c.rect(m, y - 4, w - 2*m, 16, fill=1, stroke=0)
            c.setFillColor(HexColor("#ffffff")); c.setFont("Helvetica-Bold", 10)
            c.drawString(m + 5, y + 3, "SALDO TOTAL PENDIENTE")
            c.drawRightString(w - m - 5, y + 3, f"${saldo:,.2f} {moneda}")
            y -= 20

    check_page()
    y -= 10
    c.setFillColor(HexColor("#f8f9fa")); c.rect(m, y - 18, w - 2*m, 28, fill=1, stroke=0)
    c.setFillColor(azul); c.setFont("Helvetica-Bold", 9)
    c.drawCentredString(w/2, y + 4, "¡Gracias por confiar en Tu Agencia de Viajes!")
    c.setFillColor(HexColor("#666666")); c.setFont("Helvetica", 8)
    c.drawCentredString(w/2, y - 8, "Para cualquier duda sobre su estado de cuenta, contáctenos al 55 0000 0000")

    if portal_url:
        qr_size = 52
        qr_x = w - m - qr_size
        qr_y = 52
        _dibujar_qr_portal(c, qr_x, qr_y, qr_size, portal_url)

    c.setStrokeColor(HexColor("#cccccc")); c.setLineWidth(0.5)
    c.line(m, 45, w - m, 45)
    c.setFillColor(HexColor("#999999")); c.setFont("Helvetica-Oblique", 7)
    c.drawString(m, 33, f"Tu Agencia de Viajes  ·  tuagencia.com  ·  Tel: 55 0000 0000  ·  {folio}")

    c.save()
    return buf.getvalue()
