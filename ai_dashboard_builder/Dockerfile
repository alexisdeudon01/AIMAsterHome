ARG BUILD_FROM
FROM ${BUILD_FROM}

# Install Python 3 + venv tooling (avoid PEP 668 externally-managed-environment)
RUN apk add --no-cache python3 py3-pip py3-virtualenv

WORKDIR /app
COPY requirements.txt /app/requirements.txt

# Install Python deps into a virtual environment
RUN python3 -m venv /venv \
  && /venv/bin/pip install --no-cache-dir --upgrade pip \
  && /venv/bin/pip install --no-cache-dir -r /app/requirements.txt

# Make venv python/pip the default
ENV PATH="/venv/bin:${PATH}"

COPY run.sh /run.sh
COPY app /app
RUN chmod a+x /run.sh

EXPOSE 8099

CMD ["/run.sh"]