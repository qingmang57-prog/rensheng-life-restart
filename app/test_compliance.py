"""违禁词审核自测 + 真实口播验收。

运行：
    python -m unittest test_compliance -v
    python test_compliance.py --acceptance          # 纯规则
    python test_compliance.py --acceptance --llm    # 规则 + LLM 复核

口径：
- 命中率 = 应命中句子里至少命中一个预期词的条数 / 应命中句子总数。
- 误杀率（block） = 正常句子被判 block 的条数 / 正常句子总数。
- 误杀率（任何级别） = 正常句子出现任何命中的条数 / 正常句子总数。
"""
import os
import sys
import unittest

import compliance


CASES = [
    {'id': 'c01', 'kind': 'positive', 'text': '全网最低价，今天不拍明天就涨。',
     'expect': ['全网最低'], 'want': 'block', 'note': '极限词'},
    {'id': 'c02', 'kind': 'positive', 'text': '国家级认证的配方，敏感肌也能用。',
     'expect': ['国家级'], 'want': 'block', 'note': '法定绝对化用语'},
    {'id': 'c03', 'kind': 'positive', 'text': '这款产品绝对安全，老人小孩都能用。',
     'expect': ['绝对'], 'want': 'block', 'note': '安全性断言'},
    {'id': 'c04', 'kind': 'positive', 'text': '100%有效，三天见效。',
     'expect': ['100%'], 'want': 'block', 'note': '效果承诺'},
    {'id': 'c05', 'kind': 'positive', 'text': '这个茶能根治糖尿病，还没有副作用。',
     'expect': ['根治'], 'want': 'block', 'note': '医疗宣称'},
    {'id': 'c06', 'kind': 'positive', 'text': '专家推荐，药到病除。',
     'expect': ['药到病除'], 'want': 'block', 'note': '医疗宣称'},
    {'id': 'c07', 'kind': 'positive', 'text': '跟着我做，稳赚不赔，日入过万。',
     'expect': ['稳赚'], 'want': 'block', 'note': '金融承诺'},
    {'id': 'c08', 'kind': 'positive', 'text': '保本保收益，年化15%，零风险。',
     'expect': ['保本'], 'want': 'block', 'note': '金融承诺'},
    {'id': 'c09', 'kind': 'positive', 'text': '想要的姐妹加我微信，主页有惊喜。',
     'expect': ['加我微信'], 'want': 'block', 'note': '平台导流'},
    {'id': 'c10', 'kind': 'positive', 'text': '资料我私信发你，V我一下。',
     'expect': ['V我'], 'want': 'block', 'note': '平台导流'},
    {'id': 'c11', 'kind': 'positive', 'text': '这是官方唯一指定的合作机构。',
     'expect': ['唯一'], 'want': 'block', 'note': '绝对化用语'},
    {'id': 'c12', 'kind': 'positive', 'text': '我们的课程保过，包就业，不过全额退。',
     'expect': ['保过'], 'want': 'block', 'note': '教育培训承诺'},
    {'id': 'c13', 'kind': 'positive', 'text': '七天速成，零基础也能月入过万。',
     'expect': ['速成'], 'want': 'warn', 'note': '速成承诺'},
    {'id': 'c14', 'kind': 'positive', 'text': '这个类目我们销量第一，行业领先。',
     'expect': ['销量第一'], 'want': 'block', 'note': '排名断言'},
    {'id': 'c15', 'kind': 'positive', 'text': '这是史上最强的清洁剂，没有之一。',
     'expect': ['最强'], 'want': 'block', 'note': '极限词'},
    {'id': 'c16', 'kind': 'positive', 'text': '这批货永久有效，终身售后。',
     'expect': ['永久'], 'want': 'warn', 'note': '期限承诺'},
    {'id': 'c17', 'kind': 'positive', 'text': '有问题打我电话 13800138000。',
     'expect': ['13800138000'], 'want': 'block', 'note': '手机号正则'},
    {'id': 'c18', 'kind': 'positive', 'text': '支持货到付款，也可以线下交易。',
     'expect': ['货到付款'], 'want': 'block', 'note': '私下交易'},
    {'id': 'c19', 'kind': 'positive', 'text': '想要资料看个性签名，扫码进群。',
     'expect': ['看个性签名'], 'want': 'block', 'note': '导流变体'},
    {'id': 'c20', 'kind': 'positive', 'text': '月入十万不是梦，三个月回本。',
     'expect': ['月入十万'], 'want': 'block', 'note': '收入承诺'},
    {'id': 'c21', 'kind': 'positive', 'text': '我保证你一个月瘦二十斤，绝不反弹。',
     'expect': ['保证你一个月瘦'], 'want': 'block', 'note': '效果保证正则'},
    {'id': 'c22', 'kind': 'positive', 'text': '年化收益15%，稳赚不赔。',
     'expect': ['年化收益'], 'want': 'warn', 'note': '收益提示'},
    {'id': 'c23', 'kind': 'positive', 'text': '专家说这个能降血压，无副作用。',
     'expect': ['降血压'], 'want': 'block', 'note': '疾病治疗宣称'},
    {'id': 'c24', 'kind': 'positive', 'text': '这个项目有内幕消息，跟我买。',
     'expect': ['内幕消息'], 'want': 'block', 'note': '荐股'},
    {'id': 'c25', 'kind': 'positive', 'text': '扫一扫二维码，进粉丝群领福利。',
     'expect': ['扫一扫'], 'want': 'block', 'note': '扫码导流'},
    {'id': 'c26', 'kind': 'positive', 'text': '我们是独家首发，仅此一家。',
     'expect': ['独家'], 'want': 'warn', 'note': '独家+仅此一家'},
    {'id': 'c27', 'kind': 'context', 'text': '央视推荐，国家级教练带队。',
     'expect': ['国家级'], 'want': 'hint', 'note': '称号需证明，降级'},
    {'id': 'c28', 'kind': 'context', 'text': '100%纯棉，宝宝穿着放心。',
     'expect': ['100%'], 'want': 'hint', 'note': '成分事实，降级'},
    {'id': 'c29', 'kind': 'context', 'text': '这是国家级非物质文化遗产。',
     'expect': ['国家级'], 'want': 'hint', 'note': '依法评定称号，降级'},
    {'id': 'c30', 'kind': 'context', 'text': '广告法不让说“第一”，大家写稿注意。',
     'expect': ['第一'], 'want': 'hint', 'note': '引号引用，降级'},
    {'id': 'c31', 'kind': 'negative', 'text': '第一时间回复大家，第一印象很重要。',
     'expect': [], 'note': '顺序/初次'},
    {'id': 'c32', 'kind': 'negative', 'text': '这是我唯一一次创业，也是最好吃的一顿。',
     'expect': [], 'note': '唯一一次+最好吃'},
    {'id': 'c33', 'kind': 'negative', 'text': '我第一眼就喜欢这个设计，绝对值。',
     'expect': [], 'note': '第一眼+绝对值'},
    {'id': 'c34', 'kind': 'negative', 'text': '最好不要熬夜，身体最重要。',
     'expect': [], 'note': '最好别/最好不'},
    {'id': 'c35', 'kind': 'negative', 'text': '投资有风险，入市需谨慎。',
     'expect': [], 'note': '风险提示，不是无风险'},
    {'id': 'c36', 'kind': 'negative', 'text': '关注我，评论区留言，直播间见。',
     'expect': [], 'note': '正常互动'},
    {'id': 'c37', 'kind': 'negative', 'text': '我主页有商品橱窗，点进去就能买。',
     'expect': [], 'note': '平台内购物'},
    {'id': 'c38', 'kind': 'negative', 'text': '第一次做视频，有点紧张。',
     'expect': [], 'note': '第一次'},
    {'id': 'c39', 'kind': 'negative', 'text': '唯一的问题是价格有点贵。',
     'expect': [], 'note': '口语转折'},
    {'id': 'c40', 'kind': 'negative', 'text': '医院都治不好，还是要听医生的。',
     'expect': [], 'note': '否定医疗宣称'},
    {'id': 'c41', 'kind': 'negative', 'text': '这是我们家的祖传配方，不外卖。',
     'expect': [], 'note': '无违规'},
    {'id': 'c42', 'kind': 'negative', 'text': '三天打鱼两天晒网，账号做不起来。',
     'expect': [], 'note': '三天不是三天见效'},
    {'id': 'c43', 'kind': 'negative', 'text': '我只是个普通宝妈，分享真实使用感受。',
     'expect': [], 'note': '无违规'},
    {'id': 'c44', 'kind': 'negative', 'text': '这个杯子保温效果不错，适合冬天用。',
     'expect': [], 'note': '正常分享'},
    {'id': 'c45', 'kind': 'negative', 'text': '他排在全班第一，但我不认识他。',
     'expect': [], 'note': '限定范围客观排名'},
    {'id': 'c46', 'kind': 'negative', 'text': '我是第一次来杭州，这边风景很好。',
     'expect': [], 'note': '第一次'},
    {'id': 'c47', 'kind': 'negative', 'text': '这是我们第一次合作，请多关照。',
     'expect': [], 'note': '第一次'},
    {'id': 'c48', 'kind': 'negative', 'text': '最好别在晚上喝浓茶。',
     'expect': [], 'note': '最好别'},
    {'id': 'c49', 'kind': 'negative', 'text': '这个方案绝对值不值得？看你预算。',
     'expect': [], 'note': '绝对值'},
    {'id': 'c50', 'kind': 'negative', 'text': '第一人称视角拍视频更有代入感。',
     'expect': [], 'note': '第一人称'},
]


