import os
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from urllib.request import Request, urlopen
from urllib.error import URLError
import json
from tests._path import *
from agent_foundation.local_runtime import build_local_application

class RuntimeTests(unittest.TestCase):
    def test_requires_operator_configuration_and_model_disabled_by_default(self):
        with self.assertRaises(ValueError): build_local_application({})
        with tempfile.TemporaryDirectory(dir='tmp') as folder:
            app = build_local_application({'AGENT_LOCAL_ORG':'org', 'AGENT_LOCAL_EMPLOYEE':'alice',
                                           'AGENT_LOCAL_TOKEN':'local-test', 'AGENT_LOCAL_DB':str(Path(folder)/'db')})
            task = app.create_task('local-test', 'create', 'customer', 'scene')['task_id']
            from agent_foundation.features import FeatureDisabled
            with self.assertRaises(FeatureDisabled): app.enqueue_model_job('local-test','job',task,'input',1,'hash')

    def test_loopback_process_accepts_authenticated_task(self):
        with tempfile.TemporaryDirectory(dir='tmp') as folder:
            with socket.socket() as sock:
                sock.bind(('127.0.0.1', 0)); port = sock.getsockname()[1]
            env = dict(os.environ, PYTHONPATH=str(Path.cwd() / 'src'), AGENT_LOCAL_ORG='org',
                       AGENT_LOCAL_EMPLOYEE='alice', AGENT_LOCAL_TOKEN='local-test-only',
                       AGENT_LOCAL_DB=str(Path(folder)/'db'), AGENT_LOCAL_PORT=str(port),
                       AGENT_LOCAL_MODEL_ENABLED='0')
            process = subprocess.Popen([sys.executable, '-m', 'agent_foundation.local_server'], env=env,
                                       stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
            try:
                url = f'http://127.0.0.1:{port}/tasks'
                payload = json.dumps({'customer_id':'c1', 'scenario':'scene'}).encode()
                request = Request(url, data=payload, headers={'Authorization':'Bearer local-test-only',
                                  'Idempotency-Key':'create', 'Content-Type':'application/json'})
                for _ in range(50):
                    if process.poll() is not None: self.fail('Local server exited before accepting connections')
                    try:
                        with urlopen(request, timeout=1) as response:
                            result = json.load(response)
                        break
                    except URLError:
                        time.sleep(.05)
                else:
                    self.fail('Local server did not accept connections')
                self.assertEqual('received', result['status'])
                self.assertTrue(result['task_id'])
            finally:
                process.terminate()
                process.communicate(timeout=5)
