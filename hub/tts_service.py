"""Servicio de Síntesis de Voz (TTS) 100% Local y Offline para Yedid Hub."""
import os
import tempfile
import logging
from typing import Optional
from .audit import log_event

logger = logging.getLogger("yedid_tts")

def synthesize_speech_sapi(text: str) -> bytes:
    """Genera audio WAV en español utilizando el motor SAPI5 nativo de Windows (voz Helena)."""
    import win32com.client
    voice = win32com.client.Dispatch("SAPI.SpVoice")
    stream = win32com.client.Dispatch("SAPI.SpFileStream")

    # Seleccionar voz en español (Microsoft Helena Desktop - Spanish)
    for v in voice.GetVoices():
        desc = v.GetDescription()
        if "Spanish" in desc or "Helena" in desc:
            voice.Voice = v
            break

    # Configurar velocidad natural
    voice.Rate = 0  # 0 es velocidad normal

    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
        tmp_path = tmp.name

    try:
        # SSFMCreateForWrite = 3
        stream.Open(tmp_path, 3)
        voice.AudioOutputStream = stream
        voice.Speak(text)
        stream.Close()

        with open(tmp_path, "rb") as f:
            audio_bytes = f.read()

        log_event("tts_synthesis_success", {
            "engine": "Windows SAPI5 (Helena)",
            "char_count": len(text),
            "byte_size": len(audio_bytes)
        })
        return audio_bytes
    finally:
        if os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except Exception:
                pass
