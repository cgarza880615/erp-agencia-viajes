import hashlib
import re
import os
import logging
from database import obtener_datos, ejecutar_comando, now_local


def encriptar_password(password):
    """Genera hash PBKDF2. Formato: pbkdf2$iters$salt_hex$hash_hex"""
    salt = os.urandom(16)
    dk = hashlib.pbkdf2_hmac('sha256', password.encode('utf-8'), salt, 260000)
    return f"pbkdf2$260000${salt.hex()}${dk.hex()}"


def verificar_password(password, stored_hash):
    """Verifica password contra hash PBKDF2 (nuevo) o SHA256 (legacy)."""
    if stored_hash and stored_hash.startswith('pbkdf2$'):
        try:
            _, iters, salt_hex, hash_hex = stored_hash.split('$')
            dk = hashlib.pbkdf2_hmac('sha256', password.encode('utf-8'), bytes.fromhex(salt_hex), int(iters))
            return dk.hex() == hash_hex
        except Exception:
            return False
    return hashlib.sha256(password.encode()).hexdigest() == stored_hash


def validar_password(nueva_pwd, usuario):
    """Valida política de contraseñas. Retorna (True, None) o (False, mensaje)."""
    if len(nueva_pwd) < 8:
        return False, "⚠️ Mínimo 8 caracteres."
    if not re.search(r'[A-Z]', nueva_pwd):
        return False, "⚠️ Debe incluir al menos una letra mayúscula."
    if not re.search(r'[?!$,.\-_@#%&*]', nueva_pwd):
        return False, "⚠️ Debe incluir al menos un símbolo ( ? ! $ , . - _ @ # % & * )."
    df_hist = obtener_datos(
        "SELECT password_hash FROM historial_passwords WHERE usuario=? ORDER BY id DESC LIMIT 10",
        (usuario,)
    )
    if not df_hist.empty:
        for _old_hash in df_hist['password_hash'].values:
            if verificar_password(nueva_pwd, _old_hash):
                return False, "⚠️ No puedes reutilizar ninguna de las últimas 10 contraseñas."
    return True, None


def guardar_historial_password(usuario, password_hash):
    """Guarda el hash en historial y conserva solo los últimos 10."""
    ejecutar_comando(
        "INSERT INTO historial_passwords (usuario, password_hash, fecha_cambio) VALUES (?, ?, ?)",
        (usuario, password_hash, str(now_local().date()))
    )
    ejecutar_comando(
        """DELETE FROM historial_passwords WHERE usuario=? AND id NOT IN (
               SELECT id FROM historial_passwords WHERE usuario=? ORDER BY id DESC LIMIT 10)""",
        (usuario, usuario)
    )
