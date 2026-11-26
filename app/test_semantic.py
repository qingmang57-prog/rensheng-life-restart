"""Semantic clean-cut regressions: grounded word-ID deletions, real A/V cuts, caption sync."""
import json
import os
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from unittest.mock import patch

TEST_ROOT=Path(__file__).resolve().parent/'.test-runtime'
TEST_ROOT.mkdir(exist_ok=True)
TEST_PATH=TEST_ROOT/uuid.uuid4().hex
TEST_PATH.mkdir()
os.environ.setdefault('IP_DATA_DIR',str(TEST_PATH))
os.environ['IP_SKIP_LOCAL_CONFIG']='1'
os.environ['IP_DISABLE_LOCAL_ASR']='1'
for name in ('IP_LLM_URL','IP_LLM_MODEL','IP_ASR_URL','IP_RESOLVER_URL'):
    os.environ.pop(name,None)
import app
import media

WORDS=[{'id':'a','text':'先看','start_ms':100,'end_ms':500},
       {'id':'b','text':'嗯','start_ms':600,'end_ms':900},
       {'id':'c','text':'再做判断','start_ms':3800,'end_ms':4500},
       {'id':'d','text':'条件','start_ms':4600,'end_ms':4900}]


def proposal(deletions):
    return {'deletions':deletions}


class PlanValidationTests(unittest.TestCase):
    def test_model_may_only_reference_existing_word_ids(self):
        answer=proposal([{'word_ids':['a','ghost'],'type':'repeat','repair_word_ids':['c'],'reason':'x','kept_text':'','needs_review':False}])
        deletions,skipped=app.semantic_plan_valid(WORDS,answer,5000)
        self.assertEqual(deletions,[])
        self.assertIn('不存在的词ID',skipped[0]['reason'])

    def test_global_shape_still_rejected_wholesale(self):
        for bad in ({'deletions':'none'},
                    proposal([{'word_ids':['a'],'type':'repeat','repair_word_ids':['c']}]*41)):
            with self.assertRaises(ValueError):app.semantic_plan_valid(WORDS,bad,5000)

    def test_bad_entries_skipped_per_item_not_wholesale(self):
        for bad in (proposal([{'word_ids':['a'],'type':'emphasis'}]),
                    proposal([{'word_ids':'a','type':'repeat'}]),
                    proposal([{'word_ids':[],'type':'repeat'}]),
                    proposal(['junk'])):
            deletions,skipped=app.semantic_plan_valid(WORDS,bad,5000)
            self.assertEqual(deletions,[])
            self.assertEqual(len(skipped),1)

    def test_intervals_derived_from_source_words_and_deduplicated(self):
        answer=proposal([
            {'word_ids':['b'],'type':'filler','reason':'有声水词','kept_text':'先看条件再做判断','needs_review':False},
            {'word_ids':['d','b'],'type':'repeat','repair_word_ids':['c'],'reason':'重复','kept_text':'','needs_review':True},
            {'word_ids':['zz'],'type':'repeat','repair_word_ids':['c'],'reason':'','kept_text':'','needs_review':False}])
        # 'zz' does not exist: that single entry is dropped and recorded; valid entries survive.
        deletions,skipped=app.semantic_plan_valid(WORDS,answer,5000)
        self.assertEqual(len(skipped),1)
        self.assertEqual([d['id'] for d in deletions],['del_1','del_2'])
        self.assertEqual(deletions[0]['intervals'],[[600,900]])
        self.assertEqual(deletions[0]['text'],'嗯')
        self.assertFalse(deletions[0]['needs_review'])
        # 'b' already consumed by del_1; del_2 keeps only 'd'. Cross-deletion overlap cannot double-cut.
        self.assertEqual(deletions[1]['word_ids'],['d'])
        self.assertEqual(deletions[1]['start_ms'],4600);self.assertEqual(deletions[1]['end_ms'],4900)
        self.assertTrue(deletions[1]['needs_review'])
        self.assertEqual(deletions[1]['repair_text'],'再做判断')

    def test_repeat_or_restart_requires_surviving_repair(self):
        # Disfluency rule (reparandum/interregnum/repair): never delete without a kept complete restatement.
        for bad in (proposal([{'word_ids':['d'],'type':'repeat','reason':'r'}]),
                    proposal([{'word_ids':['d'],'type':'restart','repair_word_ids':[],'reason':'r'}]),
                    proposal([{'word_ids':['d'],'type':'repeat','repair_word_ids':['ghost'],'reason':'r'}]),
                    proposal([{'word_ids':['d'],'type':'repeat','repair_word_ids':['d'],'reason':'r'}])):
            deletions,skipped=app.semantic_plan_valid(WORDS,bad,5000)
            self.assertEqual(deletions,[])
            self.assertEqual(len(skipped),1)
        # Mutually grounded repairs contradict each other: drop both, keep filler.
        deletions,skipped=app.semantic_plan_valid(WORDS,
            proposal([{'word_ids':['b'],'type':'filler','reason':'r'},
                      {'word_ids':['c'],'type':'repeat','repair_word_ids':['d'],'reason':'r'},
                      {'word_ids':['d'],'type':'repeat','repair_word_ids':['c'],'reason':'r'}]),5000)
        self.assertEqual([d['word_ids'] for d in deletions],[['b']])
        self.assertEqual(len(skipped),2)
        # filler needs no repair
        deletions,_=app.semantic_plan_valid(WORDS,proposal([{'word_ids':['b'],'type':'filler','reason':'r'}]),5000)
        self.assertEqual(deletions[0]['repair_word_ids'],[])

    def test_total_removal_ratio_capped(self):
        answer=proposal([{'word_ids':['c'],'type':'repeat','repair_word_ids':['a'],'reason':'r','kept_text':'','needs_review':False}])
        with self.assertRaises(ValueError):app.semantic_plan_valid(WORDS,answer,1500)
        self.assertEqual(app.semantic_plan_valid(WORDS,answer,5000)[0][0]['word_ids'],['c'])

    def test_empty_deletions_is_honest_result(self):
        self.assertEqual(app.semantic_plan_valid(WORDS,proposal([]),5000),([],[]))


