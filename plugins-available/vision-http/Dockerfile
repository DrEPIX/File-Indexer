# syntax=docker/dockerfile:1.7
FROM plugin_base

ARG TORCH_INDEX_URL=https://download.pytorch.org/whl/cpu
ARG TORCH_VERSION=2.2.2
ARG TORCHVISION_VERSION=0.17.2
USER root
COPY plugins-available/vision-http/requirements.txt /tmp/requirements.txt
RUN python -m pip install --no-cache-dir --index-url "${TORCH_INDEX_URL}" \
        "torch==${TORCH_VERSION}" "torchvision==${TORCHVISION_VERSION}" \
    && python -m pip install --no-cache-dir -r /tmp/requirements.txt \
    && rm -f /tmp/requirements.txt

WORKDIR /app
COPY --chown=analyzer:analyzer plugins-available/vision-http/vision_model.py /app/vision_model.py
COPY --chown=analyzer:analyzer plugins-available/vision-http/vision_server.py /app/vision_server.py
USER analyzer

ENV HF_HOME=/models/huggingface \
    TORCH_HOME=/models/torch \
    VISION_DEVICE=auto
VOLUME ["/models"]
EXPOSE 9200
HEALTHCHECK --interval=20s --timeout=5s --start-period=180s --retries=12 \
    CMD if [ -n "$VISION_AUTH_TOKEN" ]; then curl --fail --silent --header "Authorization: Bearer $VISION_AUTH_TOKEN" http://127.0.0.1:9200/health; else curl --fail --silent http://127.0.0.1:9200/health; fi
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["python", "-m", "uvicorn", "vision_server:app", "--host", "0.0.0.0", "--port", "9200"]
