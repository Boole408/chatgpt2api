ARG APP_IMAGE=python:3.13-slim
FROM ${APP_IMAGE}

RUN apt-get update && apt-get install -y --no-install-recommends \
    xvfb x11vnc novnc websockify openbox fonts-dejavu-core \
    && rm -rf /var/lib/apt/lists/*

COPY entrypoint.sh /usr/local/bin/registration-desktop
RUN chmod 755 /usr/local/bin/registration-desktop

ENV DISPLAY=:99
EXPOSE 6080
ENTRYPOINT ["/usr/local/bin/registration-desktop"]
