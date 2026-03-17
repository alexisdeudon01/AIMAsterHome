ARG BUILD_FROM
FROM ${BUILD_FROM}

WORKDIR /app
COPY requirements.txt /app/requirements.txt
RUN apk add --no-cache git && pip install --no-cache-dir -r /app/requirements.txt

COPY run.sh /run.sh
COPY app /app
RUN chmod +x /run.sh

CMD ["/run.sh"]
