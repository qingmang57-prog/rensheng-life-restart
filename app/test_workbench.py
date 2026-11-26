"""Integration checks use an isolated DB and synthetic footage, never personal files."""
import io
import json
import os
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
import zipfile
import uuid
import base64
from types import SimpleNamespace
from pathlib import Path
from unittest.mock import patch

TEST_ROOT=Path(__file__).resolve().parent/'.test-runtime'
TEST_ROOT.mkdir(exist_ok=True)
TEST_PATH=TEST_ROOT/uuid.uuid4().hex
TEST_PATH.mkdir()
os.environ['IP_DATA_DIR']=str(TEST_PATH)
os.environ['IP_SKIP_LOCAL_CONFIG']='1'
os.environ['IP_DISABLE_LOCAL_ASR']='1'
for name in ('IP_LLM_URL','IP_LLM_MODEL','IP_ASR_URL','IP_RESOLVER_URL'):
    os.environ.pop(name,None)
import app
from db import Store, Conflict
import media

SAMPLE='''# 历史导出
### 三观-01 · 看清条件
- **一句话主张**：判断前先看条件。
- **边界**：条件会变；待他验。
- **关联**：事业-01。
### 事业-01 · 做出尝试
- **一句话主张**：先做一次小尝试。
- **边界**：不派去写稿，口径锁死。
## 修订记录
历史记录须保留在完整来源中。
'''


class StorageTests(unittest.TestCase):
    def test_import_atomic_dedup_provenance(self):
        with tempfile.TemporaryDirectory(dir=TEST_ROOT) as d:
            store=Store(d);result=store.import_brain(SAMPLE,'历史.md')
            self.assertEqual(result['count'],2)
            self.assertFalse(result['duplicate'])
            self.assertTrue(store.import_brain(SAMPLE,'重复.md')['duplicate'])
            self.assertTrue(store.import_brain('\ufeff'+SAMPLE.replace('\n','\r\n'),'Windows重复.md')['duplicate'])
            items={x['legacy_id']:x for x in store.list('knowledge')}
            self.assertEqual(items['三观-01']['links'],['事业-01'])
            self.assertIn('待他验',items['三观-01']['markers'])
            self.assertEqual(items['事业-01']['content_use'],'review')
            self.assertEqual(items['三观-01']['endorsement'],'legacy')
            self.assertEqual(items['三观-01']['validation'],'unverified')
            self.assertEqual(store.get('source',result['source_id'])['text'],SAMPLE)
            self.assertNotIn('历史记录',items['事业-01']['body'])
            item=items['三观-01'];store.patch('knowledge',item['id'],{'validation':'personal_case'},item['revision'])
            with self.assertRaises(Conflict):store.patch('knowledge',item['id'],{'title':'过期写入'},item['revision'])

    def test_cut_interval_validation_and_word_removal(self):
        for invalid in ([{'start_ms':0,'end_ms':float('nan')}],[{'start_ms':-1,'end_ms':100}],[]):
            with self.assertRaises(ValueError):media.validate_segments(invalid,5000)
        words=[{'id':'a','text':'嗯','start_ms':500,'end_ms':900},{'id':'b','text':'啊','start_ms':850,'end_ms':1100}]
        self.assertEqual(media.remove_word_segments(words,['a','b'],5000),[{'start_ms':0,'end_ms':500},{'start_ms':1100,'end_ms':5000}])

    def test_captions_follow_cut_mapping_without_deleted_words(self):
        words=[{'text':'先看','start_ms':100,'end_ms':400},{'text':'嗯','start_ms':600,'end_ms':900},
               {'text':'条件','start_ms':1600,'end_ms':2000}]
        mapping=[{'start_ms':0,'end_ms':500,'output_start_ms':0,'output_end_ms':500},
                 {'start_ms':1500,'end_ms':2200,'output_start_ms':500,'output_end_ms':1200}]
        cues=media.caption_timeline(words,mapping)
        self.assertEqual(''.join(c['text'] for c in cues),'先看条件')
        self.assertEqual(cues[-1]['end_ms'],1000)
        with self.assertRaises(ValueError):media.caption_timeline([],mapping)

    def test_restrictions_in_revision_notes_are_preserved(self):
        with tempfile.TemporaryDirectory(dir=TEST_ROOT) as d:
            store=Store(d)
            store.import_brain('### 账号观-04 · 历史判断\n- **一句话主张**：观点。\n- **边界**：待补。\n## 主链\n关联内容。\n## 修订记录\n口径锁死：不派去写稿。','notes.md')
            item=store.list('knowledge')[0]
            self.assertEqual(item['content_use'],'review')
            self.assertIn('不派去写稿',item['context_notes'])
            self.assertNotIn('## 主链',item['body'])


