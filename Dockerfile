FROM python:3.12-slim AS base
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /service
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg fonts-dejavu-core \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --uid 10001 travelvideo \
    && install -d -o travelvideo -g travelvideo /data/media
COPY pyproject.toml ./

FROM base AS runtime-dependencies
RUN python -c "import subprocess,sys,tomllib; p=tomllib.load(open('pyproject.toml','rb'))['project']; subprocess.check_call([sys.executable,'-m','pip','install','--no-cache-dir',*p['dependencies']])"

FROM runtime-dependencies AS test
RUN python -c "import subprocess,sys,tomllib; p=tomllib.load(open('pyproject.toml','rb'))['project']; subprocess.check_call([sys.executable,'-m','pip','install','--no-cache-dir',*p['optional-dependencies']['test']])"
ENV RUFF_CACHE_DIR=/tmp/ruff_cache
COPY app ./app
COPY alembic.ini ./
COPY migrations ./migrations
COPY tests ./tests
USER travelvideo
CMD ["python", "-m", "pytest", "-q", "-p", "no:cacheprovider"]

FROM runtime-dependencies AS tts-worker
RUN python -c "import subprocess,sys,tomllib; p=tomllib.load(open('pyproject.toml','rb'))['project']; subprocess.check_call([sys.executable,'-m','pip','install','--no-cache-dir',*p['dependencies'],*p['optional-dependencies']['tts']])" && pip check
RUN python -m pip install --no-cache-dir pip==26.3 && pip check
ENV HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1
COPY app ./app
USER travelvideo

FROM runtime-dependencies AS runtime
RUN python -m pip install --no-cache-dir pip==26.3 && pip check
COPY app ./app
COPY alembic.ini ./
COPY migrations ./migrations
USER travelvideo
CMD ["uvicorn", "app.main:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000"]

