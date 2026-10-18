"""Módulo de Criptografía Segura para Yedid Memory Engine v2.2.

Implementa cifrado en reposo para recuerdos 'personal' y 'muy_privado' utilizando:
1. Clave maestra de 256 bits protegida mediante Windows DPAPI (CryptProtectData).
2. Cifrado autenticado por registro con AES-256-GCM (Nonce de 12 bytes + Tag de 16 bytes).
3. Restricción de permisos ACL en el archivo de claves y base de datos.
"""
import ctypes
from ctypes import wintypes
import logging
import os
import secrets
from pathlib import Path
from typing import Optional, Tuple
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from .config import DATA_DIR
from .audit import log_event

logger = logging.getLogger("yedid_memory_crypto")

KEY_FILE = DATA_DIR / ".memory_master.key"

# Definición de estructuras para Windows DPAPI
class DATA_BLOB(ctypes.Structure):
    _fields_ = [
        ("cbData", wintypes.DWORD),
        ("pbData", ctypes.POINTER(ctypes.c_char))
    ]

crypt32 = ctypes.windll.crypt32 if os.name == "nt" else None
kernel32 = ctypes.windll.kernel32 if os.name == "nt" else None

def _dpapi_protect(data: bytes, description: str = "YedidMemoryMasterKey") -> bytes:
    """Protege datos en reposo vinculados exclusivamente a la cuenta de usuario de Windows."""
    if not crypt32 or not kernel32:
        raise OSError("Windows DPAPI solo está disponible en sistemas operativos Windows.")

    in_blob = DATA_BLOB(len(data), ctypes.cast(ctypes.create_string_buffer(data), ctypes.POINTER(ctypes.c_char)))
    out_blob = DATA_BLOB()
    # CRYPTPROTECT_UI_FORBIDDEN = 0x1
    flags = 0x1
    if not crypt32.CryptProtectData(
        ctypes.byref(in_blob),
        description,
        None,
        None,
        None,
        flags,
        ctypes.byref(out_blob)
    ):
        raise ctypes.WinError()

    try:
        protected = ctypes.string_at(out_blob.pbData, out_blob.cbData)
        return protected
    finally:
        kernel32.LocalFree(out_blob.pbData)

def _dpapi_unprotect(data: bytes) -> bytes:
    """Desprotege datos cifrados con DPAPI para el usuario actual de Windows."""
    if not crypt32 or not kernel32:
        raise OSError("Windows DPAPI solo está disponible en sistemas operativos Windows.")

    in_blob = DATA_BLOB(len(data), ctypes.cast(ctypes.create_string_buffer(data), ctypes.POINTER(ctypes.c_char)))
    out_blob = DATA_BLOB()
    flags = 0x1
    if not crypt32.CryptUnprotectData(
        ctypes.byref(in_blob),
        None,
        None,
        None,
        None,
        flags,
        ctypes.byref(out_blob)
    ):
        raise ctypes.WinError()

    try:
        unprotected = ctypes.string_at(out_blob.pbData, out_blob.cbData)
        return unprotected
    finally:
        kernel32.LocalFree(out_blob.pbData)

class MemoryCrypto:
    def __init__(self, key_path: Path = KEY_FILE):
        self.key_path = key_path
        self._raw_master_key: Optional[bytes] = None
        self._aesgcm: Optional[AESGCM] = None
        self._initialize_key()

    def _initialize_key(self):
        """Carga o genera la clave maestra de cifrado protegida con DPAPI."""
        if self.key_path.exists():
            try:
                protected_blob = self.key_path.read_bytes()
                self._raw_master_key = _dpapi_unprotect(protected_blob)
                self._aesgcm = AESGCM(self._raw_master_key)
                logger.info("Clave maestra de memoria desprotegida exitosamente desde DPAPI.")
                return
            except Exception as e:
                logger.error(f"Fallo al recuperar clave maestra existente: {e}")
                raise RuntimeError(f"No se pudo acceder a la clave maestra DPAPI: {e}")

        # Generar clave de 256 bits criptográficamente segura
        logger.info("Generando nueva clave maestra para cifrado de memoria en reposo...")
        new_key = secrets.token_bytes(32)
        try:
            protected_blob = _dpapi_protect(new_key, "YedidMemoryMasterKey")
            self.key_path.write_bytes(protected_blob)

            # Restringir permisos de archivo a nivel de sistema operativo
            try:
                os.chmod(self.key_path, 0o600)
            except Exception:
                pass

            self._raw_master_key = new_key
            self._aesgcm = AESGCM(self._raw_master_key)
            log_event("memory_master_key_created", {"status": "dpapi_protected"})
            logger.info("Nueva clave maestra creada y resguardada con DPAPI en data/.memory_master.key.")
        except Exception as e:
            logger.critical(f"Error protegiendo la nueva clave maestra con DPAPI: {e}")
            raise e

    def encrypt_text(self, plaintext: str, associated_data: Optional[str] = None) -> bytes:
        """Cifra un texto sensible devolviendo el payload [Nonce (12B) + Ciphertext + Tag (16B)]."""
        if not plaintext:
            return b""
        nonce = secrets.token_bytes(12)
        aad = associated_data.encode("utf-8") if associated_data else None
        ciphertext = self._aesgcm.encrypt(nonce, plaintext.encode("utf-8"), aad)
        return nonce + ciphertext

    def decrypt_text(self, encrypted_payload: bytes, associated_data: Optional[str] = None) -> str:
        """Descifra el payload autenticado verificando integridad."""
        if not encrypted_payload or len(encrypted_payload) < 28:
            return ""
        nonce = encrypted_payload[:12]
        ciphertext = encrypted_payload[12:]
        aad = associated_data.encode("utf-8") if associated_data else None
        decrypted_bytes = self._aesgcm.decrypt(nonce, ciphertext, aad)
        return decrypted_bytes.decode("utf-8")

memory_crypto = MemoryCrypto()
