"""AssemblyAI LLM Gateway: request building and the HTTP call.

Docs (read 2026-11-11): https://assemblyai.com/docs/api-reference/llm-gateway/create-chat-completion
  - POST https://llm-gateway.assemblyai.com/v1/chat/completions
  - "Authorization: <key>" — the key itself, "not prefixed with Bearer"
  - max_tokens "Default is 1000", so 8192 is always sent (a full note with
    reasoning can exceed 1000 and would be cut off)
  - response_format json_schema; `strict` defaults to false
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
MAX_TOKENS = 8192
TEMPERATURE = 0.1          # production's GenerateContentConfig(temperature=0.1)
TIMEOUT_S = 120
MAX_RETRIES = 3            # retries after the first attempt, for 429 and 5xx only
SCHEMA_NAME = 'nota_soap'


def build_request(spec, prompt: str, schema: dict) -> dict:
    """The JSON body. temperature only for models that accept it (not Claude
    Haiku/Sonnet 5.5); reasoning_effort only for a variant that sets it."""
    body = {
        'model': spec.model,
        'messages': [{'role': 'user', 'content': prompt}],
        'max_tokens': MAX_TOKENS,
        'response_format': {
            'type': 'json_schema',
            'json_schema': {'name': SCHEMA_NAME, 'schema': schema, 'strict': False},
        },
    }
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
    params['response_format'] = {
        'type': body['response_format']['type'],
        'json_schema': {'name': body['response_format']['json_schema']['name'],
                        'strict': body['response_format']['json_schema']['strict']},
    }
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


def call_gateway(body: dict, api_key: str, post=None, sleep=time.sleep, clock=time.monotonic) -> dict:
    """POST with retries on 429/5xx (up to MAX_RETRIES), timeout TIMEOUT_S.
    Timeouts and connection errors are recorded, not retried — a stuck 120 s
    call repeated 3 times would just burn time. Never raises."""
    post = post or requests.post
    headers = {'Authorization': api_key, 'Content-Type': 'application/json'}
    attempts, started = 0, clock()
    response, error = None, None
    for attempt in range(MAX_RETRIES + 1):
        attempts += 1
        try:
            response = post(GATEWAY_URL, json=body, headers=headers, timeout=TIMEOUT_S)
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
    as a fallback)."""
    usage = (body or {}).get('usage') or {}
    inp = usage.get('input_tokens', usage.get('prompt_tokens'))
    out = usage.get('output_tokens', usage.get('completion_tokens'))
    return {'input_tokens': inp, 'output_tokens': out, 'total_tokens': usage.get('total_tokens'), 'raw': usage}
