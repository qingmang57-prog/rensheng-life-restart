"""style_profile 单元测试：统计路径、样本门槛、LLM 引用校验与断网降级。

运行：在 app 目录执行  python -m unittest -v test_style_profile
测试数据是本地固定样本，只用于验证逻辑；真实数据验收另用数据库副本。
"""
import json
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

os.environ.setdefault('IP_SKIP_LOCAL_CONFIG', '1')
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import db
import style_profile


WORKS = [
    ('w1', '我告诉你一个秘密，普通人做自媒体，先坚持三十天再说。'),
    ('w2', '我告诉你一个秘密，做自媒体别急着买设备，手机就能拍。'),
    ('w3', '一个人怎么做自媒体？先想清楚你要对谁说话。'),
    ('w4', '一个人怎么做自媒体，先把口播练顺，再谈画面。'),
    ('w5', '其实就是很多人不是不会拍，就是总想一步到位，然后就是一直不敢开始。'),
    ('w6', '就是做自媒体最难的不是开始，是坚持更新，然后接受前十条没人看。'),
]


class StyleProfileTest(unittest.TestCase):
    def make_store(self, works=WORKS, account='acct_a'):
        root = tempfile.mkdtemp(prefix='style-profile-test-')
        self.addCleanup(shutil.rmtree, root, True)
        store = db.Store(root)
        # 测试夹具一次事务写入，避免每条作品一次 fsync 拖慢用例。
        ts = db.now()
        with store.connect() as con:
            for key, title in works:
                data = {
                    'bench_account_id': account,
                    'platform': 'douyin',
                    'platform_video_id': key,
                    'title': title,
                    'source_url': '',
                    'collected_at': '2026-11-01T00:00:00+00:00',
                    'published_at': '2026-10-01T00:00:00+00:00',
                }
                con.execute('INSERT OR REPLACE INTO records VALUES (?,?,?,?,?,?)',
                            ('bench_work', key, json.dumps(data, ensure_ascii=False), 1, ts, ts))
            account_data = {'display_name': '测试账号', 'platform': 'douyin'}
            con.execute('INSERT OR REPLACE INTO records VALUES (?,?,?,?,?,?)',
                        ('bench_account', account, json.dumps(account_data, ensure_ascii=False),
                         1, ts, ts))
        return store

    @staticmethod
    def text_by_id(works=WORKS):
        return {key: style_profile.clean_text(title) for key, title in works}

    def test_statistics_path_is_complete_and_quotes_are_real(self):
        store = self.make_store()
        result = style_profile.build_style_card(store, use_llm=False)
        self.assertTrue(result['ok'])
        card = result['style_card']
        self.assertEqual(card['schema'], 'style_card.v1')
        self.assertEqual(card['method'], 'statistics')
        self.assertEqual(card['source']['work_count'], 6)
        self.assertEqual(card['source']['text_work_count'], 6)
        self.assertGreater(card['sentence']['sentence_count'], 0)
        self.assertTrue(card['openers'])
        self.assertTrue(card['closers'])
        # 开头聚类：两句“我告诉你一个秘密”必须合成一组，并给出两个作品 ID。
        top = card['openers'][0]
        self.assertEqual(top['quote'], '我告诉你一个秘密')
        self.assertEqual(top['count'], 2)
        self.assertEqual(set(top['work_ids']), {'w1', 'w2'})
        # 每一条摘句都要能回查到对应作品原文。
        texts = self.text_by_id()
        for section in ('openers', 'closers'):
            for item in card[section]:
                self.assertIn(item['quote'], texts[item['work_id']], section)
                for example in item['examples']:
                    self.assertIn(example['quote'], texts[example['work_id']])
        # 高频词与水词
        self.assertTrue(any('自媒体' in item['word'] for item in card['keywords']))
        self.assertIn('就是', [item['word'] for item in card['avoid_words']])
        self.assertEqual(card['address']['counts']['你'], 3)
        self.assertEqual(card['address']['counts']['我'], 2)
        self.assertTrue(card['prompt_block'])
        self.assertIn('我告诉你一个秘密', card['prompt_block'])
        self.assertIn('就是(4次)', card['prompt_block'])

    def test_less_than_five_works_is_rejected(self):
        store = self.make_store(WORKS[:2])
        result = style_profile.build_style_card(store, use_llm=False)
        self.assertFalse(result['ok'])
        self.assertIn('样本不足，先采集至少 5 条作品', result['error'])
        self.assertEqual(result['work_count'], 2)
        self.assertEqual(result['required'], 5)
        # 拒绝时不得写档案。
        written = style_profile.generate_and_write(store, use_llm=False)
        self.assertFalse(written['ok'])
        self.assertIsNone(store.get('profile', 'owner'))
        # 已有档案也不能被样本不足的结果污染。
        store.put('profile', {'name': '测试作者', 'style': '短句'}, 'owner', 0)
        style_profile.generate_and_write(store, use_llm=False)
        profile = store.get('profile', 'owner')
        self.assertEqual(profile['name'], '测试作者')
        self.assertNotIn('style_card', profile)

    def test_works_with_empty_text_do_not_count(self):
        works = WORKS[:4] + [('w5', '#话题标签'), ('w6', '   ')]
        store = self.make_store(works)
        result = style_profile.build_style_card(store, use_llm=False)
        self.assertFalse(result['ok'])
        self.assertEqual(result['work_count'], 4)
        self.assertEqual(result['total_work_count'], 6)
        self.assertIn('样本不足，先采集至少 5 条作品', result['error'])

    def test_llm_path_keeps_only_verifiable_quotes(self):
        store = self.make_store()
        texts = self.text_by_id()
        w1 = next(key for key, text in texts.items() if '我告诉你一个秘密' in text)
        w5 = next(key for key, text in texts.items() if '其实' in text)
        w3 = next(key for key, text in texts.items() if '一个人怎么做自媒体' in text)

        def fake_llm(system, payload):
            self.assertIn('禁止编造', system)
            self.assertEqual(payload['work_count'], 6)
            return {
                'observations': [
                    {'trait': '固定开场', 'claim': '常用“我告诉你”开头',
                     'quote': '我告诉你一个秘密', 'work_id': w1},
                    {'trait': '编造结论', 'claim': '编造了一个不存在的句子',
                     'quote': '这句话不在任何作品里', 'work_id': w1},
                ],
                'openers': [{'quote': '一个人怎么做自媒体', 'work_id': w3}],
                'closers': [{'quote': '先坚持三十天再说', 'work_id': w1}],
                'catchphrases': [{'phrase': '其实', 'quote': '其实就是很多人不是不会拍',
                                  'work_id': w5}],
                'rhythm_notes': [{'note': '开头先给结论', 'quote': '我告诉你一个秘密',
                                  'work_id': w1}],
                'avoid_words': [{'word': '就是', 'quote': '就是总想一步到位',
                                 'work_id': w5}],
            }

        stats_only = style_profile.build_style_card(store, use_llm=False)['style_card']
        result = style_profile.build_style_card(store, use_llm=True, llm=fake_llm)
        self.assertTrue(result['ok'])
        card = result['style_card']
        self.assertEqual(card['method'], 'statistics+llm')
        self.assertTrue(card['llm']['ok'])
        self.assertGreaterEqual(card['llm']['valid'], 4)
        self.assertGreaterEqual(card['llm']['rejected'], 1)
        self.assertEqual(len(card['llm_insights']['observations']), 1)
        self.assertEqual(card['llm_insights']['observations'][0]['quote'], '我告诉你一个秘密')
        self.assertNotIn('这句话不在任何作品里', json.dumps(card, ensure_ascii=False))
        # LLM 不得改动统计数字。
        for key in ('sentence', 'address', 'emotion', 'rhythm', 'openers', 'closers'):
            self.assertEqual(card[key], stats_only[key], key)
        # LLM 引用也要逐条回查原文。
        for section in ('observations', 'openers', 'closers', 'catchphrases',
                        'rhythm_notes', 'avoid_words'):
            for item in card['llm_insights'][section]:
                self.assertIn(item['quote'], texts[item['work_id']], section)

    def test_llm_failure_falls_back_to_statistics(self):
        store = self.make_store()

        def broken_llm(system, payload):
            raise RuntimeError('模拟断网')

        result = style_profile.build_style_card(store, use_llm=True, llm=broken_llm)
        self.assertTrue(result['ok'])
        card = result['style_card']
        self.assertEqual(card['method'], 'statistics')
        self.assertIn('模拟断网', card['llm']['error'])
        self.assertFalse(card['llm']['ok'])
        self.assertIsNone(card['llm_insights'])
        self.assertTrue(card['openers'])
        self.assertTrue(card['prompt_block'])

    def test_no_llm_configured_falls_back_without_error(self):
        store = self.make_store()
        with mock.patch.object(style_profile.providers, 'available', return_value={'llm': False}):
            result = style_profile.build_style_card(store, use_llm=True)
        self.assertTrue(result['ok'])
        card = result['style_card']
        self.assertEqual(card['method'], 'statistics')
        self.assertFalse(card['llm']['attempted'])
        self.assertIn('生成模型未配置', card['llm']['reason'])

    def test_llm_returns_bad_shape_falls_back(self):
        store = self.make_store()
        result = style_profile.build_style_card(
            store, use_llm=True, llm=lambda system, payload: '不是 JSON')
        self.assertTrue(result['ok'])
        card = result['style_card']
        self.assertEqual(card['method'], 'statistics')
        self.assertFalse(card['llm']['ok'])
        self.assertEqual(card['llm']['rejected'], 1)
        self.assertTrue(card['prompt_block'])

    def test_write_back_preserves_old_profile_and_no_card_before_write(self):
        store = self.make_store()
        store.put('profile', {
            'name': '测试作者',
            'style': '短句、直接',
            'audience': '想做自媒体的人',
            'redlines': '不承诺涨粉',
        }, 'owner', 0)
        self.assertEqual(style_profile.profile_style_block(store), '')

        result = style_profile.generate_and_write(store, use_llm=False)
        self.assertTrue(result['ok'])
        profile = store.get('profile', 'owner')
        self.assertEqual(profile['name'], '测试作者')
        self.assertEqual(profile['style'], '短句、直接')
        self.assertEqual(profile['audience'], '想做自媒体的人')
        self.assertEqual(profile['redlines'], '不承诺涨粉')
        self.assertEqual(profile['style_card']['schema'], 'style_card.v1')
        self.assertIn('style_card_updated_at', profile)
        self.assertIn('我告诉你一个秘密', style_profile.profile_style_block(store))
        self.assertEqual(set(profile['style_card']['source']['work_ids']),
                         {'w1', 'w2', 'w3', 'w4', 'w5', 'w6'})

    def test_account_filter(self):
        store = self.make_store(WORKS)
        store.put('bench_work', {
            'bench_account_id': 'acct_b',
            'platform': 'douyin',
            'platform_video_id': 'b1',
            'title': '另一个账号的作品文案，用来验证账号隔离。',
        }, 'b1')
        result = style_profile.build_style_card(store, account_id='acct_a', use_llm=False)
        self.assertTrue(result['ok'])
        self.assertEqual(result['style_card']['source']['work_count'], 6)
        self.assertNotIn('b1', result['style_card']['source']['work_ids'])
        other = style_profile.build_style_card(store, account_id='acct_b', use_llm=False)
        self.assertFalse(other['ok'])
        self.assertEqual(other['work_count'], 1)


if __name__ == '__main__':
    unittest.main()
