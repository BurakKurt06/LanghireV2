"""Importing an embedded backend must not leak an unused log file."""
import os
from pathlib import Path
import subprocess
import sys


def test_preconfigured_host_logging_does_not_open_a_discarded_file(tmp_path):
    env = dict(os.environ, HOME=str(tmp_path), USERPROFILE=str(tmp_path), PYTHONUTF8="1")
    program = '''
import gc, logging, sys, warnings
from pathlib import Path
sys.path.insert(0, str(Path.cwd() / 'backend'))
warnings.simplefilter('error', ResourceWarning)
errors = []
sys.unraisablehook = errors.append
handler = logging.StreamHandler()
logging.getLogger().addHandler(handler)
import main
gc.collect()
assert not errors, errors
assert logging.getLogger().handlers == [handler]
assert not (main.get_data_dir() / 'backend.log').exists()
'''
    result = subprocess.run([sys.executable, "-c", program], cwd=Path(__file__).resolve().parents[2],
        env=env, capture_output=True, text=True, encoding="utf-8", timeout=45)
    assert result.returncode == 0, result.stdout + result.stderr