class CombinePlanTests(unittest.TestCase):
    def test_semantic_and_silence_removals_merge_without_overlap(self):
        removed=[{'start_ms':600,'end_ms':900,'reason':'semantic:del_1'},
                 {'start_ms':1360,'end_ms':3040,'reason':'silence'},
                 {'start_ms':850,'end_ms':1400,'reason':'semantic:del_2'}]
        keep,merged=media.combine_removed(5000,removed)
        self.assertEqual(keep,[{'start_ms':0,'end_ms':600},{'start_ms':3040,'end_ms':5000}])
        self.assertEqual(len(merged),1)
        self.assertIn('semantic:del_1',merged[0]['reason'])

    def test_adjacent_pause_boundary_still_valid(self):
        # Semantic word ending exactly where a silence cut begins: no zero-length keep fragment.
        keep,merged=media.combine_removed(5000,[{'start_ms':600,'end_ms':1200,'reason':'semantic:del_1'},
                                                {'start_ms':1200,'end_ms':3000,'reason':'silence'}])
        self.assertEqual(keep,[{'start_ms':0,'end_ms':600},{'start_ms':3000,'end_ms':5000}])

    def test_rejects_out_of_range_and_excessive_removal(self):
        with self.assertRaises(ValueError):media.combine_removed(5000,[{'start_ms':-1,'end_ms':100,'reason':'x'}])
        with self.assertRaises(ValueError):media.combine_removed(5000,[{'start_ms':6000,'end_ms':7000,'reason':'x'}])
        with self.assertRaises(ValueError):media.combine_removed(5000,[{'start_ms':0,'end_ms':4990,'reason':'x'}])
        self.assertEqual(media.combine_removed(5000,[])[0],[{'start_ms':0,'end_ms':5000}])


