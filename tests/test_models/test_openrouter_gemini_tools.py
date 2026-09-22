"""Gemini's native schemas remain typed and pass through local permissions."""
import json

import pytest

from openvegas.gateway import openrouter
from openvegas.gateway.inference import AIGateway
from tests.test_models.test_openrouter import capabilities, catalog_row, request, response

MODEL = 'google/gemini-2.5-flash-lite'


def req():
    return request(model=MODEL, enable_tools=True)


def reply(name='Read', args=None):
    body = response()
    body['model'] = MODEL
    body['choices'][0] = {'finish_reason': 'tool_calls', 'message': {
        'role': 'assistant', 'content': None,
        'tool_calls': [{'id': 'call-native-read', 'type': 'function', 'function': {
            'name': name, 'arguments': json.dumps({'path': 'smoke-fixture.txt'} if args is None else args),
        }}],
    }}
    return body


def parse(body):
    return openrouter.parse_response(body, req(), catalog_row(), AIGateway._parse_local_tool_call)


def test_gemini_advertises_seven_flat_native_tools_with_required_parameters():
    payload = openrouter.build_payload(req(), catalog_row(), capabilities())
    definitions = {x['function']['name']: x['function']['parameters'] for x in payload['tools']}
    assert set(definitions) == {'Read', 'Search', 'Write', 'FindAndReplace', 'InsertAtEnd', 'Bash', 'List'}
    assert definitions['Read']['required'] == ['path']
    assert definitions['Write']['required'] == ['filepath', 'content']
    for schema in definitions.values():
        assert schema['additionalProperties'] is False
        assert all(p['type'] in {'string', 'integer', 'boolean'} for p in schema['properties'].values())
    assert openrouter.input_token_bound(req()) > len(json.dumps(payload['tools']).encode())
    assert payload['tool_choice'] == 'auto'
    assert payload['provider']['allow_fallbacks'] is False


def test_native_read_keeps_call_identity_and_existing_runtime_contract():
    assert parse(reply())['tool_calls'] == [{
        'tool_name': 'Read', 'arguments': {'path': 'smoke-fixture.txt'},
        'shell_mode': 'read_only', 'timeout_sec': 30, 'provider_call_id': 'call-native-read',
    }]


@pytest.mark.parametrize('name,args,mode', [
    ('List', {}, 'read_only'), ('Search', {'pattern': 'needle'}, 'read_only'),
    ('Write', {'filepath': 'x', 'content': ''}, 'mutating'),
    ('FindAndReplace', {'filepath': 'x', 'old_string': 'a', 'new_string': ''}, 'mutating'),
    ('InsertAtEnd', {'filepath': 'x', 'content': 'new'}, 'mutating'),
    ('Bash', {'command': 'pwd'}, 'read_only'),
    ('Bash', {'command': 'touch x', 'shell_mode': 'mutating'}, 'mutating'),
])
def test_flat_tools_normalize_without_execution_or_permission_bypass(name, args, mode):
    call = parse(reply(name, args))['tool_calls'][0]
    assert call['tool_name'] == name
    assert call['shell_mode'] == mode
    assert call['arguments'] == {k: v for k, v in args.items() if k != 'shell_mode'}


@pytest.mark.parametrize('name,args', [
    ('Read', {}), ('Read', {'path': True}), ('Read', {'path': 'x', 'extra': 'y'}),
    ('Read', {'path': 'x', 'shell_mode': 'mutating'}),
    ('Read', {'path': 'x', 'timeout_sec': True}),
    ('Read', {'path': 'x', 'timeout_sec': 301}), ('Read', {'path': 'x', 'timeout_sec': 0}),
    ('List', {'recursive': 1}), ('Bash', {'command': 'pwd', 'shell_mode': 'other'}),
    ('call_local_tool', {'tool_name': 'Read', 'arguments': {'path': 'x'}}),
    ('Delete', {'path': 'x'}), ('Write', {'filepath': 'x'}),
])
def test_invalid_unadvertised_or_incomplete_call_fails_closed(name, args):
    with pytest.raises(ValueError):
        parse(reply(name, args))
