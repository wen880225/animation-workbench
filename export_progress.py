"""Stage timing based on completed frames, never on source-video duration."""
import time

def sample(stage,done,total,started,after=''):
    elapsed=max(0,time.time()-started)
    done=max(0,min(total,done))
    remaining=elapsed*(total-done)/done if done>=3 and elapsed>=1 else None
    return dict(stage=stage,done=done,total=total,started=started,remaining=remaining,after=after)

def read_frame(path):
    try:
        values=[int(line.split('=',1)[1].strip()) for line in path.read_text(errors='ignore').splitlines() if line.startswith('frame=')]
        return max(values,default=0)
    except (OSError,ValueError):return 0