class EdgeSnapTests(unittest.TestCase):
    """auto-editor-style margin: cut edges extend into adjacent pauses, never into speech."""
    def test_edges_extend_into_adjacent_silence(self):
        silences=[(1000,1250),(1550,1900)]
        out=media.snap_edges_to_silence(silences,[{'start_ms':1260,'end_ms':1540,'reason':'semantic:del_1'}])
        self.assertEqual(out,[{'start_ms':1060,'end_ms':1740,'reason':'semantic:del_1'}])

    def test_extension_capped_and_never_reaches_distant_silence(self):
        out=media.snap_edges_to_silence([(0,400),(2600,3000)],[{'start_ms':1260,'end_ms':1540,'reason':'x'}])
        self.assertEqual(out,[{'start_ms':1260,'end_ms':1540,'reason':'x'}])
        out=media.snap_edges_to_silence([(1000,1255)],[{'start_ms':1260,'end_ms':1540,'reason':'x'}])
        self.assertEqual(out[0]['start_ms'],1060)  # capped at extend_ms, not silence start

    def test_edges_inside_silence_unchanged(self):
        out=media.snap_edges_to_silence([(1000,2000)],[{'start_ms':1200,'end_ms':1800,'reason':'x'}])
        self.assertEqual(out,[{'start_ms':1200,'end_ms':1800,'reason':'x'}])

    def test_snap_keeps_interval_valid_when_silence_overlaps(self):
        out=media.snap_edges_to_silence([(1200,1400),(1450,1900)],[{'start_ms':1300,'end_ms':1500,'reason':'x'}])
        self.assertTrue(all(seg['end_ms']>seg['start_ms'] for seg in out))


