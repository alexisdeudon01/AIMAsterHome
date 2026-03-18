ARG BUILD_FROM
FROM ${BUILD_FROM}

# Install Python 3 and pip (Alpine-based HA base images)
RUN apk add --no-cache python3 py3-pip

WORKDIR /app
COPY requirements.txt /app/requirements.txt
RUN pip3 install --no-cache-dir -r /app/requirements.txt

COPY run.sh /run.sh
COPY app /app
RUN chmod a+x /run.sh

EXPOSE 8099

CMD ["/run.sh"]
