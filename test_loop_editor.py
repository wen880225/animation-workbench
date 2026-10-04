import unittest,tempfile,json,hashlib,base64,io,os
from pathlib import Path
from unittest.mock import patch
import numpy as np
from PIL import Image
import loop_editor as le

class LoopTests(unittest.TestCase):
    def setUp(self):
        self.j=dict(id='a'*32,frames=[dict(status='done',file=f'frame_{i:08d}.png') for i in range(1,5)],dimensions=[64,80],config=dict(fps='12'),paths=dict(png='transparent_png'))
        self.r=le.recipe(self.j,dict(ramp=1))
        a=np.zeros((80,64,4),np.uint8);a[15:65,10:54]=[240,240,240,255];a[30:45,26:38]=[30,30,30,255];self.im=Image.fromarray(a)
    def test_fixed_canvas_and_first_frame(self):
        r=dict(self.r,dx=1.5,sx=98.5)
        first,_=le.transform(self.im,r,1);last,_=le.transform(self.im,r,4)
        self.assertTrue(np.array_equal(first,self.im));self.assertEqual(last.size,self.im.size)
        self.assertFalse(np.array_equal(last,self.im))
    def test_version_list_metadata_and_broken_record(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);versions=root/'loop_edits';versions.mkdir()
            done=versions/'修整_001';done.mkdir()
            (done/'status.json').write_text(json.dumps(dict(state='complete',phase='完成')),encoding='utf-8')
            (done/'recipe.json').write_text(json.dumps(self.r),encoding='utf-8')
            report=dict(frames=4,fps='12',dimensions=[64,80],source_range=[1,4])
            (done/'verification.json').write_text(json.dumps(report),encoding='utf-8')
            bad=versions/'修整_002';bad.mkdir();(bad/'status.json').write_text('{broken',encoding='utf-8')
            live=versions/'修整_003';live.mkdir();(live/'status.json').write_text(json.dumps(dict(state='running',phase='準備')),encoding='utf-8')
            with patch.object(le.e,'jobdir',return_value=root),patch.dict(le.RUNS,{self.j['id']:dict(state='running',version=live.name,phase='合成')},clear=True):
                result=le.handle('load',self.j,{})
            listed={v['name']:v for v in result['versions']}
            self.assertEqual(listed[done.name]['recipe'],self.r)
            self.assertEqual(listed[done.name]['report'],report)
            self.assertEqual(listed[bad.name]['state'],'error')
            self.assertEqual(listed[live.name]['state'],'running')
            self.assertEqual(listed[live.name]['phase'],'合成')
    def test_protection_and_alpha_sampling(self):
        r=dict(self.r,dx=2,protect=True,px=50,py=45,radius=18)
        out,_=le.transform(self.im,r,4)
        self.assertTrue(np.array_equal(np.array(out)[33:39,29:35],np.array(self.im)[33:39,29:35]))
        out,_=le.transform(self.im,dict(self.r,dx=.5),4);a=np.array(out)
        edge=(a[:,:,3]>0)&(a[:,:,3]<255)
        self.assertTrue(edge.any());self.assertTrue(np.all(a[edge,:3]==240))
    def test_clipping_and_validation(self):
        _,clipped=le.transform(self.im,dict(self.r,dx=40),4);self.assertTrue(clipped)
        for bad in [dict(start=4,end=2),dict(sx=float('nan')),dict(ramp=4),dict(sx=0)]:
            with self.assertRaises(ValueError):le.recipe(self.j,bad)
    def test_region_validation_and_legacy_output(self):
        self.assertNotIn('region',self.r)
        default=dict(mode='inside',shape='rect',x=25,y=25,w=50,h=50,feather=10)
        self.assertEqual(le.recipe(self.j,dict(region=default))['region'],default)
        for bad in [None,[],dict(mode='unknown'),dict(shape='triangle'),dict(x=-1),dict(w=0),dict(x=80,w=30),dict(h=float('nan')),dict(feather=51),dict(y=True)]:
            with self.subTest(region=bad),self.assertRaises(ValueError):le.recipe(self.j,dict(region=bad))
        pixels=np.arange(80*64*4,dtype=np.uint8).reshape(80,64,4)
        legacy=le.recipe(self.j,dict(ramp=1,dx=1.25,dy=-.75,sx=99,sy=102,angle=1.5,protect=True,px=50,py=45,radius=18))
        adjusted,clipped=le.transform(Image.fromarray(pixels),legacy,4)
        # Recorded from the pre-region renderer, including invisible RGB and old warning.
        self.assertEqual(hashlib.sha256(adjusted.tobytes()).hexdigest(),'f4a178644a9390de393dc397ae5ceb5c3a3e31edfbfc765bab9e5a87067d151b')
        self.assertTrue(clipped)
        explicit_all=dict(legacy,region=le.region(dict(mode='all')))
        global_only=dict(legacy,protect=False)
        self.assertTrue(np.array_equal(le.transform(Image.fromarray(pixels),explicit_all,4)[0],le.transform(Image.fromarray(pixels),global_only,4)[0]))

    def test_rect_and_ellipse_masks_preserve_unselected_rgba_exactly(self):
        yy,xx=np.mgrid[:80,:64].astype(np.float32)
        pixels=np.asarray(self.im).copy()
        pixels[pixels[:,:,3]==0,:3]=[231,41,123]
        # Nonuniform opaque pixels ensure a fractional displacement is observable.
        pixels[25:55,20:44,:3]=np.arange(30*24*3,dtype=np.uint8).reshape(30,24,3)
        for shape in ('rect','ellipse'):
            for mode in ('inside','outside'):
                for feather in (0,15):
                    with self.subTest(shape=shape,mode=mode,feather=feather):
                        region=le.region(dict(mode=mode,shape=shape,x=25,y=25,w=50,h=50,feather=feather))
                        influence=le.region_influence(region,xx,yy,64,80)
                        result,_=le.transform(Image.fromarray(pixels),dict(self.r,dx=1.5,region=region),4)
                        self.assertEqual(result.size,self.im.size)
                        result=np.asarray(result)
                        self.assertTrue(np.array_equal(result[influence==0],pixels[influence==0]))
                        self.assertFalse(np.array_equal(result[influence>0],pixels[influence>0]))
                        if mode=='inside':self.assertTrue(np.array_equal(result[:19],pixels[:19]))
                        else:self.assertTrue(np.array_equal(result[38:42,30:34],pixels[38:42,30:34]))

    def test_region_feather_direction_and_local_clipping(self):
        xx=np.array([24.5,25.5,29.5,34.5,49.5,74.5,79.5,84.5]);yy=np.full_like(xx,49.5)
        r=le.region(dict(mode='inside',shape='rect',x=25,y=25,w=50,h=50,feather=20))
        np.testing.assert_allclose(le.region_influence(r,xx,yy,100,100),[0,.028,.5,1,1,0,0,0],atol=1e-6)
        r['mode']='outside'
        np.testing.assert_allclose(le.region_influence(r,xx,yy,100,100),[0,0,0,0,0,0,.5,1],atol=1e-6)
        local=le.region(dict(mode='inside',shape='rect',x=25,y=25,w=50,h=50,feather=0))
        self.assertTrue(le.transform(self.im,dict(self.r,dx=40),4)[1])
        self.assertFalse(le.transform(self.im,dict(self.r,dx=40,region=local),4)[1])
        outside=dict(local,mode='outside',feather=15)
        self.assertFalse(le.transform(self.im,dict(self.r,dx=1.5,region=outside),4)[1])
        pixels=np.asarray(self.im).copy();pixels[30:50,60:64]=[240,240,240,255]
        self.assertTrue(le.transform(Image.fromarray(pixels),dict(self.r,dx=10,region=outside),4)[1])

    def test_region_alpha_edges_do_not_pick_up_invisible_rgb(self):
        pixels=np.zeros((80,64,4),np.uint8);pixels[:,:,:3]=[255,0,255]
        pixels[20:60,15:49]=[240,240,240,255]
        yy,xx=np.mgrid[:80,:64].astype(np.float32)
        for mode in ('inside','outside'):
            selected=le.region(dict(mode=mode,shape='ellipse',x=10,y=10,w=80,h=80,feather=0)) if mode=='inside' else le.region(dict(mode=mode,shape='rect',x=35,y=35,w=30,h=30,feather=15))
            result=np.asarray(le.transform(Image.fromarray(pixels),dict(self.r,dx=.5,region=selected),4)[0])
            active=le.region_influence(selected,xx,yy,64,80)>0
            edge=active&(result[:,:,3]>0)&(result[:,:,3]<255)
            self.assertTrue(edge.any())
            self.assertTrue(np.all(result[edge,:3]==240))

    def test_split_validation_and_identity(self):
        selected=le.region(dict(mode='split'))
        self.assertEqual(selected['outside'],le.AFFINE_DEFAULTS)
        for bad in [None,[],dict(dx=float('nan')),dict(dy=True),dict(angle=11),dict(sx=79),dict(sy=121),dict(dx=101),dict(cy=-1),dict(cx=101)]:
            with self.subTest(outside=bad),self.assertRaises(ValueError):
                le.recipe(self.j,dict(region=dict(mode='split',outside=bad)))
        pixels=np.arange(80*64*4,dtype=np.uint8).reshape(80,64,4)
        result,clipped=le.transform(Image.fromarray(pixels),dict(self.r,region=selected),4)
        np.testing.assert_array_equal(result,pixels)
        self.assertFalse(clipped)

    def test_inactive_outside_settings_survive_recipe_roundtrip_without_rendering(self):
        outside=dict(dx=-2.5,dy=1,sx=98,sy=101,angle=-.25,cx=40,cy=60)
        for mode in ('all','inside','outside'):
            with self.subTest(mode=mode):
                configured=le.recipe(self.j,dict(ramp=1,dx=1,region=dict(mode=mode,outside=outside)))
                loaded=le.recipe(self.j,json.loads(json.dumps(configured)))
                self.assertEqual(loaded['region']['outside'],outside)
                without=dict(loaded,region={k:v for k,v in loaded['region'].items() if k!='outside'})
                np.testing.assert_array_equal(le.transform(self.im,loaded,4)[0],le.transform(self.im,without,4)[0])
                loaded['region']['mode']='split'
                self.assertEqual(le.recipe(self.j,loaded)['region']['outside'],outside)
                with self.assertRaises(ValueError):
                    le.recipe(self.j,dict(region=dict(mode=mode,outside=dict(dx=101))))

    def test_split_independent_transforms_and_exact_identity_domains(self):
        yy,xx=np.mgrid[:80,:64].astype(np.float32)
        pixels=np.arange(80*64*4,dtype=np.uint8).reshape(80,64,4)
        pixels[:,:,3]=255;pixels[:4,:4,3]=0
        for shape in ('rect','ellipse'):
            for feather in (0,15):
                selected=le.region(dict(mode='split',shape=shape,feather=feather))
                mask=le.region_influence(selected,xx,yy,64,80)
                with self.subTest(shape=shape,feather=feather):
                    inner=np.asarray(le.transform(Image.fromarray(pixels),dict(self.r,dx=1.5,region=selected),4)[0])
                    np.testing.assert_array_equal(inner[mask==0],pixels[mask==0])
                    self.assertFalse(np.array_equal(inner[mask==1],pixels[mask==1]))
                    selected['outside']['dx']=-1.5
                    outer=np.asarray(le.transform(Image.fromarray(pixels),dict(self.r,region=selected),4)[0])
                    np.testing.assert_array_equal(outer[mask==1],pixels[mask==1])
                    self.assertFalse(np.array_equal(outer[mask==0],pixels[mask==0]))
                    both=np.asarray(le.transform(Image.fromarray(pixels),dict(self.r,dx=1.5,region=selected),4)[0])
                    np.testing.assert_array_equal(both[mask==1],inner[mask==1])
                    np.testing.assert_array_equal(both[mask==0],outer[mask==0])
                    self.assertEqual(both.shape,pixels.shape)
                    # Both settings ramp from the unchanged first frame together.
                    np.testing.assert_array_equal(le.transform(Image.fromarray(pixels),dict(self.r,dx=1.5,region=selected),1)[0],pixels)

    def test_split_equal_settings_are_global_and_feather_is_displacement(self):
        pixels=np.arange(80*64*4,dtype=np.uint8).reshape(80,64,4)
        affine=dict(dx=1.25,dy=-.75,sx=99,sy=102,angle=1.5,cx=40,cy=60)
        global_r=dict(self.r,**affine)
        for shape in ('rect','ellipse'):
            selected=le.region(dict(mode='split',shape=shape,outside=affine,feather=30))
            for index in (2,4):
                result=le.transform(Image.fromarray(pixels),dict(global_r,region=selected),index)[0]
                expected=le.transform(Image.fromarray(pixels),global_r,index)[0]
                np.testing.assert_array_equal(result,expected)
        # At equal mask weights the opposite displacements cancel before sampling.
        # Crossfading the two images would instead leave two displaced white lines.
        pixels=np.zeros((80,64,4),np.uint8);pixels[:,:,3]=255;pixels[:,21,:3]=255
        selected=le.region(dict(mode='split',shape='rect',x=25,y=25,w=50,h=50,feather=34.375,outside=dict(dx=-2)))
        yy,xx=np.mgrid[:80,:64].astype(np.float32)
        self.assertAlmostEqual(float(le.region_influence(selected,xx,yy,64,80)[40,21]),.5)
        result=np.asarray(le.transform(Image.fromarray(pixels),dict(self.r,dx=2,region=selected),4)[0])
        np.testing.assert_array_equal(result[40,21],pixels[40,21])

    def test_split_clipping_uses_actual_outside_field(self):
        selected=le.region(dict(mode='split'))
        self.assertFalse(le.transform(self.im,dict(self.r,dx=40,region=selected),4)[1])
        selected['outside']['dx']=1.5
        self.assertFalse(le.transform(self.im,dict(self.r,dx=40,region=selected),4)[1])
        pixels=np.asarray(self.im).copy();pixels[30:50,60:64]=[240,240,240,255]
        selected['outside']['dx']=10
        self.assertTrue(le.transform(Image.fromarray(pixels),dict(self.r,region=selected),4)[1])
        # A region touching the canvas does not hide clipping of the outside field.
        selected.update(x=0,y=0,w=100,h=100,feather=0)
        self.assertTrue(le.transform(Image.fromarray(pixels),dict(self.r,region=selected),4)[1])

    def test_split_premultiplied_alpha_avoids_hidden_color_fringe(self):
        pixels=np.zeros((80,64,4),np.uint8);pixels[:,:,:3]=[255,0,255]
        pixels[20:60,15:49]=[240,240,240,255]
        selected=le.region(dict(mode='split',shape='ellipse',x=10,y=10,w=80,h=80,feather=30,outside=dict(dx=-.5,sy=99)))
        result=np.asarray(le.transform(Image.fromarray(pixels),dict(self.r,dx=.5,sx=101,region=selected),4)[0])
        edge=(result[:,:,3]>0)&(result[:,:,3]<255)
        self.assertTrue(edge.any());self.assertTrue(np.all(result[edge,:3]==240))
        self.assertEqual(result.shape,pixels.shape)

    def test_edge_validation_and_inactive_settings_roundtrip(self):
        selected=le.region(dict(mode='edge'))
        self.assertEqual((selected['edges'],selected['edge_left'],selected['edge_right']),('both',12,12))
        for bad in [dict(edges='top'),dict(edges=None),dict(edges=[]),dict(edge_left=0),dict(edge_right=46),dict(edge_left=True),dict(edge_right=None),dict(edge_left=float('nan')),dict(edge_right=float('inf')),dict(edge_left='bad')]:
            with self.subTest(bad=bad),self.assertRaises(ValueError):
                le.recipe(self.j,dict(region=dict(mode='edge',**bad)))
        options=dict(edges='right',edge_left=19,edge_right=35)
        geometry=dict(shape='ellipse',x=10,y=15,w=75,h=60,feather=30)
        outside=dict(le.AFFINE_DEFAULTS,dx=5,sx=99)
        for mode in ('all','inside','outside','split','edge'):
            saved=le.recipe(self.j,dict(region=dict(mode=mode,**options,**geometry,outside=outside)))
            loaded=le.recipe(self.j,json.loads(json.dumps(saved)))
            self.assertEqual(saved,loaded)
            for key,expected in dict(**options,**geometry,outside=outside).items():self.assertEqual(loaded['region'][key],expected)
            if mode!='edge':
                without=dict(loaded,region={key:value for key,value in loaded['region'].items() if key not in options})
                np.testing.assert_array_equal(le.transform(self.im,dict(loaded,sx=99),4)[0],le.transform(self.im,dict(without,sx=99),4)[0])
            with self.assertRaises(ValueError):le.region(dict(mode=mode,edge_left=46))

    def test_edge_mask_anchors_pixel_centers_and_extends_outside(self):
        selected=le.region(dict(mode='edge',edge_left=20,edge_right=10))
        xx=np.array([-1,0,10,20,50,90,95,100,101],dtype=np.float32);yy=np.zeros_like(xx)
        np.testing.assert_allclose(le.region_influence(selected,xx,yy,101,80),[0,0,.5,1,1,1,.5,0,0])
        selected['edges']='left'
        np.testing.assert_allclose(le.region_influence(selected,xx,yy,101,80),[0,0,.5,1,1,1,1,1,1])
        selected['edges']='right'
        np.testing.assert_allclose(le.region_influence(selected,xx,yy,101,80),[1,1,1,1,1,1,.5,0,0])

    def test_edge_preserves_complete_locked_rgba_and_global_center_at_each_time(self):
        pixels=np.arange(80*64*4,dtype=np.uint8).reshape(80,64,4)
        # Include opaque, translucent, and zero-alpha pixels with hidden RGB.
        pixels[0,:,3]=0;pixels[-1,:,3]=255
        im=Image.fromarray(pixels);yy,xx=np.mgrid[:80,:64].astype(np.float32)
        affine=dict(self.r,sx=95,dy=.75,angle=.5)
        for edges in ('left','right','both'):
            selected=le.region(dict(mode='edge',edges=edges,edge_left=25,edge_right=20))
            influence=le.region_influence(selected,xx,yy,64,80)
            r=dict(affine,region=selected)
            np.testing.assert_array_equal(le.transform(im,r,1)[0],pixels)
            for index in (2,3,4):
                with self.subTest(edges=edges,index=index):
                    result=np.asarray(le.transform(im,r,index)[0])
                    expected=np.asarray(le.transform(im,affine,index)[0])
                    self.assertEqual(result.shape,pixels.shape)
                    np.testing.assert_array_equal(result[influence==0],pixels[influence==0])
                    np.testing.assert_array_equal(result[influence==1],expected[influence==1])
                    self.assertFalse(np.array_equal(result[influence==1],pixels[influence==1]))
                    if edges!='right':np.testing.assert_array_equal(result[:,0],pixels[:,0])
                    if edges!='left':np.testing.assert_array_equal(result[:,-1],pixels[:,-1])
                    if edges=='left':np.testing.assert_array_equal(result[:,-1],expected[:,-1])
                    if edges=='right':np.testing.assert_array_equal(result[:,0],expected[:,0])

    def test_edge_ignores_rect_outside_and_legacy_protection_controls(self):
        selected=le.region(dict(mode='edge',shape='ellipse',x=0,y=0,w=100,h=100,feather=50,outside=dict(dx=100,sy=80)))
        r=dict(self.r,sx=96,region=selected,protect=True,px=20,py=40,radius=60)
        expected=le.transform(self.im,dict(self.r,sx=96,region=le.region(dict(mode='edge'))),4)
        actual=le.transform(self.im,r,4)
        np.testing.assert_array_equal(actual[0],expected[0]);self.assertEqual(actual[1],expected[1])

    def test_edge_clipping_uses_mask_and_keeps_vertical_and_unlocked_protection(self):
        im=Image.new('RGBA',(64,80),(240,240,240,255))
        selected=le.region(dict(mode='edge'))
        self.assertTrue(le.transform(im,dict(self.r,sx=110),4)[1])
        self.assertFalse(le.transform(im,dict(self.r,sx=110,region=selected),4)[1])
        self.assertTrue(le.transform(im,dict(self.r,sy=110,region=selected),4)[1])
        for side,shift in (('left',-1),('right',1)):
            selected['edges']=side
            # Movement across the anchored side is irrelevant, but the opposite
            # side and vertical boundaries must retain clipping warnings.
            self.assertFalse(le.transform(im,dict(self.r,dx=shift,region=selected),4)[1])
            self.assertTrue(le.transform(im,dict(self.r,dx=-shift,region=selected),4)[1])
            self.assertTrue(le.transform(im,dict(self.r,dy=2,region=selected),4)[1])

    def test_edge_fold_guard_rejects_narrow_transitions_and_allows_safe_changes(self):
        selected=le.region(dict(mode='edge',edge_left=1,edge_right=1))
        unsafe=dict(self.r,sx=80,region=selected)
        np.testing.assert_array_equal(le.transform(self.im,unsafe,1)[0],self.im)
        with self.assertRaisesRegex(ValueError,'加寬左側過渡寬度.*減少寬度修正'):le.transform(self.im,unsafe,4)
        selected['edge_left']=45
        with self.assertRaisesRegex(ValueError,'加寬右側過渡寬度'):le.transform(self.im,unsafe,4)
        selected['edge_right']=45
        self.assertEqual(le.transform(self.im,unsafe,4)[0].size,self.im.size)
        selected.update(edge_left=1,edge_right=1)
        self.assertEqual(le.transform(self.im,dict(unsafe,sx=99),4)[0].size,self.im.size)
        # Even a fold confined between pixel centers must be rejected.
        with self.assertRaisesRegex(ValueError,'過渡區太窄'):
            le.transform(Image.new('RGBA',(2,3)),unsafe,4)
        # A positive but nearly collapsed derivative is unsafe too: for a pure
        # shift its minimum is 1 - 1.5 * dx / transition_width.
        selected['edges']='left'
        near_zero=(1-.0005)*(64-1)*.01/1.5
        with self.assertRaisesRegex(ValueError,'過度擠壓'):
            le.transform(self.im,dict(self.r,dx=near_zero,region=selected),4)

    def test_edge_premultiplied_sampling_and_single_column_canvas(self):
        pixels=np.zeros((80,64,4),np.uint8);pixels[:,:,:3]=[255,0,255]
        pixels[20:60,1:63]=[240,240,240,255]
        selected=le.region(dict(mode='edge',edge_left=25,edge_right=25))
        result=np.asarray(le.transform(Image.fromarray(pixels),dict(self.r,sx=95,dy=.5,region=selected),4)[0])
        partial=(result[:,:,3]>0)&(result[:,:,3]<255)
        self.assertTrue(partial.any());self.assertTrue(np.all(result[partial,:3]==240))
        np.testing.assert_array_equal(result[:,[0,-1]],pixels[:,[0,-1]])
        single=np.arange(80*4,dtype=np.uint8).reshape(80,1,4)
        image,clipped=le.transform(Image.fromarray(single),dict(self.r,dx=50,sy=80,region=selected),4)
        np.testing.assert_array_equal(image,single);self.assertFalse(clipped)

    def test_original_preview_remains_available_for_unsafe_edge_correction(self):
        unsafe=dict(self.r,sx=80,tone=40,region=le.region(dict(mode='edge',edge_left=1,edge_right=1)))
        with patch.object(le,'source',return_value=self.im):
            with self.assertRaisesRegex(ValueError,'過渡區太窄'):
                le.handle('preview',self.j,dict(recipe=unsafe,frame=4))
            preview=le.handle('preview',self.j,dict(recipe=unsafe,frame=4,original=True))
        decoded=Image.open(io.BytesIO(base64.b64decode(preview['image'].split(',',1)[1])))
        np.testing.assert_array_equal(decoded,self.im)
        self.assertFalse(preview['clipped']);self.assertEqual(preview['frame'],4)

    def test_same_name_version_revision_changes_with_new_file(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);versions=root/'loop_edits';versions.mkdir()
            target=versions/'修整_001';target.mkdir();status=target/'status.json'
            status.write_text(json.dumps(dict(state='complete')),encoding='utf-8')
            with patch.object(le.e,'jobdir',return_value=root):
                first=le.handle('load',self.j,{})['versions'][0]['revision']
                self.assertEqual(first,le.handle('load',self.j,{})['versions'][0]['revision'])
                stamp=status.stat().st_mtime_ns;status.unlink()
                status.write_text(json.dumps(dict(state='complete')),encoding='utf-8')
                os.utime(status,ns=(stamp+1000000,stamp+1000000))
                second=le.handle('load',self.j,{})['versions'][0]['revision']
                self.assertNotEqual(first,second)
                (target/'loop_transparent.webm').write_bytes(b'new-export')
                self.assertNotEqual(second,le.handle('load',self.j,{})['versions'][0]['revision'])
    def test_tone_preserves_alpha_endpoints_and_size(self):
        a=np.zeros((1,256,4),np.uint8)
        a[0,:,:3]=np.arange(256)[:,None];a[0,:,3]=np.arange(256)
        im=Image.fromarray(a)
        for tone,contrast in [(50,0),(-50,25),(100,-50),(-100,50)]:
            out=np.array(le.adjust_tone(im,dict(tone=tone,contrast=contrast)))
            self.assertTrue(np.array_equal(out[:,:,3],a[:,:,3]))
            self.assertEqual(out.shape,a.shape)
            self.assertEqual(out[0,0,0],0);self.assertEqual(out[0,-1,0],255)
            self.assertTrue(np.all(np.diff(out[0,:,0].astype(int))>=0))
        self.assertGreater(np.array(le.adjust_tone(im,dict(tone=50)))[0,128,0],128)
        self.assertTrue(np.array_equal(le.adjust_tone(im,{}),a))

    def test_tone_applies_before_ramp_and_old_recipes(self):
        r=le.recipe(self.j,dict(tone=40))
        first,_=le.render(self.im,r,1);last,_=le.render(self.im,r,4)
        self.assertTrue(np.array_equal(first,last))
        self.assertFalse(np.array_equal(first,self.im))
        self.assertEqual(le.recipe(self.j,{})['tone'],0)
        with self.assertRaises(ValueError):le.recipe(self.j,dict(tone=101))

    def test_export_preserves_sources_and_alpha(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);png=root/'transparent_png';png.mkdir();out=root/'version';out.mkdir()
            for f in self.j['frames']:self.im.save(png/f['file'])
            before={p.name:p.read_bytes() for p in png.iterdir()}
            le.RUNS[self.j['id']]=dict(state='running')
            edited=dict(self.r,tone=40,dx=1.5,region=le.region(dict(mode='split',shape='ellipse',x=25,y=25,w=50,h=50,feather=10,outside=dict(dx=-1,sy=99))))
            with patch.object(le.e,'jobdir',return_value=root):
                preview=le.handle('preview',self.j,dict(recipe=edited,frame=4))
                le.export_worker(self.j,edited,out)
            self.assertEqual(le.RUNS[self.j['id']]['state'],'complete',le.RUNS[self.j['id']])
            self.assertEqual(before,{p.name:p.read_bytes() for p in png.iterdir()})
            self.assertEqual(json.loads((out/'recipe.json').read_text())['region'],edited['region'])
            self.assertEqual(len(list((out/'transparent_png').glob('*.png'))),4)
            with Image.open(out/'transparent_png'/self.j['frames'][0]['file']) as frame:
                self.assertTrue(np.array_equal(frame,le.adjust_tone(self.im,dict(tone=40))))
            with Image.open(out/'transparent_png'/self.j['frames'][3]['file']) as frame:
                preview_image=Image.open(io.BytesIO(base64.b64decode(preview['image'].split(',',1)[1])))
                self.assertTrue(np.array_equal(frame,preview_image))
            report=json.loads((out/'verification.json').read_text());self.assertEqual(report['dimensions'],[64,80]);self.assertLessEqual(report['alpha_max_error'],2)

if __name__=='__main__':unittest.main()
