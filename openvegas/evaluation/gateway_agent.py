"""Headless Arena decisions through the native wallet gateway, never live wagers.

The study's approved inference ceiling is divided into fixed per-agent grants.
Simulated poker chips cannot create, increase or replenish those grants.
"""
from __future__ import annotations

import json
import threading
import time
from decimal import Decimal

import httpx

from openvegas.gateway.native_pricing import amount

from .host_agent import HostFailure, prompt_for, schema_for


class GatewayStudy:
    def __init__(self, backend, device, *, total_limit_v, per_agent_limit_v, max_agents,
                 max_concurrency=2, transport=None):
        # Same endpoint validation as native integrations. Never accept an
        # alternate URL from observations or a model-generated action.
        from urllib.parse import urlsplit
        url = urlsplit(backend)
        if (url.username or url.password or url.query or url.fragment or url.path
                or not url.hostname or (url.scheme != 'https' and not (
                    url.scheme == 'http' and url.hostname in ('localhost', '127.0.0.1', '::1')))):
            raise ValueError('invalid_gateway_backend')
        self.total, self.per_agent = amount(total_limit_v), amount(per_agent_limit_v)
        if (type(max_agents) is not int or not 1 <= max_agents <= 1000
                or type(max_concurrency) is not int or not 1 <= max_concurrency <= 20
                or not 0 < self.per_agent * max_agents <= self.total):
            raise ValueError('explicit_bounded_inference_limits_required')
        if not isinstance(device, str) or not device.startswith('ov_device_'):
            raise ValueError('scoped_openvegas_device_required')
        self.max_agents, self.device = max_agents, device
        self.synthetic = transport is not None
        self.http = httpx.Client(base_url=backend, transport=transport, timeout=90, follow_redirects=False)
        self.gate, self.lock = threading.BoundedSemaphore(max_concurrency), threading.Lock()
        self.grants, self.stopped = {}, False

    def request(self, method, path, token, data=None):
        try:
            reply = self.http.request(method, '/integrations/native' + path,
                headers={'Authorization': 'Bearer ' + token}, json=data)
            if not reply.is_success:
                raise HostFailure('gateway_rejected_' + str(reply.status_code))
            return reply.json()
        except (httpx.HTTPError, ValueError):
            raise HostFailure('gateway_transport_or_receipt_unavailable') from None

    def grant(self, agent, host, model, output_cap):
        with self.lock:
            if self.stopped:
                raise HostFailure('operator_stopped')
            if agent in self.grants:
                grant = self.grants[agent]
                if grant['blocked']:
                    raise HostFailure('gateway_agent_stopped')
                if (grant['host'], grant['model'], grant['max_tokens']) != (host, model, output_cap):
                    raise HostFailure('agent_route_change_requires_new_study')
                return grant
            if len(self.grants) >= self.max_agents:
                raise HostFailure('study_agent_limit')
            selection = {'host': host, 'model': model, 'provider': 'anthropic' if host == 'claude' else 'openai', 'max_tokens': output_cap}
            # A lost grant response must never allow a new allocation on retry.
            self.grants[agent] = {**selection, 'blocked': True, 'token': None}
            price = self.request('POST', '/quote', self.device, selection)
            if any(price.get(k) != v for k, v in selection.items()):
                raise HostFailure('gateway_quote_mismatch')
            result = self.request('POST', '/sessions', self.device, {**selection,
                'confirmed': True, 'limit_v': str(self.per_agent), 'pricing_fingerprint': price['pricing']['fingerprint']})
            token = result.get('token')
            if not isinstance(token, str) or not token.startswith('ov_native_'):
                raise HostFailure('gateway_grant_invalid')
            grant = {**selection, 'token': token, 'session_id': result.get('session_id'), 'used': Decimal(0), 'blocked': False}
            # Reserve the entire agent allocation even when the response is bad;
            # uncertainty must not open more sessions or replenish a spent cap.
            self.grants[agent] = grant
            if amount(result.get('budget_v')) != self.per_agent or result.get('pricing') != price['pricing']:
                grant['blocked'] = True
                raise HostFailure('gateway_grant_mismatch')
            return grant

    def stop(self):
        with self.lock:
            self.stopped = True

    def close(self):
        self.stop()
        for grant in self.grants.values():
            if grant['token'] is None:
                continue
            try:
                self.request('DELETE', '/session', grant['token'])
            except HostFailure:
                grant['blocked'] = True  # Server expiry/ceilings still apply.
        self.http.close()