ACCEPTANCE = [
    {'text': '姐妹们，这款精华我用了三个月，皮肤状态真的稳了。', 'expect': [], 'note': '正常分享'},
    {'text': '全网最低价，今天不拍明天就涨。', 'expect': ['全网最低'], 'note': '极限词'},
    {'text': '国家级认证的配方，敏感肌也能用。', 'expect': ['国家级'], 'note': '极限词'},
    {'text': '我保证你一个月瘦二十斤，绝不反弹。', 'expect': ['保证你一个月瘦'], 'note': '效果保证'},
    {'text': '这个茶能根治糖尿病，还没有副作用。', 'expect': ['根治'], 'note': '医疗宣称'},
    {'text': '专家推荐，药到病除，三天见效。', 'expect': ['药到病除'], 'note': '医疗宣称'},
    {'text': '跟着我做，稳赚不赔，日入过万。', 'expect': ['稳赚'], 'note': '金融承诺'},
    {'text': '保本保收益，年化15%，零风险。', 'expect': ['保本'], 'note': '金融承诺'},
    {'text': '想要的姐妹加我微信，主页有惊喜。', 'expect': ['加我微信'], 'note': '平台导流'},
    {'text': '资料我私信发你，V我一下。', 'expect': ['V我'], 'note': '平台导流'},
    {'text': '这是官方唯一指定的合作机构。', 'expect': ['唯一'], 'note': '极限词'},
    {'text': '我们的课程保过，包就业，不过全额退。', 'expect': ['保过'], 'note': '教育承诺'},
    {'text': '这个类目我们销量第一，行业领先。', 'expect': ['销量第一'], 'note': '排名断言'},
    {'text': '有问题打我电话 13800138000。', 'expect': ['13800138000'], 'note': '手机号'},
    {'text': '第一时间回复大家，第一印象很重要。', 'expect': [], 'note': '正常句子'},
    {'text': '这是我唯一一次创业，也是最好吃的一顿。', 'expect': [], 'note': '正常句子'},
    {'text': '我第一眼就喜欢这个设计，绝对值。', 'expect': [], 'note': '正常句子'},
    {'text': '投资有风险，入市需谨慎。', 'expect': [], 'note': '正常句子'},
    {'text': '关注我，评论区留言，我在直播间等你。', 'expect': [], 'note': '正常句子'},
    {'text': '我主页有商品橱窗，点进去就能买。', 'expect': [], 'note': '正常句子'},
]


