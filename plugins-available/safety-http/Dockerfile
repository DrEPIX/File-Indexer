# syntax=docker/dockerfile:1.7
FROM plugin_base

ARG TORCH_INDEX_URL=https://download.pytorch.org/whl/cpu
USER root
COPY plugins-available/safety-http/requirements.txt /tmp/requirements.txt
RUN python -m pip install --no-cache-dir --index-url "${TORCH_INDEX_URL}" torch torchvision \
    && python -m pip install --no-cache-dir -r /tmp/requirements.txt \
    && rm -f /tmp/requirements.txt

WORKDIR /app
COPY --chown=analyzer:analyzer plugins-available/safety-http/safety_model.py /app/safety_model.py
COPY --chown=analyzer:analyzer plugins-available/safety-http/safety_server.py /app/safety_server.py
USER analyzer

ENV HF_HOME=/models/huggingface \
    TORCH_HOME=/models/torch \
    SAFETY_DEVICE=auto
VOLUME ["/models"]
EXPOSE 9300
HEALTHCHECK --interval=20s --timeout=5s --start-period=180s --retries=12 \
    CMD if [ -n "$SAFETY_AUTH_TOKEN" ]; then curl --fail --silent --header "Authorization: Bearer $SAFETY_AUTH_TOKEN" http://127.0.0.1:9300/health; else curl --fail --silent http://127.0.0.1:9300/health; fi
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["python", "-m", "uvicorn", "safety_server:app", "--host", "0.0.0.0", "--port", "9300"]
