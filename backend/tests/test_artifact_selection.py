"""Unit tests for the artifact → tools bridge.

They cover the pure, security-relevant shaping: what ids survive normalization, what a
tool description may leak, and how results are bounded before they cross into untrusted
artifact code. No database, no app boot: ``artifact_selection`` is dependency-free.
"""

import json

import pytest
from open_webui.utils.artifact_selection import (
    DEFAULT_MAX_RESULT_BYTES,
    MAX_ARTIFACT_TOOL_IDS,
    is_valid_tool_name,
    mcp_server_id,
    normalize_tool_ids,
    public_tool_spec,
    serialize_tool_result,
)


class TestNormalizeToolIds:
    def test_deduplicates_and_preserves_order(self):
        assert normalize_tool_ids(['b', 'a', 'b', ' server:mcp:x ']) == ['b', 'a', 'server:mcp:x']

    def test_drops_malformed_entries(self):
        assert normalize_tool_ids(['ok', '', '   ', None, 42, {'id': 'x'}, ['y']]) == ['ok']

    def test_drops_absurdly_long_ids(self):
        assert normalize_tool_ids(['x' * 513, 'fine']) == ['fine']

    def test_caps_the_number_of_ids(self):
        ids = [f'tool-{index}' for index in range(MAX_ARTIFACT_TOOL_IDS + 10)]

        assert len(normalize_tool_ids(ids)) == MAX_ARTIFACT_TOOL_IDS

    @pytest.mark.parametrize('value', [None, 'tool', 3, {'tool_id': 'x'}])
    def test_non_lists_yield_nothing(self, value):
        assert normalize_tool_ids(value) == []


class TestMcpServerId:
    def test_reads_plain_and_filtered_ids(self):
        assert mcp_server_id('server:mcp:github') == 'github'
        assert mcp_server_id('server:mcp:github|list,get') == 'github'
        assert mcp_server_id('server:mcp:') is None

    def test_ignores_local_tool_ids(self):
        assert mcp_server_id('local_tool') is None
        assert mcp_server_id(None) is None


class TestPublicToolSpec:
    def test_never_exposes_callables_or_clients(self):
        spec = public_tool_spec(
            'github_list_issues',
            {
                'tool_id': 'server:mcp:github|list_issues',
                'callable': object(),
                'client': object(),
                'spec': {
                    'name': 'github_list_issues',
                    'description': 'List issues',
                    'parameters': {'type': 'object', 'properties': {'repo': {'type': 'string'}}},
                },
            },
        )

        assert set(spec) == {'name', 'label', 'description', 'parameters', 'type', 'server_id'}
        assert json.dumps(spec)  # JSON-safe by construction
        assert spec['server_id'] == 'github'
        assert spec['type'] == 'mcp'

    def test_missing_parameters_become_an_empty_object_schema(self):
        spec = public_tool_spec('local_tool', {'tool_id': 'local_tool', 'type': 'tool', 'spec': {}})

        assert spec['parameters'] == {'type': 'object', 'properties': {}}
        assert spec['server_id'] is None
        assert spec['description'] == ''

    def test_local_tools_keep_their_type(self):
        spec = public_tool_spec(
            'query_db',
            {'tool_id': 'abc', 'spec': {'name': 'query_db', 'parameters': {}}},
        )

        assert spec['type'] == 'tool'


class TestSerializeToolResult:
    def test_round_trips_plain_payloads(self):
        payload = {'rows': [{'id': 1}], 'ok': True}

        assert serialize_tool_result('tool', payload) == payload

    def test_truncates_oversized_payloads(self):
        result = serialize_tool_result('tool', {'blob': 'x' * 512}, max_bytes=64)

        assert result['truncated'] is True

    def test_stringifies_unserializable_results(self):
        class Opaque:
            def __str__(self):
                return 'opaque'

        assert serialize_tool_result('tool', Opaque()) == 'opaque'

    def test_default_budget_is_generous_but_finite(self):
        assert DEFAULT_MAX_RESULT_BYTES == 2 * 1024 * 1024
        assert serialize_tool_result('tool', 'y' * 1024) == 'y' * 1024


class TestToolNameValidation:
    @pytest.mark.parametrize('name', ['ok', 'a' * 256, 'server:mcp:github_list'])
    def test_accepts_plausible_names(self, name):
        assert is_valid_tool_name(name) is True

    @pytest.mark.parametrize('name', ['', None, 7, 'a' * 257])
    def test_rejects_everything_else(self, name):
        assert is_valid_tool_name(name) is False
