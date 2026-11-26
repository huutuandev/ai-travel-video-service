import argparse
import hashlib
import os
from pathlib import Path
from tempfile import NamedTemporaryFile
from urllib.request import urlopen

from app.services.tts import MODEL_ID, MODEL_REVISION

BUNDLE_BYTES = 281756486
CHECKSUMS = {
    "codec_decoder.onnx": "b0ab15e7828a39d53679e25b1ba4ba415a61311307202a6323130b9e1cc3029d",
    "config.json": "3762f1716fd8d7a1451ddf2e051c03b0a341e8adda4f1dbd0db0ae9777514e56",
    "constants.npz": "7c011938effe41687a9af85107a31a040dfcf0fb3d0f048ec10f4fb65c1a8829",
    "duration_predictor.onnx": "20fd7fa60006d0a48ee82e0451b3f920d2052588c083c756cce669f3947c0a68",
    "text_encoder.onnx": "204f02cccae1f16ccb2d3840f05721a37fe250b82cbd456337a0ccb49615e4bf",
    "vector_estimator.onnx": "c6c1d4398ca35d3ad1bd3f0459413d1b975d09e7f2f3a2b4493a7b92bbf6ce93",
}


def verified(path, checksum):
    if not path.is_file():
        return False
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest() == checksum


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("directory", nargs="?", type=Path, default=Path("models/tts"))
    parser.add_argument("--check", action="store_true", help="Verify locally without downloading")
    args = parser.parse_args()
    directory = args.directory
    source = f"https://huggingface.co/{MODEL_ID}"
    print(f"{MODEL_ID}@{MODEL_REVISION} — {BUNDLE_BYTES:,} bytes")
    print(f"Apache-2.0 for weights and preset voices: {source}/blob/{MODEL_REVISION}/README.md")
    invalid = [
        (name, sha) for name, sha in CHECKSUMS.items() if not verified(directory / name, sha)
    ]
    if not invalid:
        print(f"All {len(CHECKSUMS)} files verified in {directory}")
        return
    if args.check:
        raise SystemExit("Missing or invalid files: " + ", ".join(name for name, _ in invalid))
    directory.mkdir(parents=True, exist_ok=True)
    for name, checksum in invalid:
        target = directory / name
        url = f"{source}/resolve/{MODEL_REVISION}/{name}"
        print(f"Downloading and verifying {name}", flush=True)
        with NamedTemporaryFile(dir=directory, delete=False) as stream:
            staged = Path(stream.name)
            try:
                with urlopen(url, timeout=60) as response:
                    for chunk in iter(lambda: response.read(1024 * 1024), b""):
                        stream.write(chunk)
                stream.close()
                if not verified(staged, checksum):
                    raise RuntimeError(f"SHA-256 mismatch: {name}")
                staged.chmod(0o644)
                os.replace(staged, target)
            finally:
                stream.close()
                staged.unlink(missing_ok=True)
    print(f"All {len(CHECKSUMS)} files verified in {directory}")


if __name__ == "__main__":
    main()