class SemanticCutAPITests(unittest.TestCase):
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

    def request(self,path,method='GET',body=None,expected=200):
        headers={}
        if body is not None and not isinstance(body,bytes):body=json.dumps(body,ensure_ascii=False).encode();headers['Content-Type']='application/json'
        req=urllib.request.Request(self.base+path,body,headers,method=method)
        try:res=urllib.request.urlopen(req,timeout=20)
        except urllib.error.HTTPError as exc:res=exc
        with res:
            data=res.read();self.assertEqual(res.status,expected,data[:700])
            return json.loads(data) if 'application/json' in res.headers.get('Content-Type','') else data

    def wait(self,job_id,limit=40):
        deadline=time.time()+limit
        while time.time()<deadline:
            job=self.request('/api/jobs/'+job_id)
            if job['status'] not in ('queued','running'):return job
            time.sleep(.1)
        self.fail('job '+job_id+' timed out')

    def confirmed_project_with_asset(self):
        p=self.request('/api/projects','POST',{'title':'语义净剪联调','source_text':'来源原文'},expected=201)
        parts=[{'role':r,'text':t} for r,t in [('hook','钩子'),('content','内容'),('anchor','钉子'),('quote','金句'),('ending','结尾')]]
        p=self.request('/api/projects/'+p['id']+'/draft','PATCH',{'parts':parts,'draft_revision':p['draft_revision']})
        p=self.request('/api/projects/'+p['id']+'/confirm','POST',{'draft_revision':p['draft_revision']})
        raw=self.fixture.read_bytes()
        session=self.request('/api/uploads','POST',{'project_id':p['id'],'filename':'semantic.mp4','size':len(raw)},expected=201)
        url='/api/uploads/'+session['id'];self.request(url+'?offset=0','PUT',raw)
        asset=self.request(url+'/complete','POST',{})
        return p,asset

    def test_full_semantic_chain_cuts_real_media_and_syncs_captions(self):
        if not self.fixture.exists():self.skipTest('FFmpeg not installed')
        p,asset=self.confirmed_project_with_asset()
        transcript={'text':'先看 嗯 再做判断 条件','words':WORDS,'cues':[]}
        answer=proposal([
            {'word_ids':['b'],'type':'filler','repair_word_ids':[],'reason':'有声水词，无信息量','kept_text':'先看条件再做判断','needs_review':False},
            {'word_ids':['d'],'type':'repeat','repair_word_ids':['c'],'reason':'疑似与前句重复，请试听 4.6s 附近','kept_text':'再做判断','needs_review':True}])
        with patch('providers.available',return_value={'llm':True,'asr':True}),\
             patch('providers.asr',return_value=transcript),\
             patch('providers.llm_json',return_value=answer):
            job=self.request('/api/projects/'+p['id']+'/semantic-cut','POST',{'asset_id':asset['id']},expected=202)
            result=self.wait(job['id'])
        self.assertEqual(result['status'],'needs_action',result.get('error'))
        plan=result['result']['plan']
        self.assertEqual(len(plan['deletions']),2)
        self.assertEqual(plan['skipped'],[])
        self.assertTrue(plan['deletions'][1]['needs_review'])
        self.assertEqual(plan['deletions'][1]['repair_text'],'再做判断')
        self.assertEqual(plan['method'],'llm_word_id_grounded')
        # autocut-style preview: every transcript word, tagged with the deletion that would remove it.
        self.assertEqual([x for x in plan['preview'] if x[1]=='del_1'],[['嗯','del_1']])
        self.assertIn(['先看',None],plan['preview'])
        details=self.request('/api/projects/'+p['id'])
        self.assertEqual(len(details['semantic_plans']),1)
        # Apply with only the confident deletion; the review-flagged one must survive untouched.
        job=self.request('/api/projects/'+p['id']+'/apply-semantic-cut','POST',
            {'asset_id':asset['id'],'plan_id':plan['id'],'deletion_ids':['del_1']},expected=202)
        result=self.wait(job['id'])
        self.assertEqual(result['status'],'succeeded',result.get('error'))
        artifact=result['result']['artifact']
        self.assertEqual(artifact['kind'],'clean_video')
        # keep = [0-600] + [900-1360] + [3040-5000] ≈ 3.02s; word 'd' (4.6-4.9s) is inside kept tail.
        self.assertAlmostEqual(artifact['info']['duration_ms'],3020,delta=200)
        self.assertTrue(artifact['info']['audio'])
        self.assertEqual(result['result']['summary'][0]['text'],'嗯')
        cut_plan=result['result']['plan']
        self.assertEqual(cut_plan['method'],'semantic_plus_silence')
        self.assertEqual(cut_plan['edge_snap']['extend_ms'],200)
        self.assertFalse(any(s['start_ms']<=600 and s['end_ms']>=900 for s in cut_plan['segments']))
        self.assertTrue(any(s['start_ms']<=4600 and s['end_ms']>=4900 for s in cut_plan['segments']))
        # Packaging on this base must not restore removed audio and captions must follow the same mapping.
        with patch('providers.available',return_value={'llm':False,'asr':True}),patch('providers.asr',return_value=transcript):
            job=self.request('/api/projects/'+p['id']+'/prepare-video','POST',{'asset_id':asset['id'],'artifact_id':artifact['id']},expected=202)
            result=self.wait(job['id'],60)
        self.assertEqual(result['status'],'succeeded',result.get('error'))
        packaged=result['result']['artifact']
        text=''.join(c['text'] for c in packaged['caption_cues'])
        self.assertNotIn('嗯',text)
        self.assertIn('先看',text);self.assertIn('再做判断',text);self.assertIn('条件',text)
        self.assertAlmostEqual(packaged['info']['duration_ms'],artifact['info']['duration_ms'],delta=150)
        self.assertLess(packaged['caption_cues'][-1]['end_ms'],packaged['info']['duration_ms']+50)
        # Original asset, transcript and proposal remain on record.
        self.assertTrue(Path(asset['path']).is_file())
        self.assertIsNotNone(app.STORE.get('semantic_plan',plan['id']))

    def test_apply_rejects_foreign_or_fabricated_selections(self):
        if not self.fixture.exists():self.skipTest('FFmpeg not installed')
        p,asset=self.confirmed_project_with_asset()
        other,_=self.confirmed_project_with_asset()
        plan=app.STORE.put('semantic_plan',{'project_id':p['id'],'asset_id':asset['id'],'transcript_id':'t',
            'script_version_id':p['confirmed_version_id'],'deletions':[{'id':'del_1','word_ids':['b'],'type':'filler',
            'type_label':'有声水词','text':'嗯','intervals':[[600,900]],'start_ms':600,'end_ms':900,
            'kept_text':'','reason':'r','needs_review':False}],'duration_ms':5000,'method':'llm_word_id_grounded'})
        self.request('/api/projects/'+other['id']+'/apply-semantic-cut','POST',
            {'asset_id':asset['id'],'plan_id':plan['id'],'deletion_ids':['del_1']},expected=404)
        self.request('/api/projects/'+p['id']+'/apply-semantic-cut','POST',
            {'asset_id':asset['id'],'plan_id':plan['id'],'deletion_ids':['del_99']},expected=400)
        self.request('/api/projects/'+p['id']+'/apply-semantic-cut','POST',
            {'asset_id':asset['id'],'plan_id':plan['id'],'deletion_ids':[]},expected=400)
        # Client-supplied timestamps are ignored: only plan entries select server-derived intervals.
        job=self.request('/api/projects/'+p['id']+'/apply-semantic-cut','POST',
            {'asset_id':asset['id'],'plan_id':plan['id'],'deletion_ids':['del_1'],
             'segments':[{'start_ms':0,'end_ms':10}]},expected=202)
        result=self.wait(job['id'])
        self.assertEqual(result['status'],'failed')  # transcript 't' does not exist -> honest failure, no cut
        self.assertIsNone(self.request('/api/projects/'+p['id']).get('reviewed_artifact_id'))

    def test_propose_requires_providers_and_matching_version(self):
        p,asset=self.confirmed_project_with_asset()
        with patch('providers.available',return_value={'llm':False,'asr':True}):
            self.request('/api/projects/'+p['id']+'/semantic-cut','POST',{'asset_id':asset['id']},expected=409)
        stale=self.request('/api/projects/'+p['id']+'/draft','PATCH',
            {'parts':[{'role':r,'text':'改动后 '+r} for r,_ in app.ROLES],'draft_revision':p['draft_revision']})
        with patch('providers.available',return_value={'llm':True,'asr':True}):
            self.request('/api/projects/'+p['id']+'/semantic-cut','POST',{'asset_id':asset['id']},expected=409)

    def test_auto_apply_without_llm_uses_silence_and_fixed_filler(self):
        if not self.fixture.exists():self.skipTest('FFmpeg not installed')
        p,asset=self.confirmed_project_with_asset()
        words=[{'id':'a','text':'先说','start_ms':0,'end_ms':500},
               {'id':'b','text':'嗯','start_ms':550,'end_ms':800},
               {'id':'c','text':'呃','start_ms':820,'end_ms':1050},
               {'id':'d','text':'结论','start_ms':1100,'end_ms':1600},
               {'id':'e','text':'谢谢','start_ms':4000,'end_ms':4500}]
        app.STORE.put('transcript',{'text':'先说 嗯 呃 结论 谢谢','words':words,'cues':[],
            'asset_id':asset['id'],'project_id':p['id']})
        with patch('providers.available',return_value={'llm':False,'asr':False}):
            job=self.request('/api/projects/'+p['id']+'/semantic-auto-apply','POST',{'asset_id':asset['id']},expected=202)
            result=self.wait(job['id'])
        self.assertEqual(result['status'],'succeeded',result.get('error'))
        self.assertFalse(result['result']['requires_action'])
        self.assertEqual(result['result']['auto_selected'],['del_1'])
        self.assertEqual(result['result']['auto_original_seconds'],5.0)
        self.assertGreater(result['result']['auto_reduction_pct'],8)
        self.assertLess(result['result']['auto_reduction_pct'],50)
        plan=self.request('/api/projects/'+p['id'])['semantic_plans'][0]
        self.assertEqual(plan['method'],'deterministic_filler')
        self.assertTrue(plan['auto_applied'])
        self.assertIn('del_1',plan['auto_eligible_ids'])
        artifact=result['result']['artifact']
        self.assertEqual(artifact['kind'],'clean_video')
        self.assertTrue(artifact['auto_applied'])
        self.assertEqual(artifact['filename'].startswith('一键净剪-'),True)
        # semantic_auto_job 最后会把 result['plan'] 换成更新后的 semantic_plan；
        # 真正含 removed 的清理计划要从项目详情的 clean_plans 里按 artifact.plan_id 取。
        details=self.request('/api/projects/'+p['id'])
        clean=next(c for c in details['clean_plans'] if c['id']==artifact['plan_id'])
        kept=[w for w in words if w['id'] not in {'b','c'}]
        for seg in clean['removed']:
            if seg.get('reason')!='silence':continue
            for w in kept:
                self.assertFalse(seg['start_ms']<w['end_ms'] and seg['end_ms']>w['start_ms'],
                    f'静音段切进保留词 {w["text"]}')
        # Undo this cut: no prior clean version -> back to the original asset.
        undo=self.request('/api/projects/'+p['id']+'/apply-semantic-cut','POST',
            {'artifact_id':artifact['id'],'undo':True})
        self.assertEqual(undo['restored_artifact_id'],'')
        details=self.request('/api/projects/'+p['id'])
        self.assertEqual(details['latest_artifact_id'],'')
        self.assertEqual(details['stage'],'edit')
        self.assertTrue(Path(asset['path']).is_file())

    def test_undo_returns_previous_version_and_only_current(self):
        if not self.fixture.exists():self.skipTest('FFmpeg not installed')
        p,asset=self.confirmed_project_with_asset()
        transcript={'text':'先看 嗯 再做判断 条件','words':WORDS,'cues':[]}
        app.STORE.put('transcript',{**transcript,'asset_id':asset['id'],'project_id':p['id']})
        plan=app.STORE.put('semantic_plan',{'project_id':p['id'],'asset_id':asset['id'],
            'transcript_id':next(x['id'] for x in app.STORE.list('transcript',200) if x.get('asset_id')==asset['id']),
            'script_version_id':p['confirmed_version_id'],'deletions':[{'id':'del_1','word_ids':['b'],'type':'filler',
            'type_label':'有声口误','text':'嗯','intervals':[[600,900]],'start_ms':600,'end_ms':900,
            'kept_text':'','reason':'固定词表','needs_review':False}],'duration_ms':5000,
            'method':'llm_word_id_grounded','auto_eligible_ids':['del_1']})
        first=self.request('/api/projects/'+p['id']+'/apply-semantic-cut','POST',
            {'asset_id':asset['id'],'plan_id':plan['id'],'deletion_ids':['del_1']},expected=202)
        first=self.wait(first['id'])
        self.assertEqual(first['status'],'succeeded',first.get('error'))
        first_artifact=first['result']['artifact']
        second=self.request('/api/projects/'+p['id']+'/apply-semantic-cut','POST',
            {'asset_id':asset['id'],'plan_id':plan['id'],'deletion_ids':['del_1']},expected=202)
        second=self.wait(second['id'])
        self.assertEqual(second['status'],'succeeded',second.get('error'))
        second_artifact=second['result']['artifact']
        self.assertEqual(second_artifact['previous_artifact_id'],first_artifact['id'])
        self.request('/api/projects/'+p['id']+'/apply-semantic-cut','POST',
            {'artifact_id':first_artifact['id'],'undo':True},expected=409)
        undo=self.request('/api/projects/'+p['id']+'/apply-semantic-cut','POST',
            {'artifact_id':second_artifact['id'],'undo':True})
        self.assertEqual(undo['restored_artifact_id'],first_artifact['id'])
        self.assertEqual(undo['original_seconds'],round(first_artifact['info']['duration_ms']/1000,1))
        details=self.request('/api/projects/'+p['id'])
        self.assertEqual(details['latest_artifact_id'],first_artifact['id'])
        self.assertEqual(details['stage'],'review')


