"""Generated shapes only: shared multi-animation endpoints and real exports."""
import base64
import copy
import hashlib
import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np
from PIL import Image, ImageDraw

import transitions as t


class TransitionSeamTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.root=Path(self.tmp.name)
        self.jobs=self.root/'jobs'
        self.jobs.mkdir()
        self.patches=[patch.object(t.e,'ROOT',self.root),patch.object(t.e,'JOBS',self.jobs),
            patch.dict(t.e.PATHS,{},clear=True),patch.dict(t.e.ACTIVE,{},clear=True),
            patch.dict(t.le.RUNS,{},clear=True),patch.dict(t.RUNS,{},clear=True)]
        for item in self.patches:item.start()
        self.addCleanup(self.cleanup)
        self.project=t.handle('create',dict(name='Generated four-state loop'))
        self.keys=[str(i)*32 for i in range(1,5)]
        for number,key in enumerate(self.keys):
            jid='abcdef'[number]*32
            folder=self.jobs/jid/'transparent_png'
            folder.mkdir(parents=True)
            frames=[]
            for index in range(1,11):
                image=Image.new('RGBA',(64,80),(210,70,180,0))
                draw=ImageDraw.Draw(image)
                x=20+number*2+round(2*np.sin(index*.5))
                draw.rounded_rectangle((x,15,x+22,64),radius=7,fill=(130+number*15,110+number*7,90+number*10,255))
                draw.rectangle((x+4,25,x+8,29),fill=(30,30,30,255))
                draw.line((x+3,48+number,x+17,46+number),fill=(240,240,240,255),width=2)
                draw.line((x-1,24,x-1,56),fill=(120,120,120,90),width=1)
                name=f'frame_{index:08d}.png'
                image.save(folder/name)
                frames.append(dict(file=name,status='done'))
            t.e.atomic_json(folder.parent/'job.json',dict(id=jid,name=f'Generated state {number}',state='complete',
                frames=frames,dimensions=[64,80],config=dict(fps=24 if number<3 else 12),paths=dict(png='transparent_png')))
            self.project['clips'].append(dict(key=key,job_id=jid,version='original',label=f'State {number}',
                start=1,end=10,head_frames=3,tail_frames=3,tone=number*2,contrast=number,
                head=dict(dx=.3*number),tail=dict(dx=-.2*number)))
        self.project['sequence']=list(self.keys)
        self.project['shared_tone']=dict(tone=12,contrast=4)
        self.project=t.handle('save',dict(project=self.project))

    def cleanup(self):
        t.ts.lc.clear_cache()
        for item in reversed(self.patches):item.stop()
        self.tmp.cleanup()

    def build(self,project=None,**args):
        return t.handle('build_seams',dict(project=project or self.project,**args))['project']

    def png(self,value):
        return Image.open(io.BytesIO(base64.b64decode(value.split(',',1)[1]))).convert('RGBA')

    def test_distinct_shared_anchors_exact_pairs_middle_and_no_double_tone(self):
        closed=self.build()
        self.assertEqual(len({link['anchor_id'] for link in closed['seam_closure']['links']}),4)
        self.assertEqual(len({link['sha256'] for link in closed['seam_closure']['links']}),4)
        project,sources=t.validate_project(closed)
        by_key={clip['key']:clip for clip in project['clips']}
        for a,b in t.ts.pairs(project):
            ca,cb=by_key[a],by_key[b]
            expected=t._render_base_frame(cb,sources[b],cb['start'],project=project)[0]
            left=t.render_frame(ca,sources[a],ca['end'],project=project)[0]
            right=t.render_frame(cb,sources[b],cb['start'],project=project)[0]
            np.testing.assert_array_equal(left,expected)
            np.testing.assert_array_equal(right,expected)
            comparison=t.handle('compare',dict(project=closed,a=a,b=b))
            self.assertTrue(comparison['seam_closure']['enabled'])
            np.testing.assert_array_equal(self.png(comparison['left']),self.png(comparison['right']))
            for index in (3,4,5,6,7,8):
                base=t._render_base_frame(ca,sources[a],index,project=project)[0]
                np.testing.assert_array_equal(t.render_frame(ca,sources[a],index,project=project)[0],base)
            adjacent=t.render_frame(ca,sources[a],9,project=project)[0]
            self.assertEqual(adjacent.size,(64,80))
        self.assertNotEqual(t.clip_signature(project['clips'][0],sources[self.keys[0]],project),t.clip_signature(project['clips'][0],sources[self.keys[0]],self.project))

    def test_roundtrip_windows_references_and_disabled_legacy(self):
        a,b=self.keys[:2]
        project=self.build(references={a+'>'+b:'a_tail'})
        saved=t.handle('save',dict(project=project))
        loaded=t.handle('load',dict(project_id=project['id']))['project']
        self.assertEqual(saved['seam_closure'],loaded['seam_closure'])
        link=loaded['seam_closure']['links'][0]
        self.assertEqual(link['reference'],'a_tail')
        loaded['clips'][0]['head_frames']=4
        loaded['clips'][0]['tail_frames']=4
        t.validate_project(loaded)
        loaded['clips'][0]['label']='Renamed'
        t.validate_project(loaded)
        loaded['seam_closure']['enabled']=False
        loaded['sequence_version']=1
        loaded['sequence']=[a,b,a]
        result,sources=t.validate_project(loaded)
        self.assertEqual(result['sequence'],[a,b,a])
        self.assertEqual(result['seam_closure']['links'],saved['seam_closure']['links'])
        clip=result['clips'][0]
        np.testing.assert_array_equal(t.render_frame(clip,sources[a],1,project=result)[0],t._render_base_frame(clip,sources[a],1,project=result)[0])
        with self.assertRaisesRegex(ValueError,'完整固定循環'):
            self.build(result)

    def test_stale_base_tone_order_range_and_missing_tampered_anchors(self):
        project=self.build()
        modifications=[lambda p:p['clips'][0].update(tone=7),lambda p:p['clips'][0]['tail'].update(dx=3),
            lambda p:p['clips'][0].update(start=2),lambda p:p.update(sequence=list(reversed(self.keys))),
            lambda p:p['shared_tone'].update(tone=9)]
        for edit in modifications:
            stale=copy.deepcopy(project);edit(stale)
            with self.assertRaisesRegex(ValueError,'重新建立共同接點'):
                t.validate_project(stale)
            rebuilt=self.build(stale)
            t.validate_project(rebuilt)
            self.assertNotEqual(rebuilt['seam_closure']['binding'],project['seam_closure']['binding'])
        link=project['seam_closure']['links'][0]
        anchor=t.projectdir(project['id'])/'anchors'/(link['anchor_id']+'.png')
        original=anchor.read_bytes()
        anchor.write_bytes(original+b'changed')
        with self.assertRaisesRegex(ValueError,'基準已被修改'):
            t.validate_project(project)
        anchor.unlink()
        with self.assertRaisesRegex(ValueError,'基準檔案遺失'):
            t.validate_project(project)
        disabled=copy.deepcopy(project);disabled['seam_closure']['enabled']=False
        t.validate_project(disabled)
        t.validate_project(self.build(project))

    def test_source_race_during_build_and_anchor_race_before_export(self):
        original=t.ts.save_anchor
        counter=[0]
        def mutate(root,image):
            value=original(root,image)
            counter[0]+=1
            if counter[0]==4:
                path=next((self.jobs/('a'*32)/'transparent_png').glob('*.png'))
                Image.new('RGBA',(64,80),(50,50,50,255)).save(path)
            return value
        with patch.object(t.ts,'save_anchor',side_effect=mutate):
            with self.assertRaisesRegex(ValueError,'處理期間'):
                self.build()
        project=self.build()
        canonical,sources=t.validate_project(project)
        link=project['seam_closure']['links'][0]
        anchor=t.projectdir(project['id'])/'anchors'/(link['anchor_id']+'.png')
        anchor.write_bytes(anchor.read_bytes()+b'changed')
        output=t._version_dir(project['id'],'v001');output.mkdir(parents=True)
        t.RUNS[project['id']]=dict(state='running',version='v001')
        t.export_worker(canonical,sources,'v001',t.project_signature(canonical,sources))
        self.assertEqual(t.RUNS[project['id']]['state'],'error')
        self.assertIn('基準已被修改',t.RUNS[project['id']]['phase'])
        self.assertFalse((output/'manifest.json').exists())

    def test_real_four_clip_export_all_joints_neighbors_and_source_unchanged(self):
        project=self.build()
        canonical,sources=t.validate_project(project)
        originals={path:hashlib.sha256(path.read_bytes()).hexdigest() for path in self.jobs.glob('*/transparent_png/*.png')}
        pid,version=project['id'],'v001'
        output=t._version_dir(pid,version);output.mkdir(parents=True)
        t.RUNS[pid]=dict(state='running',version=version)
        t.export_worker(canonical,sources,version,t.project_signature(canonical,sources))
        self.assertEqual(t.RUNS[pid]['state'],'complete',t.RUNS[pid].get('phase'))
        manifest=t._read_json(output/'manifest.json')
        verification=manifest['seam_verification']
        self.assertTrue(verification['all_equal'])
        self.assertEqual(len(verification['pairs']),4)
        self.assertEqual([pair['can_skip_duplicate_head'] for pair in verification['pairs']],[True,True,False,False])
        for pair in verification['pairs']:
            self.assertEqual(pair['max_channel_error'],0)
            self.assertEqual(pair['changed_pixels'],0)
            self.assertIn('head_adjacent_delta',pair)
            self.assertIn('tail_adjacent_delta',pair)
        for link in project['seam_closure']['links']:
            self.assertEqual(hashlib.sha256((output/'anchors'/(link['anchor_id']+'.png')).read_bytes()).hexdigest(),link['sha256'])
        for item in manifest['clips']:
            self.assertEqual(item['dimensions'],[64,80])
            self.assertEqual(item['frames'],10)
            self.assertLessEqual(item['alpha_max_error'],2)
            clip=next(clip for clip in project['clips'] if clip['key']==item['key'])
            for index in range(1,11):
                with Image.open(t.projectdir(pid)/(item['png_pattern']%index)) as actual:
                    expected=t.render_frame(clip,sources[clip['key']],index,project=project)[0]
                    np.testing.assert_array_equal(actual,expected)
        for path,digest in originals.items():
            self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(),digest)
        self.assertEqual(t._versions(pid)[0]['seam_verification'],verification)
        damaged=t.projectdir(pid)/(manifest['clips'][0]['png_pattern']%1)
        with Image.open(damaged) as image:
            pixels=np.array(image.convert('RGBA'))
        pixels[0,0,0]^=1  # Even invisible RGB must be verified, not only alpha.
        Image.fromarray(pixels).save(damaged)
        with self.assertRaisesRegex(ValueError,'實際 PNG 首尾不一致'):
            t.ts.verify_exports(canonical,manifest['clips'],t.projectdir(pid),sources)


if __name__=='__main__':unittest.main()
