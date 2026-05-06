FROM python:3.14-alpine AS builder

COPY requirements.txt .
RUN pip --no-cache-dir install -r ./requirements.txt

FROM python:3.14-alpine AS minifier

RUN pip install --no-cache-dir python-minifier
WORKDIR /m
COPY dga.py .
RUN pyminify --in-place \
             --remove-literal-statements \
             --rename-globals \
             --remove-asserts \
             --remove-debug \
             --prefer-single-line \
             .

FROM python:3.14-alpine

WORKDIR /dga
RUN addgroup -g 1000 -S dga && adduser -u 1000 -S dga -G dga
ENV MAGICK_HOME=/usr
RUN apk add --no-cache imagemagick-dev ffmpeg
COPY --from=builder /usr/local/lib/python3.14/site-packages /usr/local/lib/python3.14/site-packages
COPY --chown=dga:dga --from=minifier /m/dga.py .
USER dga
ENV PYTHONDONTWRITEBYTECODE=1
CMD ["python3", "/dga/dga.py", "--config", "/dga/config.json"]