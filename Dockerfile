FROM python:3.13-slim
WORKDIR /app
# git: job/worker.py shells out to `git clone`/`commit`/`push` (job/worker.py)
# to work with a PR's head branch. python:3.13-slim has no VCS tooling at
# all -- verified live: the job raised
# `FileNotFoundError: [Errno 2] No such file or directory: 'git'` before this
# line was added.
RUN apt-get update && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/*
COPY pyproject.toml ./
RUN pip install --no-cache-dir uv && uv pip install --system -e .
# The `playwright` Python package (installed above) is only the driver; the
# browser binary itself is a separate download that ships with none of this.
# app/scanner.py launches Chromium via playwright.chromium.launch() on both
# the service (self-scan) and worker (every remediation run) code paths.
RUN playwright install --with-deps chromium
COPY . .
ENV PORT=8080
# job/worker.py runs as `python job/worker.py ...` (Cloud Run Jobs --command),
# not `python -m` or `uvicorn app.main:app` (the service's own CMD below).
# Running a script file by path sets sys.path[0] to *that script's own
# directory* (/app/job) rather than the working directory -- so `from app...`
# in job/worker.py cannot find the `app` package by cwd-relative resolution
# the way the service's invocation style happens to. Verified live: the job
# raised `ModuleNotFoundError: No module named 'app'` before this line was
# added; setting PYTHONPATH explicitly removes the dependence on which
# invocation style is in use.
ENV PYTHONPATH=/app
CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT}"]