def _matching(hits, expected):
    return [h for h in hits if any(word in h['term'] for word in expected)]


def _rate(part, whole):
    return 0.0 if not whole else part / whole * 100


def compute_metrics(reports, cases):
    positives = [c for c in cases if c['kind'] == 'positive']
    contexts = [c for c in cases if c['kind'] == 'context']
    negatives = [c for c in cases if c['kind'] == 'negative']
    pos_hit = sum(1 for c in positives if _matching(reports[c['id']]['hits'], c['expect']))
    neg_block = sum(1 for c in negatives if any(h['level'] == 'block' for h in reports[c['id']]['hits']))
    neg_any = sum(1 for c in negatives if reports[c['id']]['hits'])
    ctx_pass = sum(
        1 for c in contexts
        if _matching(reports[c['id']]['hits'], c['expect'])
        and all(h['level'] == 'hint' for h in _matching(reports[c['id']]['hits'], c['expect']))
    )
    return {
        'positive': len(positives), 'positive_hit': pos_hit,
        'negative': len(negatives), 'negative_block': neg_block, 'negative_any': neg_any,
        'context': len(contexts), 'context_pass': ctx_pass,
        'hit_rate': _rate(pos_hit, len(positives)),
        'fp_block_rate': _rate(neg_block, len(negatives)),
        'fp_any_rate': _rate(neg_any, len(negatives)),
    }


