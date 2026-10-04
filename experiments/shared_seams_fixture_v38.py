"""Generate neutral robot animation jobs with deliberate cross-clip drift.

Only writes under --output (default an isolated backup fixture); no user media
is opened. Clips retain distinct facial states and crop across both side edges.
"""
from pathlib import Path
import argparse
import hashlib
import json
import sys

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from experiments.alignment_synthetic_v37 import robot, warp, write_job, isolated, jid
import transitions as t


def create_fixture(out):
    out.mkdir(parents=True, exist_ok=True)
    states = [0., 1.5, -1.0, 3.0, 0.]
    names = ['AA-AB', 'AB-BB', 'BB-BA', 'BA-AA']
    drifts = [-1.1, .7, 1.4, -.5]
    entries = []
    for segment, name in enumerate(names):
        frames = []
        for index in range(24):
            phase = index / 23
            state = states[segment] * (1-phase) + states[segment+1] * phase
            # Per-clip shape offsets make adjacent endpoints disagree even
            # though they denote the same logical expression state.
            amount = float(drifts[segment] + .18 * np.sin(phase * np.pi * 2))
            rgba = warp(robot(180, 320, mouth=state * 2), amount=amount)
            frames.append(rgba)
        job_id = write_job(out, 'SYNTH-v38-' + name, frames)
        entries.append(dict(id=job_id, name=name))
    with isolated(out):
        for entry in entries:
            job = t.e.read(entry['id'])
            job['config']['fps'] = 24
            t.e.atomic_json(t.e.jobdir(entry['id']) / 'job.json', job)
        project = t.handle('create', dict(name='Synthetic shared states AA-AB-BB-BA'))
        project['clips'] = [dict(key=jid('v38-' + entry['name']), label=entry['name'], job_id=entry['id'],
                                 version='original', start=1, end=24, head_frames=6, tail_frames=6)
                            for entry in entries]
        project['sequence'] = [clip['key'] for clip in project['clips']]
        project = t.handle('save', dict(project=project))
    manifest = dict(synthetic_only=True, project_id=project['id'], jobs=entries,
                    dimensions=[180,320], fps=24, frames_per_clip=24,
                    sequence=names, head_frames=6, tail_frames=6,
                    source_hashes={str(p.relative_to(out)):hashlib.sha256(p.read_bytes()).hexdigest()
                                   for p in (out/'jobs').rglob('*.png')})
    (out/'fixture_manifest.json').write_text(json.dumps(manifest,indent=2,ensure_ascii=False),encoding='utf-8')
    return manifest


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--output',type=Path,default=ROOT/'backups/v38_shared_seams/synthetic_fixture')
    args = parser.parse_args()
    result = create_fixture(args.output.resolve())
    print(json.dumps({k:v for k,v in result.items() if k!='source_hashes'},ensure_ascii=False))
