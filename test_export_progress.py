import unittest,tempfile
from pathlib import Path
from unittest.mock import patch
from export_progress import sample,read_frame

class ProgressTests(unittest.TestCase):
    def test_estimate_uses_completed_frames(self):
        with patch('export_progress.time.time',return_value=110):
            self.assertEqual(sample('encode',10,100,100)['remaining'],90)
            self.assertIsNone(sample('encode',0,100,100)['remaining'])
            self.assertEqual(sample('encode',110,100,100)['remaining'],0)
    def test_partial_ffmpeg_progress(self):
        with tempfile.TemporaryDirectory() as td:
            p=Path(td)/'progress';p.write_text('frame=3\nframe=12\nprogress=continue\n')
            self.assertEqual(read_frame(p),12)
            p.write_text('frame=');self.assertEqual(read_frame(p),0)
if __name__=='__main__':unittest.main()
