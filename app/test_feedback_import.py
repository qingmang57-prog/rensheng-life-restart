"""创作者中心导出表格回采（P0-4）：不联网、不依赖平台接口。

验收口径：导入一份真实格式的导出表后，platform_work / metric_snapshot / review_insight
必须真正落库并能算出结论；坏行不吞、逐行给可读原因。
"""
import os
import tempfile
import unittest
import uuid
from pathlib import Path

TEST_ROOT = Path(__file__).resolve().parent/'.test-runtime'
TEST_ROOT.mkdir(exist_ok=True)
TEST_PATH = TEST_ROOT/('feedback-import-'+uuid.uuid4().hex)
TEST_PATH.mkdir()
os.environ['IP_DATA_DIR'] = str(TEST_PATH)
os.environ['IP_SKIP_LOCAL_CONFIG'] = '1'
for name in ('IP_LLM_URL', 'IP_LLM_MODEL', 'IP_ASR_URL', 'IP_RESOLVER_URL'):
    os.environ.pop(name, None)

import contracts
import feedback_import
from db import Store

HEADER = ['作品名称', '作品链接', '发布时间', '播放量', '点赞量', '评论量',
          '分享量', '收藏量', '完播率', '平均播放时长', '视频时长']
ROWS = [
    ['重启第 1 天：我把闹钟砸了', 'https://www.douyin.com/video/7411111111111111111',
     '2026-11-01', '1.2万', '328', '41', '12', '9', '45.6%', '00:32', '01:05'],
    ['重启第 2 天：先做最小的一步', 'https://www.douyin.com/video/7422222222222222222',
     '2026-11-02', '3,480', '96', '7', '3', '2', '21.3%', '00:12', '00:58'],
    ['重启第 3 天：不靠意志力', 'https://www.douyin.com/video/7433333333333333333',
     '2026-11-03', '8765', '155', '18', '5', '4', '33.0%', '00:19', '01:12'],
    ['合计', '', '', '24125', '579', '66', '20', '15', '', '', ''],
    ['没有链接的一条', '', '', '100', '1', '0', '0', '0', '', '', ''],
]


def write_xlsx(path, header=HEADER, rows=ROWS, leading_rows=2):
    from openpyxl import Workbook
    book = Workbook()
    sheet = book.active
    sheet.title = '作品数据'
    for _ in range(leading_rows):          # 真实导出前面常有标题/时间范围说明行
        sheet.append(['创作者中心 · 作品数据导出'])
    sheet.append(header)
    for row in rows:
        sheet.append(row)
    book.save(path)
    return path


class ImportUnitTests(unittest.TestCase):
    def test_parse_number_handles_wan_percent_and_clock(self):
        self.assertEqual(feedback_import.parse_number('1.2万'), 12000)
        self.assertEqual(feedback_import.parse_number('3,480'), 3480)
        self.assertEqual(feedback_import.parse_number('1.5亿'), 1.5e8)
        self.assertEqual(feedback_import.parse_number('45.6%'), 45.6)
        self.assertEqual(feedback_import.parse_number('00:32'), 32)
        self.assertEqual(feedback_import.parse_number('01:02:03'), 3723)
        self.assertIsNone(feedback_import.parse_number('-'))
        self.assertIsNone(feedback_import.parse_number(''))

    def test_header_map_does_not_confuse_play_with_watch_time(self):
        mapping = feedback_import._header_map(HEADER)
        self.assertEqual(mapping['play_count'], HEADER.index('播放量'))
        self.assertEqual(mapping['avg_watch_seconds'], HEADER.index('平均播放时长'))
        self.assertEqual(mapping['duration_seconds'], HEADER.index('视频时长'))
        self.assertEqual(mapping['completion_rate'], HEADER.index('完播率'))

    def test_video_id_parsed_from_url_and_explicit_id(self):
        self.assertEqual(feedback_import.parse_video_id(
            'https://www.douyin.com/video/7411111111111111111?x=1'), '7411111111111111111')
        self.assertEqual(feedback_import.parse_video_id(
            'https://www.douyin.com/user/x?modal_id=7455555555555555555'),
            '7455555555555555555')
        self.assertEqual(feedback_import.parse_video_id('', '7466666666666666666'),
                         '7466666666666666666')

    def test_missing_file_fails_with_readable_reason(self):
        with self.assertRaises(ValueError) as ctx:
            feedback_import.parse_export(str(TEST_PATH/'nope.xlsx'))
        self.assertIn('找不到文件', str(ctx.exception))


