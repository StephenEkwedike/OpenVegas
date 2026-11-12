"""Dependency-free shapes observed in the pinned 0.166.0 native metadata parser.

See .local/native-followthrough-20261112/catalog/fix/native-parser-observations.json.
This validates known export fields, not instructions, provider capability claims,
or unknown extension fields. Native loading remains a separate compatibility gate.
"""

EXPORT_FIELDS = frozenset({
    'slug', 'display_name', 'description', 'base_instructions', 'model_messages',
    'supported_reasoning_levels', 'default_reasoning_level', 'input_modalities', 'shell_type',
    'visibility', 'supported_in_api', 'priority', 'support_verbosity', 'default_verbosity',
    'default_reasoning_summary', 'truncation_policy', 'experimental_supported_tools',
    'context_window', 'max_context_window', 'effective_context_window_percent', 'upgrade',
    'service_tiers', 'additional_speed_tiers', 'comp_hash', 'apply_patch_tool_type',
    'availability_nux', 'include_apps_usage_instructions', 'include_plugin_usage_instructions',
    'include_skills_usage_instructions', 'node_repl_auto_review_required', 'node_repl_disabled',
    'supports_experimental_context', 'supports_image_detail_original',
    'supports_reasoning_effort_updates', 'supports_search_tool', 'use_responses_lite',
    'web_search_tool_type',
})
BOOL_FIELDS = frozenset({
    'supported_in_api', 'support_verbosity', 'include_apps_usage_instructions',
    'include_plugin_usage_instructions', 'include_skills_usage_instructions',
    'node_repl_auto_review_required', 'node_repl_disabled', 'supports_experimental_context',
    'supports_image_detail_original', 'supports_reasoning_effort_updates',
    'supports_search_tool', 'use_responses_lite',
})
STRING_FIELDS = frozenset({'slug', 'display_name'})
ENUM_FIELDS = frozenset({'shell_type', 'visibility', 'default_reasoning_summary', 'web_search_tool_type'})
NULLABLE_STRINGS = frozenset({'description', 'default_reasoning_level', 'comp_hash'})
NULLABLE_ENUMS = frozenset({'default_verbosity', 'apply_patch_tool_type'})
MESSAGE_OBJECT_FIELDS = frozenset({'approvals', 'collaboration_modes', 'auto_review',
                                   'permissions', 'multi_agent', 'token_budget', 'guardian_v2',
                                   'confirmation_policies'})


def _integer(value, bits=64):
    return type(value) is int and -(1 << (bits - 1)) <= value < (1 << (bits - 1))


def _strings(value):
    return isinstance(value, list) and all(isinstance(item, str) for item in value)


def _variant(value):
    # The pinned parser accepts both JSON spellings of a unit enum variant.
    if isinstance(value, str):
        return value
    if isinstance(value, dict) and len(value) == 1:
        key, item = next(iter(value.items()))
        if isinstance(key, str) and item is None:
            return key
    return None


def valid_native_row(row):
    """Validate without modifying or reconstructing the reviewed native row."""
    if not isinstance(row, dict) or not EXPORT_FIELDS <= row.keys():
        return False
    if (any(type(row[key]) is not bool for key in BOOL_FIELDS)
            or any(not isinstance(row[key], str) for key in STRING_FIELDS)
            or any(_variant(row[key]) is None for key in ENUM_FIELDS)
            or any(row[key] is not None and not isinstance(row[key], str)
                   for key in NULLABLE_STRINGS)
            or any(row[key] is not None and _variant(row[key]) is None for key in NULLABLE_ENUMS)
            or not isinstance(row['base_instructions'], str) or not row['base_instructions']
            or not _integer(row['context_window']) or row['context_window'] <= 0
            or not _integer(row['priority'], 32)
            or not _integer(row['effective_context_window_percent'])
            or (row['max_context_window'] is not None and not _integer(row['max_context_window']))
            or not _strings(row['experimental_supported_tools'])
            or not _strings(row['additional_speed_tiers'])):
        return False
    for key in ('upgrade', 'availability_nux', 'model_messages'):
        if row[key] is not None and not isinstance(row[key], dict):
            return False
    upgrade = row['upgrade']
    if upgrade is not None and (
        not isinstance(upgrade.get('model'), str)
        or (upgrade.get('migration_markdown') is not None
            and not isinstance(upgrade['migration_markdown'], str))
    ):
        return False
    if row['availability_nux'] is not None and not isinstance(row['availability_nux'].get('message'), str):
        return False
    messages = row['model_messages']
    if messages is not None:
        for key in ('instructions_template', 'persistent_instructions'):
            if messages.get(key) is not None and not isinstance(messages[key], str):
                return False
        for key in MESSAGE_OBJECT_FIELDS:
            if messages.get(key) is not None and not isinstance(messages[key], dict):
                return False
    truncation = row['truncation_policy']
    if (not isinstance(truncation, dict) or _variant(truncation.get('mode')) not in ('bytes', 'tokens')
            or not _integer(truncation.get('limit'))):
        return False
    tiers = row['service_tiers']
    if not isinstance(tiers, list) or any(
        not isinstance(tier, dict) or any(not isinstance(tier.get(key), str)
                                        for key in ('id', 'name', 'description'))
        for tier in tiers
    ):
        return False
    efforts = row['supported_reasoning_levels']
    if (not isinstance(efforts, list) or not 1 <= len(efforts) <= 32
            or any(not isinstance(level, dict) or not isinstance(level.get('effort'), str)
                   or not 1 <= len(level['effort']) <= 100
                   or not isinstance(level.get('description'), str) for level in efforts)):
        return False
    values = [level['effort'] for level in efforts]
    if len(set(values)) != len(values) or row['default_reasoning_level'] not in values:
        return False
    modalities = row['input_modalities']
    return (isinstance(modalities, list) and bool(modalities)
            and all(value in ('text', 'image', 'audio') for value in modalities))
