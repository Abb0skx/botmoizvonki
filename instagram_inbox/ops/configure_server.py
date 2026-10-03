"""Executed on the deployment host. Reuse Meta credentials without exposing secrets."""
import json
import os
from pathlib import Path
import subprocess


root = Path('/opt/texnikach-instagram-inbox')
root.mkdir(mode=0o700, parents=True, exist_ok=True)
state = json.loads(subprocess.check_output(['docker', 'exec', 'texnikach-mtproto-jobs',
    'python', '-c', 'from pathlib import Path; print(Path("/app/data/instagram-inbox-bootstrap.json").read_text())']))
containers = json.loads(subprocess.check_output(['docker', 'inspect'] +
    subprocess.check_output(['docker', 'ps', '-q']).decode().split()))
matches = [c for c in containers if c['Name'].startswith('/nylgfmvjodgie9dga7ngprgl-')]
if len(matches) != 1:
    raise RuntimeError('Expected exactly one current Instagram application container')
source = dict(x.split('=', 1) for x in matches[0]['Config']['Env'] if '=' in x)
config = {
    'META_APP_SECRET': source['INSTAGRAM_APP_SECRET'],
    'META_ACCESS_TOKEN': source['INSTAGRAM_ACCESS_TOKEN'],
    'META_VERIFY_TOKEN': source['INSTAGRAM_VERIFY_TOKEN'],
    'INSTAGRAM_ACCOUNT_ID': source.get('INSTAGRAM_ACCOUNT_ID', '17841444196466655'),
    'META_GRAPH_BASE_URL': 'https://graph.instagram.com/v26.0',
    'TELEGRAM_BOT_TOKEN': state['bot_token'],
    'TELEGRAM_GROUP_ID': str(state['group_id']),
    'TELEGRAM_MANAGER_IDS': str(state['owner_id']),
    'OPENAI_API_KEY': '',
    'OPENAI_MODEL': 'gpt-4.1-mini',
    'DATABASE_URL': 'sqlite:////data/inbox.db',
    'AI_CONTEXT_MESSAGES': '20',
    'INCOMING_DEBOUNCE_SECONDS': '4',
    'TOPIC_CLOSE_AFTER_HOURS': '72',
    'LEGACY_INSTAGRAM_WEBHOOK_URL': 'http://texnikach-calls-service:8000/webhooks/instagram',
}
env_path = root / '.env'
if env_path.exists():
    previous = dict(line.split('=', 1) for line in env_path.read_text().splitlines()
                    if '=' in line and not line.startswith('#'))
    # Retain separately provisioned AI credentials and intentional settings on re-run.
    config.update(previous)
for value in config.values():
    if '\n' in value or '\r' in value:
        raise ValueError('Unexpected multiline secret')
fd = os.open(env_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
with os.fdopen(fd, 'w') as out:
    out.write('\n'.join(f'{key}={value}' for key, value in config.items()) + '\n')
data = root / 'data'
data.mkdir(mode=0o700, exist_ok=True)
os.chown(data, 10001, 10001)
print(json.dumps({'configured': True, 'group_id': state['group_id'],
                  'bot_username': state['bot_username'], 'ai_key_present': bool(config['OPENAI_API_KEY'])}))
