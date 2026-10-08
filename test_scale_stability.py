import unittest
import cv2
import numpy as np
from PIL import Image,ImageDraw
import scale_stability as ss

def robot(motion=0,edges=False):
    im=Image.new('RGBA',(320,576));d=ImageDraw.Draw(im)
    d.rounded_rectangle((94,32,225,162),18,fill=(210,210,210,255))
    d.rectangle((137,162,181,192),fill=(150,150,150,255))
    d.rounded_rectangle((78-motion,190,240+motion,391),12,fill=(180,180,180,255))
    for x in [89,191]:d.rectangle((x,390,x+40,531),fill=(200,200,200,255))
    d.rectangle((0 if edges else 30,210,79,315),fill=(150,150,150,255))
    d.rectangle((241,210,319 if edges else 287,315),fill=(150,150,150,255))
    rng=np.random.default_rng(831)
    for y0,y1,x0,x1 in [(45,118,107,212),(208,370,94,226),(405,519,97,120),(405,519,200,223)]:
        for _ in range(20):
            x=int(rng.integers(x0,x1));y=int(rng.integers(y0,y1));d.rectangle((x,y,x+4,y+4),fill=(35,35,35,255))
    d.ellipse((125,130,193,135+motion*2),fill=(45,45,45,255))
    return np.array(im)

def zoom(a,sx,sy=None,tx=0):
    sy=sx if sy is None else sy;h,w=a.shape[:2];m=np.float32([[sx,0,(1-sx)*(w-1)/2+tx],[0,sy,(1-sy)*(h-1)/2]])
    p=a.astype(np.float32)/255;p[:,:,:3]*=p[:,:,3:]
    p=cv2.warpAffine(p,m,(w,h),flags=cv2.INTER_LINEAR)
    p[:,:,:3]=np.divide(p[:,:,:3],p[:,:,3:],out=np.zeros_like(p[:,:,:3]),where=p[:,:,3:]>1e-6)
    return np.uint8(np.clip(np.rint(p*255),0,255))

