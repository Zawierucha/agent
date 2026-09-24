FROM nousresearch/hermes-agent:latest

USER root
 
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

COPY pyproject.toml uv.lock /tmp/hermes-work/

WORKDIR /tmp/hermes-work

RUN uv export \
    --locked \
    --no-dev \
    --format requirements-txt \
    --output-file /tmp/hermes-requirements.txt

RUN uv pip install \
    --python /opt/hermes/.venv/bin/python \
    --requirement /tmp/hermes-requirements.txt

RUN rm -rf /tmp/hermes-work /tmp/hermes-requirements.txt

WORKDIR /opt/hermes

USER hermes