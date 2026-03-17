ARG BUILD_FROM
FROM $BUILD_FROM

# Install system dependencies
RUN apk add --no-cache bash git

# Copy add-on files
COPY requirements.txt /tmp/requirements.txt
RUN pip3 install --no-cache-dir -r /tmp/requirements.txt

COPY run.sh /run.sh
RUN chmod +x /run.sh

COPY app/ /app/

CMD ["/run.sh"]
