"""Install verified RIFE weights; use an existing CUDA PyTorch Python runtime.

Does not change ComfyUI or install packages into its environment.
"""
from pathlib import Path
import argparse,hashlib,json,subprocess,urllib.request,zipfile,io
from seam_repair_core import WEIGHTS_SHA256

ROOT=Path(__file__).resolve().parent
URL='https://drive.google.com/uc?export=download&id=1ZKjcbmt1hypiFprJPIKW0Tt0lr_2i7bg'
ZIP_SHA256='e63d481b7ae5d4a4e6ad7ac5b410ff78f3bf7be3b51b2e38ca8152747abde5b4'

def install(python):
    exe=Path(python).resolve(strict=True)
    subprocess.run([str(exe),'-c','import torch; assert torch.cuda.is_available(), "CUDA unavailable"; print(torch.cuda.get_device_name(0))'],check=True,timeout=60,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
    folder=ROOT/'models/rife425';folder.mkdir(parents=True,exist_ok=True);path=folder/'flownet.pkl'
    if not path.exists() or hashlib.sha256(path.read_bytes()).hexdigest()!=WEIGHTS_SHA256:
        with urllib.request.urlopen(URL,timeout=60) as response:content=response.read(32*1024*1024)
        if hashlib.sha256(content).hexdigest()!=ZIP_SHA256:raise ValueError('Official model archive checksum mismatch; nothing installed')
        with zipfile.ZipFile(io.BytesIO(content)) as archive:weights=archive.read('train_log/flownet.pkl')
        if hashlib.sha256(weights).hexdigest()!=WEIGHTS_SHA256:raise ValueError('Model checksum mismatch')
        tmp=path.with_suffix('.partial');tmp.write_bytes(weights);tmp.replace(path)
    (ROOT/'repair_runtime.json').write_text(json.dumps({'python':str(exe)},indent=2),encoding='utf-8')
    print('Local seam model ready; ComfyUI unchanged.')

if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--python',required=True,help='Existing Python executable with CUDA PyTorch installed')
    install(parser.parse_args().python)
