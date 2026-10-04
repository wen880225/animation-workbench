"""Install only the workbench dependencies into its private virtual environment."""
from pathlib import Path
import subprocess,sys,venv
ROOT=Path(__file__).resolve().parent
if sys.version_info[:2] != (3,12):
    print('Please install Python 3.12 (64-bit), then run setup again.');sys.exit(1)
try:
    venv.create(ROOT/'.venv',with_pip=True)
    python=ROOT/'.venv/Scripts/python.exe'
    subprocess.run([str(python),'-m','pip','install','-r',str(ROOT/'requirements.txt')],check=True)
    print('Setup complete. Run start.cmd or the Chinese launcher.')
except Exception as exc:
    print('Setup failed:',exc);sys.exit(1)
