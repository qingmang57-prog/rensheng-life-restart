import tempfile,unittest
from db import Store
import release_service as r
class ReleaseTests(unittest.TestCase):
 def setUp(self):
  self.temp=tempfile.TemporaryDirectory();self.s=Store(self.temp.name)
  self.s.put('script_version',{'project_id':'p','parts':[{'text':'真实定稿'}]},'v')
  self.s.put('project',{'title':'标题','confirmed_version_id':'v','draft_changed':False,'publication_pack':{'script_version_id':'v','selected_title':'发布标题','caption':'简介','tags':['创业']}},'p')
  self.data={'project_id':'p','script_version_id':'v','platform':'douyin','url':'https://www.douyin.com/video/7500000000000000001','confirmed':True}
 def tearDown(self):self.temp.cleanup()
 def test_ready_and_persist(self):
  self.assertTrue(r.prepare(self.s,'p')['ready']);item=r.register(self.s,self.data)
  self.assertEqual(item['verification'],'self_reported');self.assertIsNone(item['published_at'])
  self.assertEqual(r.prepare(Store(self.temp.name),'p')['receipts'][0]['id'],item['id'])
  self.assertEqual(self.s.list('matrix_publish_task'),[])
 def test_duplicate_and_cross_project(self):
  first=r.register(self.s,self.data);again=r.register(self.s,self.data);self.assertEqual(first['id'],again['id']);self.assertTrue(again['duplicate'])
  self.s.put('script_version',{'project_id':'q','parts':[{'text':'第二稿'}]},'w');self.s.put('project',{'title':'q','confirmed_version_id':'w','draft_changed':False},'q')
  with self.assertRaises(ValueError):r.register(self.s,{**self.data,'project_id':'q','script_version_id':'w'})
 def test_stale_draft(self):
  self.s.patch('project','p',{'draft_changed':True});self.assertFalse(r.prepare(self.s,'p')['ready'])
  with self.assertRaises(ValueError):r.register(self.s,self.data)
 def test_version_and_confirmation(self):
  for extra in ({'confirmed':False},{'script_version_id':'wrong'}):
   with self.assertRaises(ValueError):r.register(self.s,{**self.data,**extra})
 def test_urls(self):
  for url in ('http://www.douyin.com/video/123','https://www.douyin.com.evil.test/video/123','https://creator.douyin.com/creator-micro/content/upload','https://www.douyin.com/user/123','https://u:p@www.douyin.com/video/123','https://127.0.0.1/a'):
   with self.subTest(url=url),self.assertRaises(ValueError):r.register(self.s,{**self.data,'url':url})
 def test_wrong_platform(self):
  with self.assertRaises(ValueError):r.register(self.s,{**self.data,'platform':'bilibili'})
if __name__=='__main__':unittest.main()