class ImportExportTests(unittest.TestCase):
    def setUp(self):
        self.store = Store(str(TEST_PATH/('db-'+uuid.uuid4().hex)))
        self.project = self.store.put('project', {'id': contracts.new_id('project'),
                                                  'title': '重启 21 天实验'})

    def test_import_writes_works_snapshots_and_insight(self):
        path = write_xlsx(TEST_PATH/'export-douyin.xlsx')
        result = feedback_import.import_export(
            self.store, self.project['id'], str(path), '2026-11-16T02:00:00.000Z')
        report = result['report']
        self.assertEqual(report['header_row'], 3)
        self.assertEqual(report['sheet'], '作品数据')
        self.assertEqual(report['rows_total'], 4)           # 合计行在解析阶段已剔除
        self.assertEqual(report['rows_ok'], 3)
        self.assertEqual(report['rows_failed'], 1)          # 「没有链接的一条」
        self.assertEqual(report['works_created'], 3)
        self.assertEqual(report['snapshots_created'], 24)   # 3 作品 × 8 指标
        self.assertEqual(report['status'], 'partial')
        self.assertTrue(any('汇总行' in w for w in report['warnings']))
        self.assertEqual(report['errors'][0]['title'], '没有链接的一条')
        self.assertIn('没有作品链接', report['errors'][0]['reason'])

        works = self.store.list(contracts.KIND_PLATFORM_WORK)
        self.assertEqual(len(works), 3)
        first = next(w for w in works if w['platform_video_id'] == '7411111111111111111')
        self.assertEqual(first['platform'], 'douyin')
        self.assertEqual(first['project_id'], self.project['id'])
        self.assertEqual(first['registration_mode'], 'excel_import')
        self.assertEqual(first['source_url'],
                         'https://www.douyin.com/video/7411111111111111111')
        self.assertEqual(first['play_count'], 12000)
        self.assertEqual(first['like_count'], 328)
        self.assertEqual(first['raw']['excel_import']['row'], 4)

        snapshots = self.store.list(contracts.KIND_METRIC_SNAPSHOT)
        self.assertEqual(len(snapshots), 24)
        ids = {s['id'] for s in snapshots}
        self.assertEqual(len(ids), 24)                      # 每作品每条指标各自唯一
        play = next(s for s in snapshots
                    if s['metric_key'] == 'douyin.play' and s['value'] == 12000)
        self.assertEqual(play['unit'], 'count')
        self.assertEqual(play['source'], 'excel_export')
        self.assertEqual(play['platform_work_id'], first['id'])
        self.assertEqual(play['dimensions']['work_title'], '重启第 1 天：我把闹钟砸了')
        rate = next(s for s in snapshots if s['metric_key'] == 'douyin.completion_rate'
                    and s['platform_work_id'] == first['id'])
        self.assertEqual(rate['value'], 45.6)
        self.assertEqual(rate['unit'], 'percent')

        insights = self.store.list(contracts.KIND_REVIEW_INSIGHT)
        self.assertEqual(len(insights), 1)
        insight = insights[0]
        self.assertEqual(insight['project_id'], self.project['id'])
        self.assertEqual(insight['insight_type'], 'data')
        self.assertEqual(insight['status'], 'candidate')
        self.assertEqual(insight['created_by'], 'rule_engine')
        self.assertEqual(insight['validation'], 'unverified')
        self.assertIn('规则计算', insight['markers'])
        self.assertEqual(insight['source'], 'excel_export')
        self.assertGreaterEqual(insight['evidence_count'], 2)
        for evidence_id in insight['evidence_ids']:
            self.assertIsNotNone(self.store.get(contracts.KIND_METRIC_SNAPSHOT, evidence_id))
        claims = ' '.join(f['claim'] for f in insight['findings'])
        self.assertIn('播放量最高', claims)
        self.assertIn('完播率最高', claims)
        self.assertIn('重启第 1 天', claims)                # 12000 播放最高
        self.assertEqual(len(insight['platform_work_ids']), 3)
        self.assertNotIn('platform_work_urls', insight)   # 由 list_insights 按作品现算
        for work_id in insight['platform_work_ids']:
            self.assertIsNotNone(self.store.get(contracts.KIND_PLATFORM_WORK, work_id))
        self.assertEqual(report['insight_id'], insight['id'])

        saved = self.store.get(feedback_import.REPORT_KIND, report['id'])
        self.assertEqual(saved['rows_ok'], 3)
        self.assertEqual(saved['errors'][0]['row'], 8)

    def test_reimport_updates_works_without_duplicates_and_adds_snapshots(self):
        path = write_xlsx(TEST_PATH/'export-twice.xlsx')
        feedback_import.import_export(self.store, self.project['id'], str(path),
                                      '2026-11-16T02:00:00.000Z')
        second = feedback_import.import_export(self.store, self.project['id'], str(path),
                                              '2026-11-16T03:00:00.000Z')['report']
        self.assertEqual(second['works_created'], 0)
        self.assertEqual(second['works_updated'], 3)
        self.assertEqual(second['snapshots_created'], 24)
        self.assertEqual(len(self.store.list(contracts.KIND_PLATFORM_WORK)), 3)
        self.assertEqual(len(self.store.list(contracts.KIND_METRIC_SNAPSHOT)), 48)
        self.assertEqual(len(self.store.list(contracts.KIND_REVIEW_INSIGHT)), 2)

    def test_csv_export_with_alias_headers_imports(self):
        path = TEST_PATH/'export.csv'
        path.write_text('\n'.join([
            '标题,视频链接,播放次数,点赞数',
            '只有一条的作品,https://www.douyin.com/video/7477777777777777777,"2,000",30',
        ]), encoding='utf-8')
        report = feedback_import.import_export(
            self.store, self.project['id'], str(path), '2026-11-16T04:00:00.000Z')['report']
        self.assertEqual(report['rows_ok'], 1)
        self.assertEqual(report['snapshots_created'], 2)
        self.assertEqual(report['status'], 'ok')
        work = self.store.list(contracts.KIND_PLATFORM_WORK)[0]
        self.assertEqual(work['play_count'], 2000)
        self.assertEqual(work['title'], '只有一条的作品')

    def test_work_owned_by_other_project_is_not_hijacked(self):
        path = write_xlsx(TEST_PATH/'export-other.xlsx', rows=ROWS[:1])
        other = self.store.put('project', {'id': contracts.new_id('project'),
                                           'title': '另一个项目'})
        first = feedback_import.import_export(self.store, other['id'], str(path),
                                             '2026-11-16T02:00:00.000Z')['report']
        self.assertEqual(first['rows_ok'], 1)
        snapshots_before = len(self.store.list(contracts.KIND_METRIC_SNAPSHOT))
        report = feedback_import.import_export(
            self.store, self.project['id'], str(path), '2026-11-16T05:00:00.000Z')['report']
        self.assertEqual(report['rows_ok'], 0)
        self.assertEqual(report['status'], 'failed')
        self.assertIn('已归属其他项目', report['errors'][0]['reason'])
        self.assertEqual(len(self.store.list(contracts.KIND_METRIC_SNAPSHOT)), snapshots_before)
        work = self.store.list(contracts.KIND_PLATFORM_WORK)[0]
        self.assertEqual(work['project_id'], other['id'])

    def test_no_header_fails_with_readable_reason(self):
        path = write_xlsx(TEST_PATH/'export-no-header.xlsx', header=['随便', '写点'],
                          rows=[['a', 'b']], leading_rows=0)
        with self.assertRaises(ValueError) as ctx:
            feedback_import.import_export(self.store, self.project['id'], str(path))
        self.assertIn('作品数据表头', str(ctx.exception))

    def test_unknown_project_fails(self):
        path = write_xlsx(TEST_PATH/'export-unknown.xlsx')
        with self.assertRaises(ValueError) as ctx:
            feedback_import.import_export(self.store, 'project_missing', str(path))
        self.assertIn('项目不存在', str(ctx.exception))


if __name__ == '__main__':
    unittest.main()