class APITests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server=app.ThreadingHTTPServer(('127.0.0.1',0),app.Handler)
        cls.thread=threading.Thread(target=cls.server.serve_forever,daemon=True);cls.thread.start()
        cls.base='http://127.0.0.1:'+str(cls.server.server_port)
        cls.fixture=TEST_PATH/'synthetic.mp4'
        if media.binary('ffmpeg') and media.binary('ffprobe'):
            media.run([media.binary('ffmpeg'),'-y','-f','lavfi','-i','color=c=0x29241c:s=320x240:r=20:d=5',
                       '-f','lavfi','-i','sine=frequency=440:sample_rate=48000:duration=5',
                       '-af',"volume='if(between(t,1.2,3.2),0,0.3)':eval=frame",'-c:v','libx264','-pix_fmt','yuv420p','-c:a','aac',cls.fixture])

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown();cls.server.server_close()

    def request(self,path,method='GET',body=None,headers=None,expected=200):
        headers=headers or {}
        if body is not None and not isinstance(body,bytes):body=json.dumps(body,ensure_ascii=False).encode();headers={**headers,'Content-Type':'application/json'}
        req=urllib.request.Request(self.base+path,body,headers,method=method)
        try:res=urllib.request.urlopen(req,timeout=20)
        except urllib.error.HTTPError as exc:res=exc
        with res:
            data=res.read();self.assertEqual(res.status,expected,data[:700])
            return json.loads(data) if 'application/json' in res.headers.get('Content-Type','') else data

    def create_confirmed(self):
        p=self.request('/api/projects','POST',{'title':'联调检查','source_text':'来源原文'},expected=201)
        parts=[{'role':r,'text':t} for r,t in [('hook','你做判断前会问什么？'),('content','先核对条件，再做小尝试。'),('anchor','判断需要边界。'),('quote','先看清，再行动。'),('ending','写下你下一步可以验证的事。')]]
        p=self.request('/api/projects/'+p['id']+'/draft','PATCH',{'parts':parts,'draft_revision':p['draft_revision']})
        return self.request('/api/projects/'+p['id']+'/confirm','POST',{'draft_revision':p['draft_revision']})

    def test_reference_video_before_draft_and_grounded_content_chain(self):
        if not self.fixture.exists():self.skipTest('FFmpeg not installed')
        p=self.request('/api/projects','POST',{'title':'参考视频链路测试'},expected=201)
        raw=self.fixture.read_bytes()
        self.request('/api/uploads','POST',{'project_id':p['id'],'filename':'shot.mp4','size':len(raw)},expected=409)
        session=self.request('/api/uploads','POST',{'project_id':p['id'],'filename':'reference.mp4','size':len(raw),'purpose':'reference','auto_prepare':False},expected=201)
        url='/api/uploads/'+session['id'];self.request(url+'?offset=0','PUT',raw)
        asset=self.request(url+'/complete','POST',{})
        self.assertEqual(asset['purpose'],'reference');self.assertIsNone(asset['script_version_id'])
        details=self.request('/api/projects/'+p['id']);self.assertEqual(details['assets'],[])
        self.assertEqual(len(details['reference_assets']),1)
        transcript={'text':'先看条件，再做判断。','words':[{'id':'w1','text':'先看条件','start_ms':0,'end_ms':1200}],
            'cues':[{'id':'c1','text':'先看条件，再做判断。','start_ms':0,'end_ms':3000}]}
        answer={'analysis':{'summary':'根据条件判断','hook':{'quote':'先看条件','type':'建议','reason':'直接提出方法'},'structure':[],'key_points':[]},
            'items':[{'title':'判断要有条件','claim':'先看适用条件','boundary':'待补','quote':'先看条件'}],
            'parts':[{'role':r,'text':'测试原创稿件 '+r} for r,_ in app.ROLES]}
        with patch('providers.available',return_value={'llm':True,'asr':True}),patch('providers.asr',return_value=transcript),patch('providers.llm_json',return_value=answer):
            job=self.request('/api/projects/'+p['id']+'/prepare-content','POST',{'asset_id':asset['id']},expected=202)
            deadline=time.time()+10
            while time.time()<deadline:
                current=self.request('/api/jobs/'+job['id'])
                if current['status'] not in ('queued','running'):break
                time.sleep(.05)
        self.assertEqual(current['status'],'succeeded',current.get('error'))
        details=self.request('/api/projects/'+p['id'])
        self.assertEqual(details['source']['resolution']['opening']['precision'],'word')
        self.assertTrue(all(part['text'] for part in details['draft']))
        self.assertIsNone(details['confirmed_version_id'])
        candidate=details['knowledge_candidates'][0]
        self.assertEqual(candidate['endorsement'],'reference_only');self.assertEqual(candidate['validation'],'unverified')
        self.request('/api/projects/'+p['id']+'/prepare-video','POST',{'asset_id':asset['id']},expected=409)

    def test_model_preparation_never_overwrites_existing_draft(self):
        p=self.create_confirmed();source=app.require('source',p['source_id'])
        answer={'analysis':{'summary':'测试'},'items':[],
            'parts':[{'role':r,'text':'模型建议 '+r} for r,_ in app.ROLES]}
        job=app.STORE.put('job',{'project_id':p['id'],'kind':'test','status':'running'})
        with patch('providers.available',return_value={'llm':True}),patch('providers.llm_json',return_value=answer):
            result=app.prepare_content(p,source,job['id'])
        self.assertFalse(result['draft_filled'])
        self.assertEqual(app.require('project',p['id'])['draft'],p['draft'])

    def test_real_caption_packaging_review_and_complete_delivery(self):
        if not self.fixture.exists():self.skipTest('FFmpeg not installed')
        p=self.create_confirmed();raw=self.fixture.read_bytes()
        session=self.request('/api/uploads','POST',{'project_id':p['id'],'filename':'caption-test.mp4','size':len(raw)},expected=201)
        url='/api/uploads/'+session['id'];self.request(url+'?offset=0','PUT',raw)
        asset=self.request(url+'/complete','POST',{})
        transcript={'text':'先看条件再做判断','words':[{'id':'a','text':'先看条件','start_ms':100,'end_ms':500},
            {'id':'b','text':'再做判断','start_ms':3800,'end_ms':4500}],'cues':[]}
        with patch('providers.available',return_value={'asr':True,'llm':False}),patch('providers.asr',return_value=transcript):
            job=self.request('/api/projects/'+p['id']+'/prepare-video','POST',{'asset_id':asset['id']},expected=202)
            deadline=time.time()+35
            while time.time()<deadline:
                result=self.request('/api/jobs/'+job['id'])
                if result['status'] not in ('queued','running'):break
                time.sleep(.1)
        self.assertEqual(result['status'],'succeeded',result.get('error'))
        artifact=result['result']['artifact'];self.assertEqual(artifact['kind'],'packaged_video')
        self.assertTrue(artifact['info']['audio'])
        self.assertLess(artifact['caption_cues'][-1]['end_ms'],4500)
        self.request('/api/projects/'+p['id']+'/export?include_video=1',expected=404)
        self.request('/api/projects/'+p['id']+'/review','POST',{'artifact_id':artifact['id']})
        bundle=self.request('/api/projects/'+p['id']+'/export?include_video=1')
        with zipfile.ZipFile(io.BytesIO(bundle)) as z:
            self.assertTrue({'video.mp4','cover.jpg','captions.srt','publication.json','script.md'}.issubset(z.namelist()))
        self.request('/api/projects/'+p['id']+'/revise-captions','POST',{'artifact_id':artifact['id'],'edits':[{'id':'not-real','text':'改字'}]},expected=400)
        first=artifact['caption_cues'][0]
        change=self.request('/api/projects/'+p['id']+'/revise-captions','POST',{'artifact_id':artifact['id'],'edits':[{'id':first['id'],'text':'合成字幕校对'}]},expected=202)
        deadline=time.time()+35
        while time.time()<deadline:
            revised=self.request('/api/jobs/'+change['id'])
            if revised['status'] not in ('queued','running'):break
            time.sleep(.1)
        self.assertEqual(revised['status'],'succeeded',revised.get('error'))
        corrected=revised['result']['artifact']
        self.assertEqual(corrected['caption_cues'][0],{**first,'text':'合成字幕校对'})
        self.assertEqual(app.require('artifact',artifact['id'])['caption_cues'][0],first)
        self.assertLess(abs(corrected['info']['duration_ms']-artifact['info']['duration_ms']),80)
        self.assertIsNone(self.request('/api/projects/'+p['id'])['reviewed_artifact_id'])

    def test_guards_and_honest_provider_states(self):
        self.request('/api/profile','PATCH',{},headers={'Origin':'https://unrelated.invalid'},expected=403)
        self.request('/api/bootstrap',headers={'Host':'unrelated.invalid'},expected=403)
        self.request('/api/publish/status',expected=200)
        self.request('/api/publish/submit','POST',{},expected=400)
        p=self.create_confirmed()
        self.request('/api/publish/plan','POST',{'project_id':p['id']},expected=400)
        self.request('/api/publish/submit','POST',{'project_id':p['id'],'confirm':True},expected=409)
        self.request('/api/projects/'+p['id']+'/generate','POST',{},expected=409)
        html=self.request('/');self.assertIn('我的经验库'.encode(),html)
        self.assertIn(b'route();',self.request('/app.js'))
        self.request('/%2e%2e/app.py',expected=403)

    def test_version_snapshot_stale_write_and_export(self):
        p=self.create_confirmed();key=p['id'];version=app.STORE.get('script_version',p['confirmed_version_id'])
        self.request('/api/projects/'+key+'/source','PATCH',{'text':'后来修改来源','revision':1})
        self.assertEqual(version['source_snapshot'],'来源原文')
        raw=self.request('/api/projects/'+key+'/export');z=zipfile.ZipFile(io.BytesIO(raw))
        self.assertIn('先看清，再行动。',z.read('script.md').decode())
        changed=self.request('/api/projects/'+key+'/draft','PATCH',{'parts':p['draft'],'draft_revision':p['draft_revision']})
        self.request('/api/projects/'+key+'/draft','PATCH',{'parts':p['draft'],'draft_revision':p['draft_revision']},expected=409)
        self.request('/api/projects/'+key+'/export',expected=409)
        app.attach_if_current(key,p['confirmed_version_id'],{'latest_artifact_id':'stale-result'})
        self.assertNotEqual(app.STORE.get('project',key).get('latest_artifact_id'),'stale-result')

    def test_knowledge_creation_and_lifecycle(self):
        x=self.request('/api/knowledge','POST',{'title':'用实际条件作判断','claim':'先找适用范围。'},expected=201)
        self.assertEqual(x['validation'],'unverified')
        x=self.request('/api/knowledge/'+x['id'],'PATCH',{'lifecycle':'archived','endorsement':'accepted','revision':x['revision']})
        self.assertEqual(x['lifecycle'],'archived')
        answer=self.request('/api/brain/ask','POST',{'question':'适用范围是什么'})
        self.assertNotIn(x['id'],[r['id'] for r in answer['references']])
        listing=self.request('/api/knowledge?category=not-present')
        self.assertEqual(listing['total'],0)
        self.assertGreaterEqual(listing['category_counts'].get('认知底座',0),1)

    def test_auto_intake_pipeline_and_manual_edit_invalidation(self):
        link='https://www.douyin.com/jingxuan?modal_id=7693550505402840741'
        resolution=app.video_intake.normalize({'title':'合成接口测试','cues':[{'text':'先看条件。','start_ms':0,'end_ms':1500}],
            'metadata':{'metrics':{'likes':0,'favorites':23}}},app.video_intake.share_info(link),'test_fixture')
        with patch('providers.resolve_source',return_value=resolution):
            p=self.request('/api/projects','POST',{'url':link,'auto_resolve':True},expected=201)
            deadline=time.time()+10
            while time.time()<deadline:
                details=self.request('/api/projects/'+p['id'])
                if details['jobs'] and details['jobs'][0]['status'] not in ('queued','running'):break
                time.sleep(.1)
        self.assertEqual(details['title'],'合成接口测试')
        self.assertEqual(details['source']['text'],'先看条件。')
        self.assertEqual(details['source']['resolution']['metadata']['metrics']['favorites'],23)
        self.assertEqual(details['jobs'][0]['status'],'needs_action')
        self.assertEqual(details['next_action']['step'],'script')
        source=self.request('/api/projects/'+p['id']+'/source','PATCH',{'text':'手动更正后的正文。','revision':details['source']['revision']})
        self.assertEqual(source['resolution']['cues'],[])
        self.assertEqual(source['resolution']['opening']['precision'],'unavailable')

    def test_real_upload_resume_cut_and_range(self):
        if not self.fixture.exists():self.skipTest('FFmpeg not installed')
        p=self.create_confirmed();raw=self.fixture.read_bytes()
        session=self.request('/api/uploads','POST',{'project_id':p['id'],'filename':'synthetic.mp4','size':len(raw)},expected=201)
        url='/api/uploads/'+session['id'];middle=len(raw)//2
        self.request(url+'?offset=0','PUT',raw[:middle]);self.request(url+'?offset=0','PUT',raw[middle:],expected=409)
        resumed=self.request(url);self.assertEqual(resumed['offset'],middle)
        self.request(url+'?offset='+str(middle),'PUT',raw[middle:])
        asset=self.request(url+'/complete','POST',{})
        self.assertEqual(self.request(url+'/complete','POST',{})['id'],asset['id'])
        self.request(url+'?offset=0','PUT',b'x',expected=409)
        job=self.request('/api/projects/'+p['id']+'/cut','POST',{'asset_id':asset['id'],'mode':'natural'},expected=202)
        deadline=time.time()+25
        while time.time()<deadline:
            current=self.request('/api/jobs/'+job['id'])
            if current['status'] not in ('queued','running'):break
            time.sleep(.1)
        self.assertEqual(current['status'],'succeeded',current.get('error'))
        artifact=current['result']['artifact'];duration=artifact['info']['duration_ms']
        self.assertGreater(duration,2600);self.assertLess(duration,3900)
        plan=current['result']['plan'];self.assertGreater(len(plan['removed']),0)
        self.assertEqual(len(self.request('/api/artifacts/'+artifact['id'],headers={'Range':'bytes=0-99'},expected=206)),100)
        self.request('/api/projects/'+p['id']+'/review','POST',{'artifact_id':artifact['id']})
        self.assertEqual(self.request('/api/projects/'+p['id'])['stage'],'ready_to_export')
        transcript=app.STORE.put('transcript',{'project_id':p['id'],'asset_id':asset['id'],'words':[{'id':'w','text':'嗯','start_ms':100,'end_ms':200}]})
        self.request('/api/projects/'+p['id']+'/remove-words','POST',{'transcript_id':transcript['id'],'word_ids':['not-real']},expected=400)
        word_job=self.request('/api/projects/'+p['id']+'/remove-words','POST',{'transcript_id':transcript['id'],'word_ids':['w']},expected=202)
        deadline=time.time()+25
        while time.time()<deadline:
            result=self.request('/api/jobs/'+word_job['id'])
            if result['status'] not in ('queued','running'):break
            time.sleep(.1)
        self.assertEqual(result['status'],'succeeded',result.get('error'))
        self.assertEqual(result['result']['plan']['segments'],[{'start_ms':0,'end_ms':100},{'start_ms':200,'end_ms':asset['info']['duration_ms']}])

    def test_motion_adapter_gets_new_delivery_directory(self):
        p=self.create_confirmed()
        artifact=app.STORE.put('artifact',{'project_id':p['id'],'script_version_id':p['confirmed_version_id'],'kind':'clean_video','path':str(self.fixture)})
        brief={'groups':[{'id':'B1','title':'接口测试'}],'basic':{'keyword':'测试','explanation':'仅测试目录契约'},'presenterFace':{'cx':.5,'cy':.5,'h':.3}}
        checked=[]
        def fake_run(args,timeout=0):
            output=Path(args[args.index('--out')+1]);bp=Path(args[args.index('--brief')+1])
            self.assertFalse(output.exists(),'pack.py refuses pre-existing directories without manifests')
            self.assertTrue(bp.is_file());self.assertNotEqual(bp.parent,output)
            checked.append(True);output.mkdir()
            (output/'preview.png').write_bytes(base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aS1UAAAAASUVORK5CYII='))
            return SimpleNamespace(stdout='mock adapter output',stderr='')
        with patch('app.capabilities',return_value={'motion_installed':True}),patch('media.run',side_effect=fake_run):
            job=self.request('/api/projects/'+p['id']+'/motion','POST',{'artifact_id':artifact['id'],'brief':brief},expected=202)
            deadline=time.time()+10
            while time.time()<deadline:
                result=self.request('/api/jobs/'+job['id'])
                if result['status'] not in ('queued','running'):break
                time.sleep(.1)
        self.assertEqual(result['status'],'succeeded',result.get('error'))
        self.assertEqual(checked,[True])

    def test_audio_tail_repair_preserves_video_and_rejects_long_gap(self):
        if not self.fixture.exists():self.skipTest('FFmpeg not installed')
        short=TEST_PATH/'voice-short.m4a'
        media.run([media.binary('ffmpeg'),'-y','-i',self.fixture,'-vn','-af','atrim=duration=4.94','-c:a','aac',short])
        output=TEST_PATH/'tail-repaired.mp4'
        result=media.finalize_render_audio(self.fixture,short,output)
        self.assertLessEqual(result['tail_padding_ms'],250)
        self.assertGreater(result['tail_padding_ms'],0)
        self.assertFalse(result['video_reencoded'])
        self.assertTrue(result['output_info']['audio'])
        digest=lambda path:media.run([media.binary('ffmpeg'),'-v','error','-i',path,'-map','0:v:0','-c','copy','-f','hash','-hash','sha256','-']).stdout.strip()
        self.assertEqual(digest(self.fixture),digest(output))
        too_short=TEST_PATH/'voice-too-short.m4a'
        media.run([media.binary('ffmpeg'),'-y','-i',self.fixture,'-vn','-af','atrim=duration=1','-c:a','aac',too_short])
        with self.assertRaises(ValueError):media.finalize_render_audio(self.fixture,too_short,TEST_PATH/'should-not-mux.mp4')


if __name__=='__main__':
    try:unittest.main(verbosity=2)
    finally:app.POOL.shutdown(wait=True)
