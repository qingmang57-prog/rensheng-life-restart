"""火山 SeedASR 直连适配器回归锁（2026-11-02）。

复用切片工坊同一套凭据；协议要点照抄工坊 volc_asr_service.py。
全程 mock 网络，不产生真实调用、不花积分、不打印凭据。
"""
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import asr_volc
import providers

SAMPLE = {
    'result': {'utterances': [
        {'text': '今天讲一个真相。', 'start_time': 0, 'end_time': 1500,
         'words': [{'text': '今天', 'start_time': 0, 'end_time': 400},
                   {'text': '讲一个', 'start_time': 400, 'end_time': 1000},
                   {'text': '真相', 'start_time': 1000, 'end_time': 1500}]},
    ]}
}

# 工坊版 _parse_words 只取时间对；模拟火山 word 不带 text 的情形
SAMPLE_NO_WORD_TEXT = {
    'result': {'utterances': [
        {'text': '今天讲一个真相。', 'start_time': 0, 'end_time': 1500,
         'words': [{'start_time': 0, 'end_time': 400},
                   {'start_time': 400, 'end_time': 1500}]},
    ]}
}


def fake_post(payload_response, submit_status='20000000'):
    def _fake(url, headers, payload):
        if url.endswith('/submit'):
            return {'X-Api-Status-Code': submit_status}, {}
        return {'X-Api-Status-Code': '20000000'}, payload_response
    return _fake


class VolcAsrTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.audio = Path(self.temp.name) / 'a.mp3'
        self.audio.write_bytes(b'ID3-fake-audio')
        self.env = mock.patch.dict(os.environ, {
            'IP_ASR_MODE': 'volc', 'IP_VOLC_ASR_KEY': 'test-key-123',
            'IP_VOLC_APP_ID': '', 'IP_VOLC_RESOURCE_ID': '', 'IP_ASR_URL': '',
            'IP_DISABLE_LOCAL_ASR': '1'})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.temp.cleanup()

    def test_configured_requires_mode_and_key(self):
        self.assertTrue(asr_volc.configured())
        with mock.patch.dict(os.environ, {'IP_ASR_MODE': 'external'}):
            self.assertFalse(asr_volc.configured())
        with mock.patch.dict(os.environ, {'IP_VOLC_ASR_KEY': ''}):
            self.assertFalse(asr_volc.configured())

    def test_headers_single_key_mode(self):
        h = asr_volc._headers('req-1')
        self.assertEqual(h['X-Api-Key'], 'test-key-123')
        self.assertEqual(h['X-Api-Resource-Id'], 'volc.seedasr.auc')
        self.assertNotIn('X-Api-Access-Key', h)
        self.assertNotIn('X-Api-App-Key', h)

    def test_headers_legacy_double_mode(self):
        with mock.patch.dict(os.environ, {'IP_VOLC_APP_ID': 'app-1'}):
            h = asr_volc._headers('req-2')
        self.assertEqual(h['X-Api-App-Key'], 'app-1')
        self.assertEqual(h['X-Api-Access-Key'], 'test-key-123')
        self.assertNotIn('X-Api-Key', h)

    def test_transcribe_with_word_text(self):
        with mock.patch.object(asr_volc, '_post', side_effect=fake_post(SAMPLE)), \
             mock.patch.object(asr_volc.time, 'sleep'):
            r = asr_volc.transcribe(self.audio)
        self.assertEqual(r['method'], 'volc_seedasr')
        self.assertEqual(r['precision'], 'word')
        self.assertEqual(r['text'], '今天讲一个真相。')
        self.assertEqual([w['text'] for w in r['words']], ['今天', '讲一个', '真相'])
        self.assertEqual(r['words'][0]['start_ms'], 0)
        self.assertEqual(r['cues'][0]['end_ms'], 1500)

    def test_no_word_text_falls_back_to_utterance(self):
        with mock.patch.object(asr_volc, '_post', side_effect=fake_post(SAMPLE_NO_WORD_TEXT)), \
             mock.patch.object(asr_volc.time, 'sleep'):
            r = asr_volc.transcribe(self.audio)
        self.assertEqual(r['precision'], 'utterance')
        self.assertEqual(len(r['words']), 1)
        self.assertEqual(r['words'][0]['text'], '今天讲一个真相。')
        self.assertEqual(r['words'][0]['end_ms'], 1500)

    def test_wrapped_result_shape(self):
        wrapped = {'data': {'result': SAMPLE['result']}}
        with mock.patch.object(asr_volc, '_post', side_effect=fake_post(wrapped)), \
             mock.patch.object(asr_volc.time, 'sleep'):
            r = asr_volc.transcribe(self.audio)
        self.assertEqual(r['text'], '今天讲一个真相。')

    def test_empty_audio_file(self):
        empty = Path(self.temp.name) / 'empty.mp3'
        empty.write_bytes(b'')
        with self.assertRaises(asr_volc.VolcEmptyAudio):
            asr_volc.transcribe(empty)

    def test_8mb_limit(self):
        big = Path(self.temp.name) / 'big.mp3'
        big.write_bytes(b'x' * (8 * 1024 * 1024 + 1))
        with self.assertRaises(asr_volc.VolcAsrError) as ctx:
            asr_volc.transcribe(big)
        self.assertIn('8MB', str(ctx.exception))

    def test_no_speech(self):
        def _fake(url, headers, payload):
            if url.endswith('/submit'):
                return {'X-Api-Status-Code': '20000000'}, {}
            return {'X-Api-Status-Code': '20000003'}, {}
        with mock.patch.object(asr_volc, '_post', side_effect=_fake), \
             mock.patch.object(asr_volc.time, 'sleep'):
            with self.assertRaises(asr_volc.VolcEmptyAudio):
                asr_volc.transcribe(self.audio)

    def test_submit_rejected(self):
        with mock.patch.object(asr_volc, '_post', side_effect=fake_post(SAMPLE, '40000001')):
            with self.assertRaises(asr_volc.VolcAsrError):
                asr_volc.transcribe(self.audio)

    def test_poll_timeout(self):
        def _fake(url, headers, payload):
            if url.endswith('/submit'):
                return {'X-Api-Status-Code': '20000000'}, {}
            return {'X-Api-Status-Code': '20000001'}, {}
        with mock.patch.object(asr_volc, '_post', side_effect=_fake), \
             mock.patch.object(asr_volc.time, 'sleep'):
            with self.assertRaises(asr_volc.VolcAsrError) as ctx:
                asr_volc.transcribe(self.audio)
        self.assertIn('轮询超时', str(ctx.exception))

    def test_missing_key_error_does_not_leak_secret(self):
        with mock.patch.dict(os.environ, {'IP_VOLC_ASR_KEY': ''}):
            with self.assertRaises(asr_volc.VolcAsrError) as ctx:
                asr_volc.transcribe(self.audio)
        self.assertNotIn('test-key-123', str(ctx.exception))

    def test_providers_mode_volc(self):
        self.assertTrue(providers.volc_asr())
        self.assertFalse(providers.external_asr())
        self.assertEqual(providers.available()['asr_mode'], 'volc')
        self.assertTrue(providers.available()['asr'])

    def test_providers_asr_routes_to_volc(self):
        with mock.patch.object(asr_volc, '_post', side_effect=fake_post(SAMPLE)), \
             mock.patch.object(asr_volc.time, 'sleep'):
            r = providers.asr(self.audio)
        self.assertEqual(r['method'], 'volc_seedasr')
        self.assertEqual(len(r['words']), 3)
        self.assertTrue(all('id' in w for w in r['words']))

    def test_external_mode_unchanged(self):
        with mock.patch.dict(os.environ, {
                'IP_ASR_MODE': 'external', 'IP_ASR_URL': 'http://127.0.0.1:9999/asr',
                'IP_VOLC_ASR_KEY': ''}):
            self.assertTrue(providers.external_asr())
            self.assertFalse(providers.volc_asr())
            self.assertEqual(providers.available()['asr_mode'], 'external')

    def test_no_plaintext_key_in_env_template(self):
        template = Path(__file__).resolve().parents[2] / '部署' / '云端全功能_20261101' / 'env.example'
        text = template.read_text(encoding='utf-8')
        self.assertIn('IP_VOLC_ASR_KEY=', text)
        for line in text.splitlines():
            if line.startswith('IP_VOLC_ASR_KEY='):
                self.assertEqual(line.strip(), 'IP_VOLC_ASR_KEY=')


if __name__ == '__main__':
    unittest.main()
