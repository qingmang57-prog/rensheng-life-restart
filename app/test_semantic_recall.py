"""B4 语义净剪召回补强：用真实词级转写复现「重复短语漏检」，先红后绿。

复现的两条链路缺口（现有 test_semantic.py 的 16 项一条都不覆盖）：
  1) 原位重复：同一句话/同一个词连说两遍、且没有"更完整的重说"可指。现有提示词
     （app.py:622 规则2末句）说这种情况"不是改口，不得删除"，模型因此不报；
     即使报了，semantic_plan_valid（app.py:567-575）也因 repair_word_ids 为空整条丢弃。
  2) 500 词硬切分块（app.py:624-625）：跨块的重复对任何一次模型调用都不可见，
     后面的模型再强也看不到，属于结构性漏检。

真实数据只读来源（词ID/文本/毫秒均为原值，可直接搜索核对）：
  本机剪辑目录下「1027口播」与「35岁选行业」两份 audio.asr.json（分别为 2128 / 2066 词）
预期：本文件 4 项红（InPlaceRepeatTests 3 + ChunkBoundaryRecallTests 1）、1 项绿（FillerRecallTests）。

本文件不调用真实 LLM、不跑 ffmpeg、不启服务；现有测试文件不改。
"""
import json
import os
import time
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

TEST_ROOT = Path(__file__).resolve().parent / '.test-runtime'
TEST_ROOT.mkdir(exist_ok=True)
TEST_PATH = TEST_ROOT / ('recall-' + uuid.uuid4().hex)
TEST_PATH.mkdir()
os.environ['IP_DATA_DIR'] = str(TEST_PATH)
os.environ['IP_SKIP_LOCAL_CONFIG'] = '1'
os.environ['IP_DISABLE_LOCAL_ASR'] = '1'
for _name in ('IP_LLM_URL', 'IP_LLM_MODEL', 'IP_ASR_URL', 'IP_RESOLVER_URL'):
    os.environ.pop(_name, None)
import app  # noqa: E402  (import 前必须先设环境变量)

REAL_1027 = Path('剪辑/1027口播/audio.asr.json')
REAL_35 = Path('剪辑/35岁选行业/audio.asr.json')


def load_words(path):
    return json.loads(path.read_text(encoding='utf-8'))['words']


def slice_by_id(words, ids):
    by_id = {w['id']: w for w in words}
    return [by_id[i] for i in ids]


def proposal(word_ids, kind='repeat', repair=(), reason=''):
    # 模型在"找不到更完整重说"时的典型输出：repair_word_ids 为空。
    return {'deletions': [{'word_ids': list(word_ids), 'type': kind, 'repair_word_ids': list(repair),
                           'reason': reason, 'kept_text': '', 'needs_review': False}]}


class InPlaceRepeatTests(unittest.TestCase):
    """原位重复：模型不指 repair（因为确实没有"更完整的重说"），应由后端回源认保留份。"""

    @classmethod
    def setUpClass(cls):
        if not (REAL_1027.is_file() and REAL_35.is_file()):
            raise unittest.SkipTest('真实词级转写不在本机，跳过召回用例')
        cls.w1027 = load_words(REAL_1027)
        cls.w35 = load_words(REAL_35)

    def test_so_so_stutter_keeps_the_later_copy(self):
        # 1027口播 502.73s："所以所以"（word_1911/1912 与 word_1913/1914 文本相同）。
        words = slice_by_id(self.w1027, ['word_1909', 'word_1910', 'word_1911', 'word_1912',
                                         'word_1913', 'word_1914', 'word_1915'])
        deletions, skipped = app.semantic_plan_valid(
            words, proposal(['word_1911', 'word_1912'], reason='口吃：所以所以'), 20000)
        self.assertEqual(len(deletions), 1, '原位重复被丢弃：%s' % (skipped,))
        item = deletions[0]
        self.assertEqual(item['word_ids'], ['word_1911', 'word_1912'])
        self.assertEqual(item['repair_word_ids'], ['word_1913', 'word_1914'])
        self.assertEqual(item['repair_text'], '所以')
        self.assertEqual(item['intervals'], [[502730, 502870], [502870, 503145]])
        self.assertEqual(item['start_ms'], 502730)
        self.assertEqual(item['end_ms'], 503145)
        self.assertEqual(item['type'], 'repeat')
        self.assertTrue(item['needs_review'], '后端补出的"保留哪一份"必须标先试听')

    def test_triple_stutter_keeps_one_usable_copy(self):
        # 35岁选行业 85.71s：连说四个"慢"（word_455..458），只该留一组。
        words = slice_by_id(self.w35, ['word_453', 'word_454', 'word_455', 'word_456',
                                       'word_457', 'word_458', 'word_459'])
        deletions, skipped = app.semantic_plan_valid(
            words, proposal(['word_455', 'word_456'], reason='口吃：慢慢慢慢'), 20000)
        self.assertEqual(len(deletions), 1, '四连"慢"未被召回：%s' % (skipped,))
        item = deletions[0]
        self.assertEqual(item['repair_word_ids'], ['word_457', 'word_458'])
        self.assertEqual(item['repair_text'], '慢慢')
        self.assertEqual(item['intervals'], [[85710, 85890], [85890, 86110]])

    def test_deleting_the_later_copy_keeps_the_earlier_one(self):
        # 模型也可能选择"删后留前"；后端同样要能回源，而不是整条丢弃。
        words = slice_by_id(self.w1027, ['word_1909', 'word_1910', 'word_1911', 'word_1912',
                                         'word_1913', 'word_1914', 'word_1915'])
        deletions, skipped = app.semantic_plan_valid(
            words, proposal(['word_1913', 'word_1914'], reason='重复'), 20000)
        self.assertEqual(len(deletions), 1, '删后留前被丢弃：%s' % (skipped,))
        item = deletions[0]
        self.assertEqual(item['repair_word_ids'], ['word_1911', 'word_1912'])
        self.assertEqual(item['start_ms'], 503690)

    def test_prompt_states_the_in_place_repeat_rule(self):
        # 提示词段是漏检第一现场，但当前内联在 semantic_cut_job 里、无法回归断言。
        # patch 后应提为模块级 SEMANTIC_PROMPT，并明确"同文本原位重复"也要报。
        self.assertTrue(hasattr(app, 'SEMANTIC_PROMPT'),
                        '提示词段应提为模块级常量 SEMANTIC_PROMPT，否则召回口径无法回归')
        self.assertIn('原位重复', app.SEMANTIC_PROMPT)
        self.assertIn('repair_word_ids', app.SEMANTIC_PROMPT)


