# backend/llm_gateway.py
"""#81: SOAP extraction through the AssemblyAI LLM Gateway (LLM_BACKEND=gateway).

Ported from the #83 bake-off harness (bakeoff/gateway.py), not imported
from it — production never depends on bakeoff/. Same request the bake-off
ran (claude-haiku-5-5 at reasoning_effort low was its choice):

  POST https://llm-gateway.assemblyai.com/v1/chat/completions
  Authorization: <ASSEMBLYAI_API_KEY>       (the key itself, no "Bearer")
  {"model": "claude-haiku-5-5", "messages": [{"role": "user", "content": <prompt>}],
   "max_tokens": 8192, "reasoning_effort": "low"}

Docs (create-chat-completion, read 2026-11-11): max_tokens "Default is
1000" — a full note can exceed it, so 8192 is always sent; model_region is
omitted for default in-region processing (never sent); 429 carries
Retry-After and 5xx are transient. No temperature (claude-haiku-5-5 doesn't
list it among its supported parameters) and no response_format: like the
Gemini path, the prompt's JSON template is the only format instruction, and
extraction_core.clean_json_response strips any code fence.

Never logs the prompt, the output or an error body (it can echo the
prompt): one line per HTTP attempt with ids, token counts and timing only.
"""
import json
import random
import time

import requests

from extraction_core import clean_json_response
from logger import logger

GATEWAY_URL = 'https://llm-gateway.assemblyai.com/v1/chat/completions'
MODEL = 'claude-haiku-5-5'
REASONING_EFFORT = 'low'
MAX_TOKENS = 8192
TIMEOUT_S = 120
MAX_RETRIES = 2                 # after the first attempt, for 429 and 5xx only
MAX_RETRY_WAIT_S = 30           # cap on a server-sent Retry-After
TRUNCATED = frozenset({'max_tokens', 'length'})


class GatewayError(Exception):
    """The Gateway gave no usable note. The message carries only a reason
    code and numbers — never prompt or output text — because callers log it."""


def build_request(prompt: str) -> dict:
    return {
        'model': MODEL,
        'messages': [{'role': 'user', 'content': prompt}],
        'max_tokens': MAX_TOKENS,
        'reasoning_effort': REASONING_EFFORT,
    }


def usage_of(body) -> dict:
    """input/output tokens (OpenAI-style names as a fallback) and reasoning
    tokens when reported (undocumented, seen in the #83 smoke test inside
    completion_tokens_details; included in output_tokens). None when absent."""
    usage = (body or {}).get('usage') if isinstance(body, dict) else None
    usage = usage if isinstance(usage, dict) else {}
    reasoning = None
    for details in ('completion_tokens_details', 'output_tokens_details'):
        d = usage.get(details)
        if isinstance(d, dict) and d.get('reasoning_tokens') is not None:
            reasoning = d['reasoning_tokens']
            break
    return {'in': usage.get('input_tokens', usage.get('prompt_tokens')),
            'out': usage.get('output_tokens', usage.get('completion_tokens')),
            'reasoning': reasoning}


def _choice(body):
    try:
        return body['choices'][0]
    except (KeyError, IndexError, TypeError):
        return None


def _retry_wait(response, attempt: int) -> float:
    retry_after = response.headers.get('Retry-After') if response is not None else None
    if retry_after:
        try:
            return min(max(float(retry_after), 1.0), MAX_RETRY_WAIT_S)
        except ValueError:
            pass
    return 2 ** attempt * 2 + random.uniform(0, 1)       # 2-3 s, then 4-5 s


def _log_call(status, body, latency_s, finish=None):
    u = usage_of(body)
    request_id = body.get('request_id') if isinstance(body, dict) else None

    def v(x):
        return '-' if x is None else x
    logger.info(f"LLM: backend=gateway model={MODEL} request_id={v(request_id)} in={v(u['in'])} "
                f"out={v(u['out'])} reasoning={v(u['reasoning'])} latency={latency_s:.2f}s "
                f"status={v(status)} finish={v(finish)}")


def call(prompt: str, api_key: str, post=None, sleep=None, clock=None) -> dict:
    """One extraction: the Gateway's parsed JSON note. Retries 429/5xx up to
    MAX_RETRIES times with backoff; a timeout or connection error is not
    retried (a stuck 120 s call repeated would hold the job for minutes).
    Raises GatewayError for anything that isn't a complete JSON object."""
    if not api_key:
        raise GatewayError('no ASSEMBLYAI_API_KEY')
    post = post or requests.post
    sleep = sleep or time.sleep          # looked up at call time (tests replace it)
    clock = clock or time.monotonic
    body = build_request(prompt)
    headers = {'Authorization': api_key, 'Content-Type': 'application/json'}
    response = None
    for attempt in range(MAX_RETRIES + 1):
        started = clock()
        try:
            response = post(GATEWAY_URL, json=body, headers=headers, timeout=TIMEOUT_S)
        except requests.RequestException as e:
            _log_call(None, None, clock() - started)
            raise GatewayError(type(e).__name__) from None
        try:
            payload = response.json()
        except ValueError:
            payload = None
        choice = _choice(payload)
        finish = choice.get('finish_reason') if isinstance(choice, dict) else None
        _log_call(response.status_code, payload, clock() - started, finish)
        if (response.status_code == 429 or response.status_code >= 500) and attempt < MAX_RETRIES:
            sleep(_retry_wait(response, attempt))
            continue
        break

    if response.status_code != 200:
        raise GatewayError(f'http_{response.status_code}')
    if finish in TRUNCATED:
        # Cut off at max_tokens: the JSON is incomplete — a failure, never
        # a partial note.
        raise GatewayError(f'truncated ({finish})')
    content = (choice or {}).get('message', {}).get('content') if isinstance(choice, dict) else None
    if not isinstance(content, str) or not content.strip():
        raise GatewayError('empty response')
    try:
        data = json.loads(clean_json_response(content))
    except ValueError as e:
        raise GatewayError(f'invalid JSON ({type(e).__name__})') from None
    if not isinstance(data, dict):
        raise GatewayError('JSON is not an object')
    return data
