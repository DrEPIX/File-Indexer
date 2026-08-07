# CLIP HTTP Analyzer

This standalone service implements `mediaengine.analyzer/1` and emits native
`openai/clip-vit-base-patch32` image embeddings. It uses Hugging Face
`transformers` because that model has a stable 512-dimensional projection and a
single processor for both image and zero-shot text inputs.

Outputs:

- `clip=embedding`: one asset vector for an image;
- for video, up to `config.max_keyframes` frame-region vectors plus one
  normalized mean-pooled asset vector;
- optional `clip.tag` labels with real softmax confidences from
  `config.prompts`.

Default prompts are indoor, outdoor, document, screenshot, portrait, and
landscape. Set `prompts = []` to disable labels and `tag_threshold` to control
emission. The model's native vector is never padded or truncated.

## Model cache and network

Weights are not in the image. The first health/analyze access downloads them to
`HF_HOME=/models/huggingface`; `/models` is a named Compose volume. The manifest
therefore declares `requires.network = true`. A fully offline deployment can
pre-populate the volume and then deny egress.

`GET /health` triggers lazy background loading and returns 200 with `loading`
until ready. `POST /analyze` returns 503 plus `Retry-After` during that window.

## Run without the engine

Install test/runtime dependencies in an isolated environment (PyTorch and model
weights are not needed by the fake-model contract tests):

```powershell
python -m venv .venv
.venv\Scripts\python -m pip install -r requirements.txt httpx
.venv\Scripts\python -m unittest -v test_contract.py
```

For a real local service, install PyTorch for the desired device, then:

```powershell
python -m uvicorn server:app --app-dir plugins-available/clip-http --host 127.0.0.1 --port 9100
```

For Compose CPU/GPU commands, model volumes, and mount guarantees, see
`docker/README.md`. The Compose manifest URL is `http://clip:9100`. If the
engine runs directly on the host, copy `plugin.toml` and change `base_url` to
`http://127.0.0.1:9100`.

Set the same optional token in `CLIP_AUTH_TOKEN` and `[plugin.http].auth_token`.
When configured, every endpoint requires `Authorization: Bearer <token>`.