class AutoSelectionUnitTests(unittest.TestCase):
    def test_only_fixed_lexicon_fillers_are_auto_selected(self):
        lookup={'x':{'id':'x','text':'嗯'},'y':{'id':'y','text':'啊'},
                'z':{'id':'z','text':'呃，'},'j':{'id':'j','text':'额'},'r':{'id':'r','text':'再做判断'}}
        filler=lambda ids,review=False:{'word_ids':ids,'type':'filler','repair_word_ids':[],'needs_review':review}
        self.assertTrue(app.semantic_auto_eligible(filler(['x']),lookup))
        self.assertTrue(app.semantic_auto_eligible(filler(['z']),lookup))
        self.assertFalse(app.semantic_auto_eligible(filler(['y']),lookup))
        # ASR splits 巨额 into 巨/额/的; deleting 额 produces 巨的亏损.
        self.assertFalse(app.semantic_auto_eligible(filler(['j']),lookup))
        self.assertFalse(app.semantic_auto_eligible(filler(['x'],True),lookup))
        # 改口：删除侧与保留侧必须相邻、同属一次重说，且 kept_text 能对上保留词。
        retake={'p':{'id':'p','text':'压'},'q':{'id':'q','text':'力呀'},
                'r':{'id':'r','text':'压'},'s':{'id':'s','text':'力呀'}}
        self.assertTrue(app.semantic_auto_eligible({'word_ids':['p','q'],'type':'repeat','repair_word_ids':['r','s'],'kept_text':'压力呀','needs_review':False},retake))
        # 实测事故：删掉“压”却仍声称保留“压力呀”，保留侧只有“力呀”。
        self.assertFalse(app.semantic_auto_eligible({'word_ids':['p'],'type':'repeat','repair_word_ids':['q'],'kept_text':'压力呀','needs_review':False},retake))
        # 两次不完整尝试拼成一句：删除侧与保留侧中间夹着实词。
        self.assertFalse(app.semantic_auto_eligible({'word_ids':['p'],'type':'restart','repair_word_ids':['s'],'kept_text':'力呀','needs_review':False},retake))
        self.assertFalse(app.semantic_auto_eligible({'word_ids':['p','q'],'type':'repeat','repair_word_ids':[],'needs_review':False},retake))
        self.assertFalse(app.semantic_auto_eligible({'word_ids':['p','q'],'type':'restart','repair_word_ids':['r','s'],'kept_text':'压力呀','needs_review':True},retake))

    def test_repair_mismatch_is_downgraded_instead_of_auto_applied(self):
        words=[{'id':'p','text':'压','start_ms':0,'end_ms':200},
               {'id':'q','text':'力呀','start_ms':210,'end_ms':500},
               {'id':'r','text':'压','start_ms':600,'end_ms':800},
               {'id':'s','text':'力呀','start_ms':810,'end_ms':1100}]
        lookup={w['id']:w for w in words}
        good,_=app.semantic_plan_valid(words,proposal([
            {'word_ids':['p','q'],'type':'repeat','repair_word_ids':['r','s'],'kept_text':'压力呀','needs_review':False}]),5000)
        self.assertFalse(good[0]['needs_review'])
        self.assertTrue(app.semantic_auto_eligible(good[0],lookup))
        bad,_=app.semantic_plan_valid(words,proposal([
            {'word_ids':['p'],'type':'repeat','repair_word_ids':['q'],'kept_text':'压力呀','needs_review':False}]),5000)
        self.assertEqual(len(bad),1)                     # 建议保留给用户看，不静默丢弃
        self.assertTrue(bad[0]['needs_review'])
        self.assertFalse(app.semantic_auto_eligible(bad[0],lookup))

    def test_filler_plan_groups_tight_runs_and_skips_isolated(self):
        words=[{'id':'a','text':'先说','start_ms':0,'end_ms':500},
               {'id':'b','text':'嗯','start_ms':550,'end_ms':800},
               {'id':'c','text':'呃','start_ms':820,'end_ms':1050},
               {'id':'d','text':'结论','start_ms':1100,'end_ms':1600},
               {'id':'e','text':'谢谢','start_ms':4000,'end_ms':4500}]
        deletions,skipped=app.semantic_filler_plan(words,5000)
        self.assertEqual([d['word_ids'] for d in deletions],[['b','c']])
        self.assertEqual(deletions[0]['start_ms'],550)
        self.assertEqual(deletions[0]['end_ms'],1050)
        self.assertEqual(skipped,[])
        self.assertTrue(all(d['type']=='filler' for d in deletions))

    def test_silence_removal_never_clips_kept_word_tail(self):
        # 真实样本：静音段 107.44s 起，ASR 词“么”到 107.495s；必须先让开词尾再删。
        words=[{'id':'a','text':'怎','start_ms':106910,'end_ms':106990},
               {'id':'b','text':'么','start_ms':106990,'end_ms':107495},
               {'id':'c','text':'你','start_ms':109260,'end_ms':109420}]
        out=app.semantic_trim_silence([{'start_ms':107444,'end_ms':109102,'reason':'silence'}],words)
        self.assertEqual([(x['start_ms'],x['end_ms']) for x in out],[(107525,109102)])
        for seg in out:
            for w in words:
                self.assertFalse(seg['start_ms']<w['end_ms'] and seg['end_ms']>w['start_ms'])


if __name__=='__main__':
    try:unittest.main(verbosity=2)
    finally:app.POOL.shutdown(wait=True)
