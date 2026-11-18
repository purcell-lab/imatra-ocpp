# SPDX-License-Identifier: Apache-2.0
FROM python:3.12-slim-trixie@sha256:5e2bdb1b0d542d5039cffb8181c65930093949194a65c365dfb1b0fe13a4fa5b
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /srv
COPY pyproject.toml README.md LICENSE NOTICE ./
COPY src ./src
RUN pip install --no-cache-dir . && useradd -r -d /nonexistent -s /usr/sbin/nologin bridge
USER bridge
ENTRYPOINT ["python", "-m", "imatra_ocpp"]
CMD ["/data/options.json"]
