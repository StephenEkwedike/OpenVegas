from openvegas.tui.model_picker import format_options


def row(**caps):
    return {'provider': 'openrouter', 'model_id': 'fixture/exact', 'enabled': True,
            'available': True, 'capabilities': {'reviewed': True, **caps}}


def test_feature_labels_show_reviewed_features_and_actual_efforts_only():
    output = format_options([row(tools=True, image_input=True, file_upload=True,
        reasoning_efforts=['high', 'low'], streaming_mode='buffered')])
    assert '[tools; images; files; reasoning: low/high; buffered replies]' in output
    assert 'web' not in output and 'xhigh' not in output


def test_unreviewed_or_malformed_capabilities_do_not_claim_support():
    assert '[' not in format_options([row(reviewed=False, tools=True)])
    assert '[text]' in format_options([row(tools='yes', reasoning_efforts=['bogus\x1b'])])