class StabilityTests(unittest.TestCase):
    def test_tiny_nonlinear_scale_drift(self):
        a=robot()
        for drift in [.005,.01,.02]:
            truth=1+drift*np.linspace(0,1,25)**1.6
            frames=[zoom(a,s) for s in truth];plan=ss.analyze(lambda i:frames[i],len(frames),24)
            self.assertEqual(plan['status'],'applied',plan)
            measured=np.array([v['sx'] for v in plan['transforms']])
            self.assertLess(np.max(np.abs(measured-truth)),.0012)
            out=[ss.apply(f,t) for f,t in zip(frames,plan['transforms'])]
            widths=[np.count_nonzero(f[:,:,3]>127,axis=1).max() for f in out]
            self.assertLessEqual(max(widths)-min(widths),1)
            self.assertTrue(all(x.shape==a.shape for x in out))
    def test_horizontal_drift_and_local_expression_survive(self):
        originals=[robot(round(3*np.sin(i/24*np.pi)**2)) for i in range(25)]
        frames=[zoom(f,1+.01*i/24,1,tx=2*np.sin(i/24*np.pi)) for i,f in enumerate(originals)]
        p=ss.analyze(lambda i:frames[i],25,24)
        self.assertEqual(p['status'],'applied')
        self.assertLess(abs(p['transforms'][-1]['sx']-1.01),.0012)
        self.assertLess(abs(p['transforms'][-1]['sy']-1),.0012)
        # Opening mouth remains substantially different after global correction.
        out=ss.apply(frames[12],p['transforms'][12])
        self.assertGreater(np.abs(out[128:145,120:200,:3].astype(float)-originals[0][128:145,120:200,:3]).mean(),8)
    def test_local_motion_without_drift_is_not_global_scale(self):
        frames=[robot(round(4*np.sin(i/24*np.pi)**2)) for i in range(25)]
        p=ss.analyze(lambda i:frames[i],25,24)
        self.assertEqual(p['status'],'stable')
        self.assertTrue(np.array_equal(ss.apply(frames[12],p['transforms'][12]),frames[12]))

    def test_real_local_breathing_warp_is_preserved(self):
        a=robot();frames=[]
        for i in range(25):
            local=zoom(a,1+.025*np.sin(i/24*np.pi)**2,1)
            f=a.copy();f[190:390]=local[190:390];frames.append(f)
        p=ss.analyze(lambda i:frames[i],25,24)
        # Local torso expansion must not become a global shrink of head and legs.
        self.assertLess(max(abs(t['sx']-1) for t in p['transforms']),.001)
        out=ss.apply(frames[12],p['transforms'][12])
        self.assertGreater(out[350,:,3].sum(),a[350,:,3].sum()*1.015)

    def test_shared_reference_matches_second_expression(self):
        ref=robot();frames=[zoom(robot(3),1.015+.005*i/20) for i in range(21)]
        p=ss.analyze(lambda i:frames[i],21,24,reference=ref)
        self.assertEqual(p['status'],'applied')
        self.assertLess(abs(p['transforms'][0]['sx']-1.015),.0012)
        self.assertLess(abs(p['transforms'][-1]['sx']-1.02),.0012)

    def test_static_identity_and_textureless_review(self):
        a=robot();p=ss.analyze(lambda i:a,12,24)
        self.assertEqual(p['status'],'stable')
        self.assertTrue(np.array_equal(a,ss.apply(a,p['transforms'][4])))
        empty=np.zeros_like(a);p=ss.analyze(lambda i:empty,12,24)
        self.assertEqual(p['status'],'review')
    def test_local_textured_region_cannot_shrink_static_silhouette(self):
        im=Image.new('RGBA',(320,576));d=ImageDraw.Draw(im);d.ellipse((45,15,275,560),fill=(180,180,180,255))
        rng=np.random.default_rng(4)
        for _ in range(240):
            x=int(rng.integers(95,226));y=int(rng.integers(200,375));d.rectangle((x,y,x+4,y+4),fill=(35,35,35,255))
        a=np.array(im);frames=[]
        for i in range(25):
            moving=zoom(a,1+.02*np.sin(i/24*np.pi)**2);f=a.copy();f[190:391,78:240,:3]=moving[190:391,78:240,:3];frames.append(f)
        p=ss.analyze(lambda i:frames[i],25,24)
        self.assertNotEqual(p['status'],'applied')
        self.assertTrue(np.array_equal(ss.apply(frames[12],p['transforms'][12])[:,:,3],a[:,:,3]))

    def test_local_breathing_silhouette_does_not_shrink_fixed_head(self):
        im=Image.new('RGBA',(320,576));d=ImageDraw.Draw(im);d.ellipse((45,15,275,560),fill=(180,180,180,255))
        rng=np.random.default_rng(4)
        for _ in range(240):
            x=int(rng.integers(95,226));y=int(rng.integers(160,390));d.rectangle((x,y,x+4,y+4),fill=(35,35,35,255))
        a=np.array(im);frames=[]
        for i in range(25):
            moving=zoom(a,1+.02*np.sin(i/24*np.pi)**2,1);f=a.copy();f[150:400]=moving[150:400];frames.append(f)
        p=ss.analyze(lambda i:frames[i],25,24)
        out=ss.apply(frames[12],p['transforms'][12])
        self.assertTrue(np.array_equal(out[:150,:,3],a[:150,:,3]))

    def test_side_contact_does_not_hide_new_top_cropping(self):
        a=np.zeros((576,320,4),np.uint8);a[210:315,:80]=[180,180,180,255];a[1:8,23:31]=[220,90,90,255]
        with self.assertRaisesRegex(ValueError,'裁切'):ss.apply(a,dict(sx=.98,sy=.98,cx=160,cy=288))

    def test_side_edge_coverage_allows_tangential_scale_correction(self):
        a=robot(edges=True);frames=[zoom(a,1+.02*i/24) for i in range(25)]
        p=ss.analyze(lambda i:frames[i],25,24);out=ss.apply(frames[-1],p['transforms'][-1])
        ref=np.where(a[:,0,3]>127)[0];actual=np.where(out[:,0,3]>127)[0]
        self.assertLessEqual(abs(ref.min()-actual.min()),1);self.assertLessEqual(abs(ref.max()-actual.max()),1)
        self.assertTrue(np.all(out[220:300,0,3]>250))
        self.assertEqual(out.shape,a.shape)

if __name__=='__main__':unittest.main()
