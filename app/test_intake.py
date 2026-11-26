import json
import unittest
from urllib.parse import quote
from unittest.mock import patch
import video_intake as intake

LINK='https://www.douyin.com/jingxuan?modal_id=7693550505402840741'


class IntakeTests(unittest.TestCase):
    def test_share_url_preserves_large_id(self):
        info=intake.share_info('看看这个视频 ['+LINK+']('+LINK+')')
        self.assertEqual(info['video_id'],'7693550505402840741')
        self.assertEqual(info['canonical_url'],'https://www.douyin.com/video/7693550505402840741')
        self.assertIn('看看这个视频',info['share_text'])

    def test_only_supported_public_targets(self):
        for value in ('https://127.0.0.1/internal','https://douyin.com.attacker.invalid/video/1','https://user:secret@douyin.com/video/1','http://www.douyin.com/video/12345678'):
            with self.assertRaises(ValueError):intake.share_info(value)

    def test_metrics_missing_is_not_zero_or_rounded_guess(self):
        result=intake.normalize({'metadata':{'metrics':{'likes':0,'favorites':'1.2万','shares':None,'comments':False}}},intake.share_info(LINK),'test_fixture')
        self.assertEqual(result['metadata']['metrics']['likes'],0)
        self.assertEqual(result['metadata']['metrics']['favorites'],'1.2万')
        self.assertIsNone(result['metadata']['metrics']['shares'])
        self.assertIsNone(result['metadata']['metrics']['comments'])

    def test_opening_window_does_not_claim_exact_sentence_cut(self):
        cues=intake.cues_valid([{'text':'这句字幕会超过两秒。','start_ms':300,'end_ms':3600}])
        opening=intake.opening_window(cues,[])
        self.assertEqual(opening['precision'],'cue_overlap')
        self.assertTrue(opening['segments'][0]['crosses_end'])
        self.assertEqual(intake.opening_window([],[])['precision'],'unavailable')

    def test_word_window_uses_real_overlap(self):
        words=intake.cues_valid([{'text':'先','start_ms':0,'end_ms':300},{'text':'问','start_ms':1850,'end_ms':2100},{'text':'什么','start_ms':2200,'end_ms':2600}],'word')
        window=intake.opening_window([],words)
        self.assertEqual(window['text'],'先问')
        self.assertEqual(window['precision'],'word')
        self.assertTrue(window['segments'][-1]['crosses_end'])

    def test_invalid_timestamps_are_rejected(self):
        for start,end in ((-1,100),(0,float('nan')),(100,90)):
            with self.assertRaises(ValueError):intake.cues_valid([{'text':'词','start_ms':start,'end_ms':end}])

    def test_hydration_only_extracts_selected_video(self):
        info=intake.share_info(LINK)
        raw={'aweme_id':info['video_id'],'desc':'自拟测试标题','statistics':{'digg_count':0,'collect_count':23},'video':{'duration':6000}}
        page='<script id="RENDER_DATA" type="application/json">'+quote(json.dumps({'data':[raw]}))+'</script>'
        obj=intake.embedded_video(page,info['video_id'])
        result=intake.from_douyin(obj,info)
        self.assertEqual(result['metadata']['metrics']['likes'],0)
        self.assertEqual(result['metadata']['metrics']['favorites'],23)
        self.assertEqual(result['transcript'],'')
        self.assertIsNone(intake.embedded_video(page,'1111111111111111111'))

    def test_challenge_page_stops_without_solving(self):
        with patch.object(intake,'public_page',return_value=('<script>__ac_nonce="challenge"</script>',LINK)) as fetch:
            result=intake.public_resolve(LINK)
        self.assertEqual(result['status'],'blocked')
        self.assertEqual(result['transcript'],'')
        self.assertTrue(all(x is None for x in result['metadata']['metrics'].values()))
        self.assertEqual(fetch.call_count,1)

    def test_model_quotes_must_resolve_to_source(self):
        source=intake.normalize({'cues':[{'text':'先看条件。','start_ms':0,'end_ms':1500}]},intake.share_info(LINK),'test_fixture')
        result=intake.validate_analysis({'summary':'需要核对的解读','hook':{'quote':'这是不存在的钩子'},'structure':[{'label':'内容','quote':'伪造的原话'}]},source)
        self.assertIsNone(result['hook'])
        self.assertEqual(result['structure'],[])
        self.assertTrue(result['warnings'])

    def test_download_media_requires_https_platform_url(self):
        for url in ('http://www.douyin.com/video.mp4','file://etc/passwd','ftp://cdn.douyin.com/a.mp4',''):
            with self.assertRaises(ValueError):intake.download_media(url,'unused-target.mp4')

    def test_download_media_enforces_size_caps(self):
        class FakeHeaders(dict):
            def get(self,k,default=None):return super().get(k,default)
        class FakeStream:
            def __init__(self,length,size):self.length=length;self.size=size;self.sent=0;self.headers=FakeHeaders({'Content-Length':str(length)})
            def read(self,n):
                if self.sent>=self.size:return b''
                chunk=b'x'*min(n,self.size-self.sent);self.sent+=len(chunk);return chunk
            def __enter__(self):return self
            def __exit__(self,*a):pass
        with patch('urllib.request.build_opener') as opener:
            opener.return_value.open.return_value=FakeStream(50,50)
            with self.assertRaises(ValueError):intake.download_media('https://www.douyin.com/a.mp4','t.mp4',max_bytes=10)
            opener.return_value.open.return_value=FakeStream(100000,100)
            with self.assertRaises(ValueError):intake.download_media('https://www.douyin.com/a.mp4','t.mp4',max_bytes=50)
            self.assertEqual(opener.call_count,2)


if __name__=='__main__':unittest.main(verbosity=2)
