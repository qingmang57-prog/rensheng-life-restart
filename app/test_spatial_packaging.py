import copy, json, tempfile, unittest
from pathlib import Path
from unittest.mock import patch
from db import Store, Conflict
import smart_packaging as sp
import spatial_packaging as core
class SpatialTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.store=Store(self.tmp.name)
        self.video=Path(self.tmp.name)/'source.bin';self.video.write_bytes(b'technical-source-v1')
        self.project=self.store.put('project',{'title':'空间技术输入','confirmed_version_id':'v1','draft_changed':False})
        self.clean=self.store.put('artifact',{'project_id':self.project['id'],'script_version_id':'v1','kind':'clean_video',
            'asset_id':'a1','path':str(self.video),'info':{'duration_ms':16000,'width':1920,'height':1080},
            'mapping':[{'start_ms':0,'end_ms':16000,'output_start_ms':0,'output_end_ms':16000}]})
        self.tr=self.store.put('transcript',{'asset_id':'a1','words':[],'cues':[
            {'start_ms':0,'end_ms':4000,'text':'先把素材放进同一空间'},
            {'start_ms':4000,'end_ms':8000,'text':'再让镜头沿路线向前走'},
            {'start_ms':8000,'end_ms':12000,'text':'最后在原位强调这一点'},
            {'start_ms':12000,'end_ms':16000,'text':'更换内容仍然复用相同核心'}]})
    def tearDown(self):self.tmp.cleanup()
    def plan(self):return sp.ensure_plan(self.store,self.project,self.clean,core.STYLE)
    def test_bundle_hashes(self):self.assertTrue(core.capabilities()['ready'])
    def test_manual_landscape_selection_and_reopen(self):
        p=self.plan();self.assertEqual(p['selected']['id'],core.STYLE)
        self.assertEqual(p['id'],sp.ensure_plan(self.store,self.project,self.clean)['id'])
    def test_not_in_auto_rank(self):
        r,_=sp.match_styles([{'text':'知识流程'}],16000,{},available=sp.styles())
        self.assertNotIn(core.STYLE,[x['id'] for x in r])
    def test_current_validates(self):
        p=self.plan();self.assertEqual(sp.validate_plan(self.store,self.project,p)['id'],self.clean['id'])
    def test_engine_change_invalidates(self):
        p=self.plan();binding=dict(core.engine_binding(),bundleDigest='changed')
        with patch.object(core,'engine_binding',return_value=binding):
            with self.assertRaises(Conflict):sp.validate_plan(self.store,self.project,p)
    def test_file_replacement_invalidates(self):
        p=self.plan();self.video.write_bytes(b'technical-source-v2')
        with self.assertRaises(Conflict):sp.validate_plan(self.store,self.project,p)
    def test_options_require_current_plan_and_invalidate(self):
        p=self.plan();cues=p['cues']
        opts={'groups':[{'kind':'process','cue_ids':[c['id'] for c in cues[:3]]}],'emphasis_cue_ids':[cues[2]['id']]}
        q=sp.ensure_plan(self.store,self.project,self.clean,core.STYLE,opts,p['id'])
        self.assertNotEqual(p['fingerprint'],q['fingerprint'])
        self.assertEqual(q['spatial_input']['contentScenes'][0]['kind'],'process')
        with self.assertRaises(Conflict):sp.validate_plan(self.store,self.project,p)
    def test_foreign_cue_and_capacity_rejected(self):
        p=self.plan()
        with self.assertRaises(ValueError):core.validate_options({'groups':[{'kind':'process','cue_ids':['foreign',p['cues'][0]['id']]}]},p['cues'])
    def test_deleted_words_stay_deleted(self):
        transcript={'words':[{'start_ms':0,'end_ms':1000,'text':'保留'},{'start_ms':1000,'end_ms':2000,'text':'删除'},{'start_ms':2000,'end_ms':3000,'text':'结尾'}]}
        mapping=[{'start_ms':0,'end_ms':1000,'output_start_ms':0},{'start_ms':2000,'end_ms':3000,'output_start_ms':1000}]
        cues=sp.cut_cues(transcript,mapping,2000);packet=core.normalize_input(cues,{},2)
        self.assertNotIn('删除',''.join(c['text'] for c in packet['captions']))
    def test_metadata_does_not_truncate_text(self):
        cues=[{'id':'a','start_ms':0,'end_ms':8000,'text':'这是一条超过十二字和二十二字限制仍然必须完整保留的技术测试原句'}]
        packet=core.normalize_input(cues,{},8)
        self.assertEqual(packet['contentScenes'][0]['blocks'][0]['text'],cues[0]['text'])
    def test_other_style_switch_rejects_old_plan(self):
        p=self.plan();styles=[s for s in sp.styles() if not s.get('spatial_template')]
        self.assertTrue(styles)
        sp.ensure_plan(self.store,self.project,self.clean,styles[0]['id'])
        with self.assertRaises(Conflict):sp.validate_plan(self.store,self.project,p)
    def test_wrong_project_rejected(self):
        p=self.plan()
        with self.assertRaises(KeyError):sp.validate_plan(self.store,dict(self.project,id='foreign'),p)
    def test_sparse_group_rejects_omitted_middle_cue(self):
        p=self.plan()
        with self.assertRaisesRegex(ValueError,'连续'):
            core.validate_options({'groups':[{'kind':'process','cue_ids':[p['cues'][0]['id'],p['cues'][2]['id']]}]},p['cues'])
if __name__=='__main__':unittest.main()
