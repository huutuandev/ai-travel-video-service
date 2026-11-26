import atexit
import json
import os
import subprocess
import sys
from contextlib import contextmanager, redirect_stdout
from functools import lru_cache
from pathlib import Path
from queue import Empty, Queue
from threading import Thread
from time import monotonic, sleep

from app.core.exceptions import MediaError

MODEL_ID = "pnnbao-ump/VieNeu-TTS-v3-Nano"
MODEL_REVISION = "a239fbfe19ca572f1d5f4316521806ddacc0b279"
MODEL_FILES = (
    "text_encoder.onnx", "duration_predictor.onnx", "vector_estimator.onnx",
    "codec_decoder.onnx", "config.json", "constants.npz",
)


class NarrationError(MediaError):
    pass


ERRORS = {
    "TTS_MODEL_UNAVAILABLE": "Run setup_tts_model.py and mount the model directory",
    "TTS_MODEL_LOAD_FAILED": "VieNeu could not load. Check SDK, model files and available RAM",
    "TTS_VOICE_INVALID": "Voice is not a preset in the installed VieNeu model",
    "TTS_INFERENCE_FAILED": "Local synthesis failed",
    "TTS_TIMEOUT": "Local synthesis exceeded its time limit",
    "TTS_DISABLED": "Enable worker TTS or supply a voiceover",
}


def tts_error(code):
    return NarrationError(code, ERRORS[code])


def check_model(directory):
    if not all((directory / name).is_file() and (directory / name).stat().st_size
               for name in MODEL_FILES):
        raise tts_error("TTS_MODEL_UNAVAILABLE")


@contextmanager
def synthesis_slot(config, deadline):
    directory = config.media_root / ".tts-locks"
    directory.mkdir(parents=True, exist_ok=True)
    if os.name == "nt":
        import msvcrt

        def lock(stream, release=False):
            stream.seek(0)
            msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK if release else msvcrt.LK_NBLCK, 1)
    else:
        import fcntl

        def lock(stream, release=False):
            fcntl.flock(stream, fcntl.LOCK_UN if release else fcntl.LOCK_EX | fcntl.LOCK_NB)

    while monotonic() < deadline:
        for index in range(config.tts_max_concurrent):
            with (directory / str(index)).open("a+b") as stream:
                try:
                    lock(stream)
                except OSError:
                    continue
                try:
                    yield
                finally:
                    lock(stream, release=True)
                return
        sleep(min(0.15, max(0, deadline - monotonic())))
    raise tts_error("TTS_TIMEOUT")


class LocalTTS:
    def __init__(self, directory):
        self.directory = directory
        self.process = None

    def close(self):
        process, self.process = self.process, None
        if process is not None:
            if process.poll() is None:
                process.kill()
            process.wait()
            process.stdin.close()
            process.stdout.close()

    def synthesize(self, text, voice, target, deadline):
        remaining = deadline - monotonic()
        if remaining <= 0:
            raise tts_error("TTS_TIMEOUT")
        check_model(self.directory)
        if self.process is None or self.process.poll() is not None:
            self.close()
            self.process = subprocess.Popen(
                [sys.executable, "-u", "-m", "app.services.tts",
                 str(self.directory), str(os.getpid())],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                encoding="utf-8", env={**os.environ, "HF_HUB_OFFLINE": "1",
                                      "TRANSFORMERS_OFFLINE": "1", "PYTHONUTF8": "1"},
            )
        process = self.process
        result = Queue(maxsize=1)

        def receive():
            try:
                result.put(process.stdout.readline())
            except (OSError, ValueError):
                result.put("")

        Thread(target=receive, daemon=True).start()
        try:
            process.stdin.write(json.dumps({
                "text": text, "voice": voice, "target": str(target),
            }) + "\n")
            process.stdin.flush()
            code = json.loads(result.get(timeout=max(0, deadline - monotonic())))
        except Empty:
            self.close()
            raise tts_error("TTS_TIMEOUT") from None
        except (OSError, ValueError):
            self.close()
            raise tts_error("TTS_INFERENCE_FAILED") from None
        except BaseException:
            self.close()
            raise
        if code != "OK":
            raise tts_error(code if isinstance(code, str) and code in ERRORS else
                            "TTS_INFERENCE_FAILED")


@lru_cache(maxsize=1)
def local_tts(directory):
    client = LocalTTS(directory.resolve())
    atexit.register(client.close)
    return client


def serve(directory):
    # The SDK receives a verified local directory and only bundled preset IDs.
    output = sys.stdout
    model = None
    for line in sys.stdin:
        request = json.loads(line)
        code = "OK"
        with redirect_stdout(sys.stderr):
            if model is None:
                try:
                    from vieneu import Vieneu

                    model = Vieneu(mode="v3nano", onnx_dir=str(directory), threads=2)
                except Exception:
                    code = "TTS_MODEL_LOAD_FAILED"
            if code == "OK":
                voice = None if request["voice"] == "default" else request["voice"]
                try:
                    model.get_preset_voice(voice)
                except ValueError:
                    code = "TTS_VOICE_INVALID"
                if code == "OK":
                    try:
                        audio = model.infer(request["text"], voice=voice)
                        model.save(audio, request["target"])
                    except Exception:
                        code = "TTS_INFERENCE_FAILED"
        print(json.dumps(code), file=output, flush=True)


if __name__ == "__main__":
    if sys.platform == "linux":
        import ctypes
        import signal

        # Exit if Celery kills the parent before it can close the inference subprocess.
        if ctypes.CDLL(None).prctl(1, signal.SIGTERM, 0, 0, 0) or (
            os.getppid() != int(sys.argv[2])
        ):
            sys.exit(1)
    serve(Path(sys.argv[1]))

