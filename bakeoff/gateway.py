"""AssemblyAI LLM Gateway: request building and the HTTP call.

Docs (read 2026-11-11): https://assemblyai.com/docs/api-reference/llm-gateway/create-chat-completion
  - POST https://llm-gateway.assemblyai.com/v1/chat/completions
  - "Authorization: <key>" — the key itself, "not prefixed with Bearer"
  - max_tokens "Default is 1000", so it is always sent: MAX_TOKENS (8192)
    unless models.yaml sets max_tokens for the model (reasoning tokens count
    toward it; a full note with reasoning can exceed 1000 and be cut off)
  - response_format: the only documented type is json_schema ("Use
    `json_schema` for structured outputs"); json_object is not documented
    for any model. See OUTPUT_MODES.
  - model_region: omitted on purpose ("omit this field for default in-region
    processing")
  - usage: input_tokens / output_tokens / total_tokens; body field request_id
  - 429 carries Retry-After; 5xx are transient
"""
import hashlib
import random
import time

import requests

GATEWAY_URL = 'https://llm-gateway.assemblyai.com/v1/chat/completions'
MAX_TOKENS = 8192          # default output cap; models.yaml max_tokens sets it per model
TEMPERATURE = 0.1          # production's GenerateContentConfig(temperature=0.1)
TIMEOUT_S = 120            # default read timeout; models.yaml timeout_s sets it per model
MAX_RETRIES = 3            # retries after the first attempt, for 429 and 5xx only
SCHEMA_NAME = 'nota_soap'

# How the JSON output is requested (models.yaml `output_mode`):
#   none        — no response_format; the prompt's JSON template only. Closest
#                 to production, which sends Gemini response_mime_type JSON and
#                 no schema; production's clean_json_response strips fences.
#   json_object — response_format {"type": "json_object"} (JSON mode), for a
#                 model whose Gateway docs say it supports it.
#   json_schema — response_format json_schema with extraction_core.wire_schema
#                 (strict false). Kept for later; no bake-off model uses it.
OUTPUT_MODES = ('none', 'json_object', 'json_schema')


def build_request(spec, prompt: str, schema: dict | None = None) -> dict:
    """The JSON body. response_format follows spec.output_mode (`schema` is
    needed only for json_schema); temperature only for models that accept it
    (not Claude Haiku/Sonnet 5.5); reasoning_effort only for a variant that
    sets it."""
    body = {
        'model': spec.model,
        'messages': [{'role': 'user', 'content': prompt}],
        'max_tokens': spec.max_tokens,
    }
    if spec.output_mode == 'json_object':
        body['response_format'] = {'type': 'json_object'}
    elif spec.output_mode == 'json_schema':
        if schema is None:
            raise ValueError(f'{spec.name}: output_mode json_schema needs a schema')
        body['response_format'] = {
            'type': 'json_schema',
            'json_schema': {'name': SCHEMA_NAME, 'schema': schema, 'strict': False},
        }
    elif spec.output_mode != 'none':
        raise ValueError(f'{spec.name}: unknown output_mode {spec.output_mode!r}')
    if spec.accepts_temperature:
        body['temperature'] = TEMPERATURE
    if spec.reasoning_effort:
        body['reasoning_effort'] = spec.reasoning_effort
    assert 'model_region' not in body
    return body


def request_params(body: dict) -> dict:
    """What was sent, minus the prompt text itself (stored as a hash + size):
    the run files record parameters, not 9 KB of repeated prompt."""
    prompt = body['messages'][0]['content']
    params = {k: v for k, v in body.items() if k != 'messages'}
    rf = body.get('response_format')
    if rf and rf['type'] == 'json_schema':     # the schema itself isn't repeated in every run file
        params['response_format'] = {'type': 'json_schema',
                                     'json_schema': {'name': rf['json_schema']['name'],
                                                     'strict': rf['json_schema']['strict']}}
    params['prompt_sha256'] = hashlib.sha256(prompt.encode('utf-8')).hexdigest()
    params['prompt_chars'] = len(prompt)
    params['messages'] = [{'role': 'user', 'content': '<production prompt; see prompt_sha256>'}]
    return params


def _retry_wait(response, attempt: int) -> float:
    retry_after = response.headers.get('Retry-After') if response is not None else None
    if retry_after:
        try:
            return max(float(retry_after), 1.0)
        except ValueError:
            pass
    return 2 ** attempt * 2 + random.uniform(0, 1)       # 2-3 s, 4-5 s, 8-9 s


def call_gateway(body: dict, api_key: str, post=None, sleep=time.sleep, clock=time.monotonic,
                 timeout: float = TIMEOUT_S) -> dict:
    """POST with retries on 429/5xx (up to MAX_RETRIES), `timeout` seconds per attempt.
    Timeouts and connection errors are recorded, not retried — a stuck 120 s
    call repeated 3 times would just burn time. Never raises."""
    post = post or requests.post
    headers = {'Authorization': api_key, 'Content-Type': 'application/json'}
    attempts, started = 0, clock()
    response, error = None, None
    for attempt in range(MAX_RETRIES + 1):
        attempts += 1
        try:
            response = post(GATEWAY_URL, json=body, headers=headers, timeout=timeout)
        except requests.RequestException as e:
            response, error = None, f'{type(e).__name__}'
            break
        if response.status_code == 429 or response.status_code >= 500:
            if attempt < MAX_RETRIES:
                sleep(_retry_wait(response, attempt))
                continue
        break
    latency = clock() - started
    result = {'attempts': attempts, 'latency_s': round(latency, 3), 'http_status': None,
              'body': None, 'raw_body': None, 'error': error}
    if response is not None:
        result['http_status'] = response.status_code
        result['raw_body'] = response.text
        try:
            result['body'] = response.json()
        except ValueError:
            result['body'] = None
        if response.status_code != 200 and not error:
            result['error'] = f'http_{response.status_code}'
    return result


def content_of(body) -> str | None:
    try:
        return body['choices'][0]['message']['content']
    except (KeyError, IndexError, TypeError):
        return None


def usage_of(body) -> dict:
    """Gateway usage: input_tokens / output_tokens (OpenAI-style names accepted
    as a fallback), plus reasoning_tokens when the response reports them.

    The API reference documents only input/output/total_tokens, but the
    2026-11-11 smoke-test responses also carried the OpenAI-style
    completion_tokens_details.reasoning_tokens (Haiku 5.5: 2,695 of 3,359
    output tokens; Gemini 2.5 Flash: 1,738 of 2,272; Flash-Lite: 0) — i.e.
    reasoning is INCLUDED in output_tokens, which is what is billed. None
    when absent (not documented, so never assumed to be 0)."""
    usage = (body or {}).get('usage') or {}
    inp = usage.get('input_tokens', usage.get('prompt_tokens'))
    out = usage.get('output_tokens', usage.get('completion_tokens'))
    reasoning = None
    for details in ('completion_tokens_details', 'output_tokens_details'):
        value = (usage.get(details) or {}).get('reasoning_tokens') if isinstance(usage.get(details), dict) else None
        if value is not None:
            reasoning = value
            break
    return {'input_tokens': inp, 'output_tokens': out, 'reasoning_tokens': reasoning,
            'total_tokens': usage.get('total_tokens'), 'raw': usage}
