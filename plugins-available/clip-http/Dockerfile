# syntax=docker/dockerfile:1.7
FROM plugin_base

ARG TORCH_INDEX_URL=https://download.pytorch.org/whl/cpu
USER root
COPY plugins-available/clip-http/requirements.txt /tmp/requirements.txt
RUN python -m pip install --no-cache-dir --index-url "${TORCH_INDEX_URL}" torch \
    && python -m pip install --no-cache-dir -r /tmp/requirements.txt \
    && rm -f /tmp/requirements.txt

WORKDIR /app
COPY --chown=analyzer:analyzer plugins-available/clip-http/model.py /app/model.py
COPY --chown=analyzer:analyzer plugins-available/clip-http/server.py /app/server.py
USER analyzer

ENV HF_HOME=/models/huggingface \
    TORCH_HOME=/models/torch \
    CLIP_DEVICE=auto
VOLUME ["/models"]
EXPOSE 9100
HEALTHCHECK --interval=15s --timeout=5s --start-period=90s --retries=10 \
    CMD if [ -n "$CLIP_AUTH_TOKEN" ]; then curl --fail --silent --header "Authorization: Bearer $CLIP_AUTH_TOKEN" http://127.0.0.1:9100/health; else curl --fail --silent http://127.0.0.1:9100/health; fi
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["python", "-m", "uvicorn", "server:app", "--host", "0.0.0.0", "--port", "9100"]