class LexiconTest(unittest.TestCase):
    def test_every_entry_has_resolvable_source(self):
        raw = compliance._LEXICON
        self.assertGreaterEqual(len(raw['entries']), 40, '词条数量过少，覆盖不足')
        ids = set()
        for entry in raw['entries']:
            self.assertNotIn(entry['id'], ids, '词条 id 重复：%s' % entry['id'])
            ids.add(entry['id'])
            self.assertIn(entry['basis'], raw['sources'])
            source = raw['sources'][entry['basis']]
            self.assertTrue(source['url'].startswith('http'), '%s 来源无 URL' % entry['id'])
            self.assertRegex(source['retrieved_at'], r'^\d{4}-\d{2}-\d{2}$')
            self.assertTrue(entry['category'])
            self.assertTrue(1 <= len(entry['suggestions']) <= 2)

    def test_lexicon_meta_exposes_sources(self):
        meta = compliance.lexicon_meta()
        self.assertTrue(meta['version'])
        self.assertGreaterEqual(meta['source_count'], 8)
        for source in meta['sources']:
            self.assertTrue(source['url'])
            self.assertTrue(source['retrieved_at'])


class RuleTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.reports = {c['id']: compliance.check(c['text']) for c in CASES}

    def test_cases(self):
        for case in CASES:
            report = self.reports[case['id']]
            hits = report['hits']
            if case['kind'] == 'negative':
                self.assertEqual(hits, [], '%s 误杀：%s' % (case['id'], case['note']))
                continue
            matched = _matching(hits, case['expect'])
            self.assertTrue(matched, '%s 未命中：%s' % (case['id'], case['text']))
            self.assertEqual(matched[0]['level'], case['want'],
                             '%s 级别不符：期望 %s 实际 %s' % (case['id'], case['want'], matched[0]['level']))

    def test_every_hit_has_usable_suggestions(self):
        for case in CASES:
            for hit in self.reports[case['id']]['hits']:
                self.assertTrue(1 <= len(hit['suggestions']) <= 2)
                self.assertTrue(all(x.strip() for x in hit['suggestions']))

    def test_exclusions_are_traceable(self):
        report = compliance.check('第一时间回复大家。')
        self.assertEqual(report['hits'], [])
        self.assertTrue(any(x['term'] == '第一' for x in report['excluded']))

    def test_metrics(self):
        metrics = compute_metrics(self.reports, CASES)
        print('\n[30+ 用例] 命中率 %.1f%% (%d/%d)，block 误杀率 %.1f%% (%d/%d)，'
              '任何级别误杀率 %.1f%% (%d/%d)，语境降级通过 %d/%d' % (
                  metrics['hit_rate'], metrics['positive_hit'], metrics['positive'],
                  metrics['fp_block_rate'], metrics['negative_block'], metrics['negative'],
                  metrics['fp_any_rate'], metrics['negative_any'], metrics['negative'],
                  metrics['context_pass'], metrics['context']))
        self.assertEqual(metrics['positive_hit'], metrics['positive'])
        self.assertEqual(metrics['negative_block'], 0)
        self.assertEqual(metrics['negative_any'], 0)
        self.assertEqual(metrics['context_pass'], metrics['context'])


