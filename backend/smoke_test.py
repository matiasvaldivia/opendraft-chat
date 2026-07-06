import sys
import os
sys.path.insert(0, '.')

from config import get_config
from utils.agent_runner import setup_model

print('--- config ---')
cfg = get_config()
print('provider     :', cfg.model.provider)
print('model        :', cfg.model.model_name)
print('has_openai   :', bool(cfg.openai_api_key))
print('base_url     :', os.environ.get('OPENAI_BASE_URL'))
print('api_key_prefix:', (cfg.openai_api_key or '')[:8] + '...' if cfg.openai_api_key else '<none>')

print('--- setup_model() ---')
m = setup_model()
print('type         :', type(m).__name__)
print('model_name   :', m.model_name)
print('temperature  :', m.default_temperature)

print('--- live call (simple) ---')
resp = m.generate_content('In one sentence: what is LiteLLM?')
print('response.text:', resp.text.strip())

print('--- live call (with GenerationConfig) ---')
from utils.gemini_client import GenerationConfig
gc = GenerationConfig(temperature=0.3, max_output_tokens=60)
resp2 = m.generate_content('Reply with a single JSON object: {"ok": true}', generation_config=gc)
print('response.text:', resp2.text.strip())

print('ALL OK')