import copy
import json
import runpy
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np
from PIL import Image
import seam_alignment as sa


def model():
    binding=dict(job_id='a'*32,version='original',clip_key='',side='tail',frame=4,fingerprint='b'*64,appearance_hash='c'*64)
    return dict(schema=1,dimensions=[128,192],controls=[dict(target=[x,y],source=[x,y+1.25]) for y in (48,96,144) for x in (32,64,96)],boundaries={side:dict(source=[0,191],target=[0,191]) for side in ('left','right')},regularization=.00008,scale=1.,provenance=dict(scope='loop',source=binding,reference=dict(binding,side='reference',frame=1)))


class AlignmentTests(unittest.TestCase):
    def tearDown(self):sa.clear_cache()

    def test_strict_model_validation_and_bounds(self):
        good=model();self.assertEqual(sa.validate_model(json.loads(json.dumps(good))),good)
        mutations=[lambda m:m.update(schema=True),lambda m:m.update(dimensions=[1,20]),lambda m:m.update(dimensions=[4096,4096]),lambda m:m.update(controls=m['controls']*10),lambda m:m['controls'][0]['source'].__setitem__(0,float('nan')),lambda m:m['controls'][0]['target'].__setitem__(1,193),lambda m:m['controls'][0].update(extra=[]),lambda m:m.update(regularization=False),lambda m:m.update(scale=float('inf')),lambda m:m['boundaries']['left'].update(source=[0,150,100,191],target=[0,100,150,191]),lambda m:m['provenance']['source'].update(version='../outside'),lambda m:m['provenance']['source'].update(fingerprint='wrong')]
        for mutate in mutations:
            bad=copy.deepcopy(good);mutate(bad)
            with self.subTest(model=bad),self.assertRaises(ValueError):sa.validate_model(bad)
        for bad in [None,[],dict(enabled='true'),dict(enabled=True),dict(enabled=False,strength=True),dict(enabled=False,strength=float('nan')),dict(enabled=False,unknown=1)]:
            with self.subTest(alignment=bad),self.assertRaises(ValueError):sa.validate_alignment(bad)
        self.assertEqual(sa.validate_alignment(dict(enabled=False)),dict(enabled=False,strength=100.))

    def test_model_rebuild_premultiplied_sampling_identity_and_cache_bounds(self):
        m=model();dx,dy=sa.maps(m,(128,192))
        np.testing.assert_array_equal(dx[:,[0,-1]],0)
        expected=(dx.copy(),dy.copy());sa.clear_cache()
        actual=sa.maps(json.loads(json.dumps(m)),(128,192))
        np.testing.assert_array_equal(expected,actual)
        self.assertFalse(actual[0].flags.writeable)
        pixels=np.zeros((192,128,4),np.uint8);pixels[:,:,:3]=[255,0,255];pixels[40:152,20:108]=[240,240,240,255]
        np.testing.assert_array_equal(sa.resample_frame(pixels,dx,dy,0),pixels)
        result=sa.resample_frame(pixels,dx,dy,1)
        fringe=(result[:,:,3]>0)&(result[:,:,3]<255)
        self.assertTrue(fringe.any());self.assertTrue(np.all(result[fringe,:3]==240))
        for n in range(6):
            changed=copy.deepcopy(m);changed['provenance']['source']['frame']=n+1;sa.maps(changed)
        self.assertLessEqual(len(sa._CACHE),4)
        with self.assertRaisesRegex(ValueError,'畫布'):sa.maps(m,(64,96))

    def test_folded_model_and_mid_strength_fold_are_rejected(self):
        unsafe=model();unsafe['controls'][4]['source'][0]=124
        with self.assertRaisesRegex(ValueError,'折返|擠壓'):sa.maps(unsafe)
        # A 180-degree mapping has positive determinant at t=1 but collapses
        # halfway. Guarding the full strength interval must reject it.
        yy,xx=np.mgrid[:48,:48].astype(np.float32)
        with self.assertRaises(ValueError):sa._guard(47-2*xx,47-2*yy)

    def test_missing_optional_dependencies_do_not_break_module_or_legacy(self):
        namespace=runpy.run_path(str(Path(sa.__file__)))
        original_import=__import__
        def without_cv(name,*args,**kwargs):
            if name=='cv2':raise ImportError('test unavailable')
            return original_import(name,*args,**kwargs)
        with patch('builtins.__import__',side_effect=without_cv):
            with self.assertRaisesRegex(ValueError,'setup.cmd'):namespace['_dependencies']()
            self.assertFalse(namespace['active'](None))
            self.assertFalse(namespace['validate_alignment'](dict(enabled=False))['enabled'])

    def test_detached_boundary_identity_and_failed_match_warning_conditions(self):
        a=np.zeros((96,96,4),np.uint8);a[20:70,20:70]=[240,240,240,255]
        profile,pairs,report=sa.boundary_profile(a,a,0)
        np.testing.assert_array_equal(profile,0);self.assertEqual(pairs,[])
        self.assertFalse(report['matched']);self.assertEqual(report['source_intervals'],[])
        b=a.copy();b[20:70,0]=[240,240,240,255]
        profile,pairs,report=sa.boundary_profile(b,a,0)
        self.assertFalse(report['matched']);self.assertTrue(report['source_intervals'])
        np.testing.assert_array_equal(profile,0)
        with self.assertRaisesRegex(ValueError,'尺寸'):
            sa.build_alignment(np.zeros((20,20,4),np.uint8),np.zeros((20,20,4),np.uint8),model()['provenance'])
        with self.assertRaisesRegex(ValueError,'穩定|對應點'):
            sa.build_alignment(a,a,model()['provenance'])

    def test_public_transition_alignment_improves_independent_robot_metrics(self):
        # This tests fitted correspondences + saved-model rendering together.
        # Independent threshold/EDT metrics catch a no-op or reversed warp;
        # neither the model's self-report nor user imagery is used.
        from experiments.alignment_synthetic_v37 import robot, warp, public_alignment, independent_metrics
        reference = robot()
        source = warp(reference)
        before = independent_metrics(source, reference)
        with tempfile.TemporaryDirectory() as folder:
            outputs, _ = public_alignment(Path(folder), source, reference, 'regression-local-bend')
        partial = independent_metrics(outputs['68'], reference)
        full = independent_metrics(outputs['100'], reference)
        self.assertLess(partial['silhouette_mismatch_pixels'], before['silhouette_mismatch_pixels'] * .6)
        self.assertLess(partial['interior_ink_chamfer_px'], before['interior_ink_chamfer_px'] * .6)
        self.assertLess(full['silhouette_mismatch_pixels'], partial['silhouette_mismatch_pixels'])
        self.assertLess(full['interior_ink_chamfer_px'], partial['interior_ink_chamfer_px'])
        for pixels in outputs.values():
            self.assertEqual(pixels.shape, source.shape)
            self.assertTrue(np.any(pixels[:, :, 3] == 0))
            self.assertTrue(np.any((pixels[:, :, 3] > 0) & (pixels[:, :, 3] < 255)))


if __name__=='__main__':unittest.main()
