-- Add GPT-5.4 to provider catalog for OpenAI routing.
INSERT INTO provider_catalog (
    provider,
    model_id,
    display_name,
    enabled,
    cost_input_per_1m,
    cost_output_per_1m,
    v_price_input_per_1m,
    v_price_output_per_1m,
    max_tokens
)
VALUES (
    'openai',
    'gpt-5.4',
    'GPT-5.4',
    TRUE,
    2.50,
    15.00,
    300,
    1800,
    128000
)
ON CONFLICT (provider, model_id) DO UPDATE
SET
    display_name = EXCLUDED.display_name,
    enabled = TRUE,
    cost_input_per_1m = EXCLUDED.cost_input_per_1m,
    cost_output_per_1m = EXCLUDED.cost_output_per_1m,
    v_price_input_per_1m = EXCLUDED.v_price_input_per_1m,
    v_price_output_per_1m = EXCLUDED.v_price_output_per_1m,
    max_tokens = EXCLUDED.max_tokens,
    updated_at = now();

INSERT INTO schema_migrations(version)
VALUES ('034_provider_catalog_gpt54')
ON CONFLICT (version) DO NOTHING;