class DegradeTest(unittest.TestCase):
    def test_no_llm_keeps_rules(self):
        saved = {key: os.environ.pop(key, None) for key in ('IP_LLM_URL', 'IP_LLM_MODEL', 'IP_LLM_KEY')}
        try:
            report = compliance.check('全网最低价', use_llm=True)
            self.assertEqual(report['mode'], 'rules')
            self.assertEqual(report['llm']['status'], 'unconfigured')
            self.assertTrue(any('全网最低' in h['term'] for h in report['hits']))
        finally:
            for key, value in saved.items():
                if value is not None:
                    os.environ[key] = value

    def test_llm_failure_keeps_rules(self):
        import providers
        original = providers.llm_json
        saved = {key: os.environ.get(key) for key in ('IP_LLM_URL', 'IP_LLM_MODEL')}
        providers.llm_json = lambda *a, **k: (_ for _ in ()).throw(RuntimeError('模拟断网'))
        os.environ['IP_LLM_URL'] = 'https://example.invalid/v1'
        os.environ['IP_LLM_MODEL'] = 'test-model'
        try:
            report = compliance.check('全网最低价', use_llm=True)
            self.assertEqual(report['mode'], 'rules')
            self.assertEqual(report['llm']['status'], 'failed')
            self.assertTrue(any('全网最低' in h['term'] for h in report['hits']))
        finally:
            providers.llm_json = original
            for key, value in saved.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value


def run_acceptance(use_llm=False):
    texts = [item['text'] for item in ACCEPTANCE]
    reports = compliance.check_many(texts, use_llm=use_llm)
    print('\n## 20 条真实口播验收（%s）\n' % ('规则 + LLM' if use_llm else '纯规则'))
    print('| # | 句子 | 期望 | 实际命中 | 级别 | 判定 | 首条替换建议 |')
    print('|---|---|---|---|---|---|---|')
    positive = negative = hit = fp_block = fp_any = 0
    for index, (item, report) in enumerate(zip(ACCEPTANCE, reports), 1):
        matched = _matching(report['hits'], item['expect'])
        if item['expect']:
            positive += 1
            hit += bool(matched)
            outcome = '命中' if matched else '未命中'
        else:
            negative += 1
            if any(h['level'] == 'block' for h in report['hits']):
                outcome = '误杀(block)'
                fp_block += 1
            elif report['hits']:
                outcome = '误杀(非block)'
                fp_any += 1
            else:
                outcome = '未命中'
        levels = ', '.join(sorted({h['level'] for h in report['hits']})) or '-'
        terms = '、'.join(h['term'] for h in report['hits'][:3]) or '-'
        suggestion = report['hits'][0]['suggestions'][0] if report['hits'] else '-'
        print('| %d | %s | %s | %s | %s | %s | %s |' % (
            index, item['text'], '、'.join(item['expect']) or '无', terms, levels, outcome, suggestion))
    print('\n命中率：%d/%d = %.1f%%' % (hit, positive, _rate(hit, positive)))
    print('误杀率（block）：%d/%d = %.1f%%' % (fp_block, negative, _rate(fp_block, negative)))
    print('误杀率（任何级别）：%d/%d = %.1f%%' % (fp_any, negative, _rate(fp_any, negative)))
    statuses = sorted({str(r['llm'].get('status')) for r in reports})
    print('LLM 状态：%s；模式：%s' % (statuses, sorted({r['mode'] for r in reports})))
    return reports


def load_local_config():
    """和 app.py 一致地读取 .env.local，让 --acceptance --llm 与主程序行为对齐。"""
    from pathlib import Path
    path = Path(__file__).resolve().parent / '.env.local'
    if not path.is_file() or os.getenv('IP_SKIP_LOCAL_CONFIG') == '1':
        return
    for line in path.read_text(encoding='utf-8-sig').splitlines():
        if not line.strip() or line.lstrip().startswith('#'):
            continue
        name, sep, value = line.partition('=')
        if sep and name.strip().startswith('IP_') and value.strip():
            os.environ.setdefault(name.strip(), value.strip().strip('"\''))


if __name__ == '__main__':
    load_local_config()
    if '--acceptance' in sys.argv:
        run_acceptance(use_llm='--llm' in sys.argv)
    else:
        unittest.main()