class ChunkBoundaryRecallTests(unittest.TestCase):
    """500 词硬切：跨块的重复谁也看不见（真实样本正好卡在第 500 个词）。"""

    @classmethod
    def setUpClass(cls):
        if not REAL_1027.is_file():
            raise unittest.SkipTest('真实词级转写不在本机，跳过召回用例')
        cls.words = load_words(REAL_1027)

    @staticmethod
    def detect(system, payload):
        # 一个"照规则办事"的模型替身：只报它真正看见的相邻同文本重复，并指后一份为保留版。
        # 这里不用真实 LLM（禁花额度），但它不会漏报看得见的东西，所以能单独验分块。
        ws = payload['words']
        text = [w['text'] for w in ws]
        out = []
        i = 0
        while i < len(text):
            for size in (5, 4, 3, 2):
                if i + 2 * size <= len(text) and text[i:i + size] == text[i + size:i + 2 * size]:
                    out.append({'word_ids': [w['id'] for w in ws[i:i + size]],
                                'type': 'repeat',
                                'repair_word_ids': [w['id'] for w in ws[i + size:i + 2 * size]],
                                'reason': '原位重复', 'needs_review': False})
                    i += 2 * size
                    break
            else:
                i += 1
        return {'deletions': out}

    def wait_job(self, job_id, limit=60):
        deadline = time.time() + limit
        while time.time() < deadline:
            job = app.STORE.get('job', job_id)
            if job and job['status'] not in ('queued', 'running'):
                return job
            time.sleep(.05)
        self.fail('job ' + job_id + ' timed out')

    def test_duplicate_split_by_word_500_is_visible_to_the_model(self):
        # 1027口播 131.59s："你收入你收入" = word_500..502 与 word_503..505，
        # 第 500 个词（word_500）正好是分块边界，两个 chunk 各拿到半句。
        span = ['word_500', 'word_501', 'word_502']
        project = {'id': 'p_recall', 'title': '召回', 'draft_changed': False,
                   'confirmed_version_id': 'v_recall'}
        asset = {'id': 'a_recall', 'purpose': 'shooting', 'script_version_id': 'v_recall',
                 'path': str(TEST_PATH / 'not-used.mp4'),
                 'info': {'duration_ms': self.words[-1]['end_ms'] + 200, 'audio': True}}
        app.STORE.put('transcript', {'asset_id': 'a_recall', 'project_id': 'p_recall',
                                     'words': self.words, 'cues': [], 'text': '占位'})
        with patch('providers.available', return_value={'llm': True, 'asr': True}):
            with patch('providers.llm_json', side_effect=self.detect):
                job = app.semantic_cut_job(project, asset)
                result = self.wait_job(job['id'])
        self.assertIn(result['status'], ('succeeded', 'needs_action'), result.get('error'))
        plan = result['result']['plan']
        found = [d['word_ids'] for d in plan['deletions']]
        self.assertIn(span, found,
                      '分块把 %s 和它的重复 copy 切开，模型两头都看不到完整重复；'
                      'deletions=%s' % (span, found[:20]))


class FillerRecallTests(unittest.TestCase):
    """词表里真实存在的语气词（filler）现在就能召回，补强不得回退。"""

    @classmethod
    def setUpClass(cls):
        if not REAL_1027.is_file():
            raise unittest.SkipTest('真实词级转写不在本机，跳过召回用例')
        cls.words = load_words(REAL_1027)

    def test_filler_present_in_word_list_is_still_recalled(self):
        ids = ['word_257']  # 1027口播 66.99s 的"啊"
        words = slice_by_id(self.words, ['word_255', 'word_256', 'word_257', 'word_258'])
        deletions, skipped = app.semantic_plan_valid(
            words, proposal(ids, kind='filler', reason='有声水词'), 20000)
        self.assertEqual(len(deletions), 1, '词表内语气词未被召回：%s' % (skipped,))
        self.assertEqual(deletions[0]['type'], 'filler')
        self.assertEqual(deletions[0]['text'], '啊')
        self.assertEqual(deletions[0]['repair_word_ids'], [])


if __name__ == '__main__':
    try:
        unittest.main(verbosity=2)
    finally:
        app.POOL.shutdown(wait=False)
