# ERP Agencia de Viajes

> **English**: A self-hosted, open-source back-office system (CRM/ERP) for small travel agencies — clients, quotes, itineraries, payment plans, group trips, and an integrated AI assistant. Built with FastAPI + SQLite. UI and docs are in Spanish (LatAm market); see [License](#licencia) below (PolyForm Noncommercial — free for non-commercial use, commercial license available on request).

Sistema de gestión (CRM/ERP) para agencias de viajes pequeñas y medianas: clientes, cotizaciones, itinerarios, planes de pago, viajes grupales, y un asistente de IA integrado. Pensado para que una agencia de 2 a 10 personas controle su operación completa sin depender de Excel.

Este proyecto es una versión **genérica y saneada** de un sistema que lleva más de un año en producción real en una agencia de viajes — no es un ejercicio ni una maqueta.

## Módulos incluidos

- **Torre de Control** — dashboard con lo que pasa hoy: cobros, pagos atrasados, próximas salidas.
- **Clientes** — ficha completa, viajeros frecuentes, detección de duplicados.
- **Cotizaciones** — hasta 3 opciones de hotel por cotización, PDF, conversión a itinerario con un clic.
- **Itinerarios** — el corazón del sistema: costos, proveedores, cobros, plan de pagos, semáforo de pagos a proveedor, extras, cancelación.
- **Grupos de Viaje** — presupuesto compartido y prorrateo automático entre reservas.
- **Calendario** — vista mensual de salidas, regresos y pagos programados.
- **Ingresos / Egresos / Libro Diario** — control financiero completo, separado de los cobros a clientes.
- **Auditoría** — bitácora de toda acción sensible (cancelaciones, anulaciones, cambios de rol).
- **Admin** — usuarios, roles, métodos de pago, rentabilidad.
- **Asistente IA** — widget de chat integrado que consulta datos reales (saldos, itinerarios, alertas) y puede dejar propuestas de alta de cliente/cotización listas para confirmar — nunca escribe en la base de datos sin que un humano confirme.
- **Manual de usuario** incluido (`docs/manual/`) — se sirve dentro de la misma app en `/manual`, con buscador y control de acceso por rol.

## Stack tecnológico

- **Backend**: FastAPI + Jinja2 + HTMX (sin frontend build, sin Node)
- **Base de datos**: SQLite
- **PDF**: ReportLab
- **Excel**: openpyxl / pandas
- **IA**: [Groq](https://groq.com) (inferencia) corriendo el modelo de pesos abiertos [`openai/gpt-oss-120b`](https://openai.com/index/gpt-oss/) (licencia Apache 2.0)
- **WhatsApp** (opcional): Meta WhatsApp Cloud API

## Instalación

```bash
python3 -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt

cp .env.example .env
# Edita .env y pon tu GROQ_API_KEY (gratis en https://console.groq.com/keys)

uvicorn main:app --reload
```

Abre `http://localhost:8000` — la base de datos (`erp.db`) se crea sola en el primer arranque, vacía.

### Primer login

```
Usuario:     admin
Contraseña:  admin
```

El sistema te forzará a cambiar esta contraseña en el primer login. **Cámbiala de inmediato si vas a exponer el sistema fuera de tu red local.**

### WhatsApp (opcional)

El módulo de WhatsApp está desactivado por defecto (`HIDE_WHATSAPP=1` en `.env.example`). Si quieres activarlo, necesitas tu propia cuenta de Meta Business y llenar `WA_PHONE_NUMBER_ID`, `WA_ACCESS_TOKEN` y `WA_VERIFY_TOKEN` en tu `.env`, y quitar `HIDE_WHATSAPP`.

## Estructura del proyecto

```
main.py              # rutas y lógica de la aplicación
database.py           # esquema, migraciones, funciones de acceso a datos
auth.py                # hashing y validación de contraseñas
pdf_engine.py          # generación de PDFs (cotizaciones, itinerarios, recibos)
wa.py                    # integración WhatsApp Business (opcional)
asistente_ia.py           # asistente de IA (Groq)
asistente_ia_tools.py       # funciones de negocio que puede usar el asistente
templates/                    # vistas Jinja2
static/                        # CSS, JS, imágenes
docs/manual/                    # manual de usuario (servido en /manual)
```

## Créditos y servicios de terceros

Este proyecto no incluye ni redistribuye ninguna cuenta, credencial ni servicio de terceros — cada quien debe contratar y configurar los suyos.

- **[Groq](https://groq.com)** — proveedor de inferencia usado por el Asistente IA, corriendo **`openai/gpt-oss-120b`** (modelo de pesos abiertos publicado por OpenAI bajo licencia Apache 2.0). Nota histórica: una versión anterior de este sistema usaba Google Gemini, migrado por bloqueos de IP de datacenter en el nivel gratuito de esa API.
- **[Meta WhatsApp Cloud API](https://developers.facebook.com/docs/whatsapp/cloud-api)** — usada por el módulo opcional de WhatsApp.

Ninguna marca de estos proveedores (ni de ningún proveedor de hosting/dominio que uses para desplegarlo) pertenece a este proyecto ni se reclama como propia.

## Licencia

Este proyecto se distribuye bajo **[PolyForm Noncommercial 1.0.0](LICENSE.md)**:

- ✅ Puedes usar, estudiar, modificar y distribuir el código libremente para cualquier propósito **no comercial**.
- ❌ **No puedes usarlo con fines comerciales** (venderlo, ofrecerlo como servicio de pago, usarlo dentro de un negocio que genera ingresos) sin una licencia comercial aparte.
- 💼 Para licenciamiento comercial, escribe a **carlos.garza88@gmail.com**.
