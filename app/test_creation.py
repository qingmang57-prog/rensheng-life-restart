"""融合链路回归：隔离数据库 + 模型契约替身，不访问外部模型。"""
import json
import os
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

TEST_ROOT=Path(__file__).resolve().parent/'.test-runtime'
TEST_ROOT.mkdir(exist_ok=True)
os.environ['IP_DATA_DIR']=str(TEST_ROOT/'creation-api')
os.environ['IP_SKIP_LOCAL_CONFIG']='1'
os.environ['IP_DISABLE_LOCAL_ASR']='1'
for name in ('IP_LLM_URL','IP_LLM_MODEL','IP_ASR_URL','IP_RESOLVER_URL'):
    os.environ.pop(name,None)
import app
import creation_service as cs
import bench_service
from db import Store


def response(count=3, **extra):
    return {'topics':[{'title':'待核对选题'+str(i),'angle':'从一个真实场景切入','thesis':'论点待验证','basis':cs.BASIS_LIST[i%len(cs.BASIS_LIST)],**extra} for i in range(count)]}


class CreationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server=app.ThreadingHTTPServer(('127.0.0.1',0),app.Handler)
        cls.thread=threading.Thread(target=cls.server.serve_forever,daemon=True)
        cls.thread.start()
        cls.base='http://127.0.0.1:'+str(cls.server.server_port)

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown();cls.server.server_close()

    def setUp(self):
        self.test_path=Path(tempfile.mkdtemp(prefix='creation-',dir=TEST_ROOT))
        self.store=Store(self.test_path)
        for module in (app,cs,bench_service):
            p=patch.object(module,'STORE',self.store);p.start();self.addCleanup(p.stop)

    def request(self,path,method='GET',body=None,expected=200):
        raw=json.dumps(body,ensure_ascii=False).encode() if body is not None else None
        req=urllib.request.Request(self.base+'/api'+path,raw,{'Content-Type':'application/json'},method=method)
        try:res=urllib.request.urlopen(req,timeout=15)
        except urllib.error.HTTPError as exc:res=exc
        with res:
            data=json.loads(res.read());self.assertEqual(res.status,expected,data)
            return data

    def wait(self,job):
        for _ in range(200):
            job=self.request('/jobs/'+job['id'])
            if job['status'] not in ('queued','running'):return job
            time.sleep(.015)
        self.fail('job did not finish')

    def generate(self,body=None,result=None):
        with patch.object(app.providers,'available',return_value={'llm':True}),patch.object(app.providers,'llm_json',return_value=result or response()):
            j=self.request('/creation/topics','POST',body or {'context':'真实场景','goal':'attention'},202)
            return self.wait(j)

    def project(self):
        topic=self.generate()['result']['items'][0]
        r=self.request('/bench/topics/'+topic['id']+'/start-project','POST',{})
        return self.request('/projects/'+r['project_id'])

    def test_catalog_and_profile_partial_update_and_conflict(self):
        cat=self.request('/creation/catalog')
        self.assertEqual(sum(len(x['subtypes']) for x in cat['structures']),15)
        self.assertEqual((len(cat['elements']),len(cat['reasons'])),(8,15))
        old=self.request('/profile','PATCH',{'industry':'宠物医疗','experience':'7年','product':'门诊','audience':'养宠家庭','revision':0})
        new=self.request('/profile','PATCH',{'style':'自然口语','revision':old['revision']})
        self.assertEqual(new['industry'],'宠物医疗');self.assertEqual(new['product'],'门诊')
        self.request('/profile','PATCH',{'industry':'过期修改','revision':old['revision']},409)
        self.assertEqual(self.request('/bootstrap')['profile']['style'],'自然口语')

    def test_wrong_subtype_and_no_provider_do_not_create_records(self):
        self.request('/creation/topics','POST',{'context':'想法','structure':'story','subtype':'solve'},400)
        with patch.object(app.providers,'available',return_value={'llm':False}):
            self.request('/creation/topics','POST',{'context':'想法'},409)
        self.assertEqual(self.store.list('job'),[])

    def test_topics_preserve_brief_and_goal_ignores_other_goal_options(self):
        j=self.generate({'context':'产品选择','goal':'conversion','structure':'knowledge','subtype':'solve','reasons':['service'],'elements':['made-up']})
        self.assertEqual(j['status'],'succeeded',j.get('error'))
        topic=j['result']['items'][0]
        self.assertEqual(topic['status'],'candidate')
        self.assertEqual(topic['validation'],'unverified')
        self.assertEqual(topic['brief_snapshot']['reasons'],['service'])
        self.assertEqual(topic['brief_snapshot']['elements'],[])
        self.assertEqual(len(self.request('/bench/topics')['items']),3)

    def test_invalid_last_topic_does_not_partially_save_batch(self):
        invalid=response();invalid['topics'][-1]['evidence_work_ids']=['invented']
        j=self.generate(result=invalid)
        self.assertEqual(j['status'],'failed');self.assertEqual(self.store.list('topic'),[])

    def test_pain_remains_hypothesis_and_adoption_is_idempotent(self):
        self.request('/creation/topics','POST',{'goal':'pain'},400)
        self.request('/profile','PATCH',{'industry':'宠物医疗','audience':'养宠家庭'})
        j=self.generate({'goal':'pain'})
        self.assertEqual(j['status'],'succeeded');self.assertEqual(self.store.list('topic'),[])
        item=self.request('/creation/pain-points')['items'][0]
        first=self.request('/creation/pain-points/'+item['id']+'/adopt','POST',{})
        again=self.request('/creation/pain-points/'+item['id']+'/adopt','POST',{})
        self.assertEqual(first['topic_id'],again['topic_id'])
        topic=self.store.get('topic',first['topic_id'])
        self.assertEqual(topic['validation'],'unverified');self.assertEqual(topic['pain_point_id'],item['id'])

    def test_start_project_is_atomic_and_passes_brief_to_script(self):
        topic=self.generate()['result']['items'][0]
        with ThreadPoolExecutor(max_workers=5) as pool:
            rs=list(pool.map(lambda _:self.request('/bench/topics/'+topic['id']+'/start-project','POST',{}),range(5)))
        self.assertEqual(len({r['project_id'] for r in rs}),1)
        self.assertEqual(len(self.store.list('project')),1);self.assertEqual(len(self.store.list('source')),1)
        p=self.request('/projects/'+rs[0]['project_id'])
        self.assertEqual(p['creation_brief']['context'],'真实场景')
        self.assertEqual(p['next_action']['step'],'script')
        self.assertFalse(any(x['text'] for x in p['draft']))
        self.assertEqual(p['source']['text'],'')

    def test_generation_never_overwrites_and_stale_suggestion_is_rejected(self):
        p=self.project();before=p['draft']
        parts=[{'role':r,'text':'建议文字'+l} for r,l in app.ROLES]
        with patch.object(app.providers,'available',return_value={'llm':True}),patch.object(app.providers,'llm_json',return_value={'parts':parts}):
            job=self.wait(self.request('/projects/'+p['id']+'/writing-suggestions','POST',{'mode':'draft','draft_revision':1},202))
        self.assertEqual(job['status'],'succeeded');s=job['result']['suggestion']
        self.assertTrue(job['result']['draft_filled'])
        filled=self.request('/projects/'+p['id'])
        self.assertEqual(filled['draft_revision'],2);self.assertEqual(filled['stage'],'draft')
        self.assertEqual([{'role':x['role'],'text':x['text']} for x in filled['draft']],[{'role':x['role'],'text':x['text']} for x in parts])
        self.assertEqual(s['input_snapshot']['topic']['title'],p['title'])
        altered=[{'role':r,'text':'本人改写'+l} for r,l in app.ROLES]
        self.request('/projects/'+p['id']+'/draft','PATCH',{'parts':altered,'draft_revision':2})
        self.request('/projects/'+p['id']+'/accept-suggestion','POST',{'suggestion_id':s['id']},409)

    def test_accept_preserves_versions_and_reference_snapshot(self):
        p=self.project();parts=[{'role':r,'text':'本人原稿'+l} for r,l in app.ROLES]
        p=self.request('/projects/'+p['id']+'/draft','PATCH',{'parts':parts,'draft_revision':1})
        p=self.request('/projects/'+p['id']+'/confirm','POST',{'draft_revision':p['draft_revision']})
        version=self.store.get('script_version',p['confirmed_version_id'])
        altered=[{'role':r,'text':'自然口语'+l} for r,l in app.ROLES]
        with patch.object(app.providers,'available',return_value={'llm':True}),patch.object(app.providers,'llm_json',return_value={'parts':altered}):
            job=self.wait(self.request('/projects/'+p['id']+'/writing-suggestions','POST',{'mode':'recreate','reference':'仅参考的原文','draft_revision':p['draft_revision']},202))
        s=job['result']['suggestion'];self.assertEqual(s['input_snapshot']['reference'],'仅参考的原文')
        accepted=self.request('/projects/'+p['id']+'/accept-suggestion','POST',{'suggestion_id':s['id']})
        self.assertFalse(accepted['draft_changed']);self.assertEqual(accepted['stage'],'shoot')
        self.assertTrue(accepted['confirmed_version_id'])
        self.assertEqual([{'role':x['role'],'text':x['text']} for x in self.store.get('script_version',accepted['confirmed_version_id'])['parts']],
                         [{'role':x['role'],'text':x['text']} for x in altered])
        self.assertEqual(self.store.get('script_version',version['id'])['parts'],version['parts'])
        self.assertEqual(self.store.get('source',p['source_id'])['text'],'')
        history=self.request('/creation/history?kind=creation_recreate')
        self.assertEqual(len(history['items']),1)
        self.assertEqual(history['items'][0]['id'],job['id'])

    def test_accept_suggestion_writes_confirmed_version_and_advances(self):
        p=self.project();parts=[{'role':r,'text':'建议'+l} for r,l in app.ROLES]
        with patch.object(app.providers,'available',return_value={'llm':True}),patch.object(app.providers,'llm_json',return_value={'parts':parts}):
            job=self.wait(self.request('/projects/'+p['id']+'/writing-suggestions','POST',{'mode':'draft','draft_revision':1},202))
        r=self.request('/projects/'+p['id']+'/accept-suggestion','POST',{'suggestion_id':job['result']['suggestion']['id']})
        self.assertEqual(r['stage'],'shoot');self.assertTrue(r['confirmed_version_id'])
        self.assertFalse(r['draft_changed']);self.assertEqual(r['next_action']['step'],'shoot')
        version=self.store.get('script_version',r['confirmed_version_id'])
        self.assertEqual([{'role':x['role'],'text':x['text']} for x in version['parts']],
                         [{'role':x['role'],'text':x['text']} for x in parts])
        self.assertEqual(version['project_id'],p['id'])

    def test_generate_prompt_carries_principles_block(self):
        p=self.project();seen={}
        self.store.put('knowledge',{'title':'三观-01 长期主义','category':'三观','claim':'慢一点反而更快','boundary':'不适用于抢时效的热点',
            'endorsement':'accepted','lifecycle':'active','content_use':'available','validation':'personal_case'})
        self.request('/projects/'+p['id']+'/source','PATCH',{'text':'本人原创资料：讲清楚一个具体做法。'})
        def fake(prompt,payload):
            seen['prompt']=prompt;seen['payload']=payload
            return {'parts':[{'role':r,'text':'建议'+l} for r,l in app.ROLES]}
        with patch.object(app.providers,'available',return_value={'llm':True}),patch.object(app.providers,'llm_json',side_effect=fake):
            job=self.wait(self.request('/projects/'+p['id']+'/generate','POST',{},202))
        self.assertEqual(job['status'],'succeeded')
        self.assertIn('principles_block',seen['payload'])
        self.assertIn('慢一点反而更快',seen['payload']['principles_block'])
        self.assertTrue(seen['payload']['personal_principles'])
        self.assertIn('三观',seen['prompt'])

    def test_generate_retries_once_when_model_omits_parts(self):
        p=self.project()
        self.request('/projects/'+p['id']+'/source','PATCH',{'text':'本人原创资料：讲清楚一个具体做法。'})
        parts=[{'role':r,'text':'建议'+l} for r,l in app.ROLES]
        calls=[]
        def fake(prompt,payload):
            calls.append(1)
            return {'draft':'少了parts'} if len(calls)==1 else {'parts':parts}
        with patch.object(app.providers,'available',return_value={'llm':True}),patch.object(app.providers,'llm_json',side_effect=fake):
            job=self.wait(self.request('/projects/'+p['id']+'/generate','POST',{},202))
        self.assertEqual(job['status'],'succeeded',job.get('error'))
        self.assertEqual(len(calls),2)
        self.assertEqual(len(job['result']['suggestion']['parts']),len(parts))

    def test_generate_second_failure_keeps_raw_snippet(self):
        p=self.project()
        self.request('/projects/'+p['id']+'/source','PATCH',{'text':'本人原创资料：讲清楚一个具体做法。'})
        bad={'draft':'模型这次只给了draft字段'}
        with patch.object(app.providers,'available',return_value={'llm':True}),patch.object(app.providers,'llm_json',return_value=bad):
            job=self.wait(self.request('/projects/'+p['id']+'/generate','POST',{},202))
        self.assertEqual(job['status'],'failed')
        self.assertIn('模型这次只给了draft字段',job.get('error',''))

    def test_source_change_rejects_old_suggestion_and_polish_needs_draft(self):
        p=self.project()
        self.request('/projects/'+p['id']+'/writing-suggestions','POST',{'mode':'polish','draft_revision':1},400)
        with patch.object(app.providers,'available',return_value={'llm':True}),patch.object(app.providers,'llm_json',return_value={'parts':[{'role':r,'text':'建议'+l} for r,l in app.ROLES]}):
            j=self.wait(self.request('/projects/'+p['id']+'/writing-suggestions','POST',{'mode':'draft','draft_revision':1},202))
        self.request('/projects/'+p['id']+'/source','PATCH',{'text':'补充真实资料','revision':p['source']['revision']})
        self.request('/projects/'+p['id']+'/accept-suggestion','POST',{'suggestion_id':j['result']['suggestion']['id']},409)

    def test_restricted_knowledge_is_not_used_as_writing_material(self):
        k=self.store.put('knowledge',{'title':'资料','claim':'待核对','content_use':'review','lifecycle':'active','endorsement':'legacy'})
        self.request('/creation/topics','POST',{'context':'想法','knowledge_ids':[k['id']]},400)
        self.store.patch('knowledge',k['id'],{'content_use':'available'})
        j=self.generate({'context':'想法','knowledge_ids':[k['id']]},response(knowledge_ids=[k['id']]))
        p=bench_service.start_project(j['result']['items'][0]['id'])
        self.store.patch('knowledge',k['id'],{'endorsement':'rejected'})
        with patch.object(app.providers,'available',return_value={'llm':True}):
            self.request('/projects/'+p['project_id']+'/writing-suggestions','POST',{'mode':'draft','draft_revision':1},400)


if __name__=='__main__':unittest.main()