class GatewayAgent:
    def __init__(self, study, host, model, *, output_cap=256):
        if host not in ('claude', 'codex') or type(output_cap) is not int or not 1 <= output_cap <= 2048:
            raise ValueError('invalid_gateway_agent')
        self.study, self.host, self.model, self.output_cap = study, host, model, output_cap

    def decide(self, agent_id, observation):
        with self.study.gate:
            grant = self.study.grant(agent_id, self.host, self.model, self.output_cap)
            if grant['blocked'] or self.study.stopped:
                raise HostFailure('gateway_agent_stopped')
            prompt, schema = prompt_for(agent_id, observation), schema_for(observation)
            if self.host == 'claude':
                protocol = 'messages'
                body = {'model': self.model, 'max_tokens': self.output_cap, 'stream': False,
                        'messages': [{'role': 'user', 'content': prompt}],
                        'output_config': {'format': {'type': 'json_schema', 'schema': schema}}}
            else:
                protocol = 'responses'
                body = {'model': self.model, 'max_output_tokens': self.output_cap, 'stream': False,
                        'input': prompt, 'store': False,
                        'text': {'format': {'type': 'json_schema', 'name': 'arena_action', 'strict': True, 'schema': schema}}}
            started = time.perf_counter()
            try:
                response = self.study.request('POST', '/v1/' + protocol, grant['token'], body)
                if response.get('model') != self.model:
                    raise HostFailure('gateway_model_identity_mismatch')
                if protocol == 'messages':
                    text = ''.join(b['text'] for b in response['content'] if b.get('type') == 'text')
                    if response.get('stop_reason') != 'end_turn':
                        raise HostFailure('gateway_incomplete_action')
                else:
                    text = ''.join(c['text'] for b in response['output'] if b.get('type') == 'message'
                                   for c in b.get('content', []) if c.get('type') == 'output_text')
                    if response.get('status') != 'completed':
                        raise HostFailure('gateway_incomplete_action')
                action = json.loads(text)['action']
                if (type(action) is not dict or set(action) != {'type', 'amount_to'}
                        or action['type'] not in observation['legal_actions']
                        or ((action['type'] == 'raise') != (type(action['amount_to']) is int))
                        or action['type'] != 'raise' and action['amount_to'] is not None):
                    raise HostFailure('invalid_structured_output')
                status = self.study.request('GET', '/session', grant['token'])
                used = amount(status['used_or_reserved_v'])
                if status.get('session_id') != grant['session_id'] or not grant['used'] <= used <= self.study.per_agent:
                    raise HostFailure('gateway_receipt_mismatch')
                debit = used - grant['used']
                grant['used'] = used
                return action, {'billing': 'admin_sponsored_openvegas_gateway', 'provider': grant['provider'],
                    'requested_model': self.model, 'observed_model': response['model'], 'inference_debit_v': str(debit),
                    'agent_ceiling_v': str(self.study.per_agent), 'study_ceiling_v': str(self.study.total),
                    'simulated_wager_chips_fund_inference': False, 'provider_request_id': response.get('id'),
                    'input_tokens': response.get('usage', {}).get('input_tokens'),
                    'output_tokens': response.get('usage', {}).get('output_tokens'),
                    'latency_ms': round((time.perf_counter() - started) * 1000)}
            except HostFailure:
                grant['blocked'] = True
                raise
            except (ValueError, KeyError, TypeError):
                grant['blocked'] = True
                raise HostFailure('invalid_gateway_action_or_receipt') from None
