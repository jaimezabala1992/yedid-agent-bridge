"""Proceso worker independiente y aislado para transcripción de voz con faster-whisper.

Este proceso corre de manera desacoplada del servidor principal FastAPI para garantizar
que al descargarse tras el periodo de inactividad (60 segundos), el sistema operativo Windows
recupere el 100% de la memoria RAM consumida por el modelo y las librerías CTranslate2.
"""
import json
import logging
import os
import sys
import time

# Configuración de salida sin búfer para comunicación IPC inmediata
sys.stdout.reconfigure(line_buffering=True)
sys.stderr.reconfigure(line_buffering=True)

logger = logging.getLogger("whisper_worker")

def main():
    model = None

    # Emitir señal de preparación (ready) a través de stdout
    print(json.dumps({"status": "ready"}), flush=True)

    for line in sys.stdin:
        clean_line = line.strip()
        if not clean_line:
            continue

        try:
            req = json.loads(clean_line)
            action = req.get("action")

            if action == "exit":
                print(json.dumps({"status": "exiting"}), flush=True)
                break

            elif action == "ping":
                print(json.dumps({"status": "pong", "pid": os.getpid()}), flush=True)

            elif action == "transcribe":
                file_path = req.get("file_path")
                language = req.get("language", "es")

                if not file_path or not os.path.exists(file_path):
                    print(json.dumps({"success": False, "error": f"Archivo de audio no encontrado: {file_path}"}), flush=True)
                    continue

                if model is None:
                    t_load_0 = time.time()
                    from faster_whisper import WhisperModel
                    # Modelo tiny en CPU con int8: máxima velocidad y mínimo consumo (~75MB base)
                    model = WhisperModel("tiny", device="cpu", compute_type="int8")
                    t_load = round((time.time() - t_load_0) * 1000, 1)
                else:
                    t_load = 0.0

                t_trans_0 = time.time()
                segments, info = model.transcribe(
                    file_path,
                    language=language,
                    beam_size=5,
                    vad_filter=True
                )
                text = " ".join([seg.text.strip() for seg in segments]).strip()
                t_trans = round((time.time() - t_trans_0) * 1000, 1)

                resp = {
                    "success": True,
                    "text": text,
                    "duration": round(info.duration, 2),
                    "language": info.language,
                    "load_ms": t_load,
                    "transcribe_ms": t_trans,
                    "pid": os.getpid()
                }
                print(json.dumps(resp), flush=True)

            else:
                print(json.dumps({"success": False, "error": f"Accion desconocida: {action}"}), flush=True)

        except Exception as e:
            print(json.dumps({"success": False, "error": str(e)}), flush=True)

    if model is not None:
        del model
    sys.exit(0)

if __name__ == "__main__":
    main()
