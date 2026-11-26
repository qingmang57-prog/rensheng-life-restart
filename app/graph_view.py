# -*- coding: utf-8 -*-
"""成长图谱的碎片数据（只读，独立文件）。

为什么单独一个文件：app.py 正由另一条线在改，这里只新增文件、不碰主程序。
生效只需要在 app.py 里加两行（一行 import、一行路由）：

    import graph_view                     # 放在 app.py 顶部其它模块 import 旁边
    if graph_view.route(self,method,bits,query,STORE):return   # 放在 route() 里 growth_service.route 之后

路由：
    GET /api/graph/cards    图谱要用的碎片节点、分类簇、目标与支柱

口径：
- 碎片 = 第二大脑知识池（content_index 里 pool='pool'、非示例、有效），
  也就是首页「全部 N 条」那批，不改写原文、不写任何记录。
- 归属支柱按 assets/growth_pillar_mapping.json 的分类映射；映射为 null 的归「未归类」。
- 卡片本身没有手动关联目标（goal_ids 为空）时，不伪造归属，只给映射结果。
"""
from __future__ import annotations

import json
import re
import sqlite3
import time
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

TZ = timezone(timedelta(hours=8))

SUMMARY_LIMIT = 180     # 摘要上限（字），点开详情看完整内容
CARDS_LIMIT = 800       # 兜底上限，正常 195 条不会碰到
MAX_KEYWORDS = 10       # 每张卡取前 10 个关键词
MENTION_MIN = 3         # 提及边：标题/标题关键词至少 3 字，避免「价值」这种短词误连
CO_DF_CAP = .20         # 共现边：df 超过 20% 的泛词不参与判定（提及边不受影响）

# 词表：从这 195 张卡的标题/正文里按词频挖出来的名词与动名词（df 2–15%，已去虚词与碎片词）。
# 运行时不依赖 jieba：用最长匹配在这张词表上切词，切出来的每个词都能回指到正文原句。
KEYWORD_TEXT = """
万粉 三观 专业 专门 世界 世界观 个人 个体 中国 临场 主义 主题 习惯 事业 事实 事物 互联网 交学费 交易 产品 产物 亲身经历 人人 人家 人性 人格 人格结构 人生 人生观 人类 人群
代价 代码 价值观 价格 任务 企业 企业管理者 休谟 优先 优势 优化 传播 传染 估值 低谷 体力 体系 体面 作品 例子 供给 供需 信任 信号 信息 借口 假设 偏差 做人 储蓄 先动 先存
先守 免费 入口 入门 全球 全面 公司 公平 共同体 关系 关键 兴趣 农夫 冲动 冲突 决策 准则 凡勃伦 出口 分工 分配 刚性 创业 创业者 初学者 判断力 判断能力 利益 刻度 剧本 力气
力量 办法 功利 动作 动力 动手 动机 劳动 勇气 勒庞 区分 升级 单位 博士 卡住 危害 危机 历史 压力 原则 原地 原理 参照系 反应 发作 发展 受众 变化 变量 口号 口碑 口粮 合作
合理 同情 同源 同理 同类 同质化 名声 名字 后人 后果 员工 命运 品格 品质 商业 商业模式 噪音 因势 因素 团队 困境 困难 国家 圈子 地位 地图 地基 场合 场景 城市 基础 基金
声誉 声音 处境 大事 大脑 大钱 天使 天气 天生 天花板 天赋 失灵 奖励 媒体 存款 存量 孙子兵法 学会 学问 宁可 安全感 安静 宏观 定义 定价 定价权 定力 定论 宝典 实验 客户 客气
家人 家庭 密度 富兰克林 小事 小心 小时 小时数 局面 层面 屠夫 岗位 工作 工具 工资 差距 市场 帮人 常识 年轻人 年龄 幻觉 广告 库存 底层 底气 底牌 底线 建议 开口 弱者 强度
强者 形式 形状 影响 影响力 循环 心态 心性 心智 心理 心理健康 心理学 快车道 念头 思想 思维 性格 性质 怨气 总结 恐惧 情形 情感 情绪 想象 意志 意志力 意见 意识 感觉 成本
成果 战史 战略 战略思维 战胜 房间 所有人 手机 手段 手艺 执行力 找人 技巧 技术 技能 投资 投资者 投钱 报价 拍板 指标 挑战 捷径 排队 推销员 收件箱 收益 收益率 政府 故事 效应
效率 教育 数字 数据 数量 整理 文字 断言 斯密 方向 方案 方法论 无法 日子 日程 时代 时刻 时机 明白 普通人 智力 智商 智慧 曲线 替代 替代性 月入 有形 朋友 服务 期限 本能
本质 朴素 机会 权力 杠杆 条数 来源 极端 林肯 标准 标签 标题 校准 样子 核心思想 根源 格局 框架 梦想 检查 检验 概念 概率 模型 模式 模板 次数 比价 比例 比喻 毛泽东 气氛
水准 水平 沃特斯 法则 法律 泡沫 波动 注意力 活法 流动 流程 流量 测试 浪费 消失 消耗 消费 涨薪 深入研究 深度 溢价 满足感 漏洞 潜意识 激情 灵魂 灾难 热情 熟练度 燃料 爸爸
爽文 版本 状态 独立思考 环境 现实 现金 现金流 理念 理性 理由 理论 瓶颈 生产 生命 生效 生活 生活水准 用力 电话 画面 疗法 痛苦 百万富翁 直指 直觉 省事 省力 省钱 矛盾 研究
社会 稀缺性 程序 程度 稻盛和夫 穷爸爸 突破 立场 竞争 答案 策略 算数 算法 管理 管用 精力 精神 系统 系统化 系统阐述 纪伯伦 纳瓦尔 终局 经典 经历 经济 经济学 经营 经验 结构
结论 群体 老板 老话 职位 聪明人 肌肉 股权 背景 胜率 脑子 脸色 脸面 自动 自律 自查 至亲 艾伦 节奏 芒格 花钱 范围 落地 虚构 蛋糕 融合 融资 螺丝 行业 行动 行当 行情 观众
观念 规划 规则 规律 规模 规矩 视角 视频 角度 计划 训练 议价 议题 记录 记账 讲法 讲道理 许可 论证 设计 证据 证明 评价 评判 评论 话术 误导 调用 谈判 负债 财产 财务 财商
财富 财报 责任 账单 账号 账户 货币 质量 费力 资产 资本 资源 资源配置 资金 赛道 走极端 足球 距离 路径 路线 身份 身体 躺平 转嫁给 运作 运气 运营 运行机制 运转 选择权 选题
通俗 通路 逻辑 道德 道理 酿酒师 重构 重演 重要性 金融危机 金钱 金银 金额 钩子 银子 销售 错误 镜子 长大 门槛 阶段 阶段性 降级 面包师 面子 韧性 顺序 预支 预测 领域 频率
风口 风险 驱动力 骂人 高度 魔术师 麻烦 黑天鹅
"""
KEYWORDS = frozenset(KEYWORD_TEXT.split())
KEY_MAX = max(len(word) for word in KEYWORDS)

PILLAR_MAPPING_FILE = Path(__file__).resolve().parent / 'assets' / 'growth_pillar_mapping.json'


def pillar_mapping():
    """读分类 → 支柱映射。唯一真源是 assets/growth_pillar_mapping.json；
    文件缺失或坏掉时返回空映射（全部进「未归类」），不再带内置默认值——
    默认值曾把六类全并进「认知」，是通病 P2 的典型（2026-11-25 移除）。只读，不缓存写盘。"""
    try:
        raw = json.loads(PILLAR_MAPPING_FILE.read_text(encoding='utf-8'))
    except (OSError, ValueError, TypeError):
        return {}
    source = raw.get('category_to_pillar') if isinstance(raw, dict) else None
    if not isinstance(source, dict):
        return {}
    return {str(k).strip(): str(v).strip() for k, v in source.items()
            if isinstance(k, str) and isinstance(v, str) and v.strip()}


def _goals(store):
    goals = [g for g in store.list('growth_goal', 200) if not g.get('archived')]
    root = next((g for g in goals if g.get('level') == 'root'), None)
    pillars = sorted((g for g in goals if g.get('level') == 'pillar'),
                     key=lambda x: (x.get('sort_order', 0), x.get('created_at') or '', x['id']))
    return root, pillars


def _rows(store):
    """知识池碎片行；content_index 还没建好时返回空列表，不让整页报错。"""
    try:
        with store.connect() as con:
            rows = con.execute(
                "SELECT source_kind,source_id,title,category,category_legacy,recorded_at,goal_ids,snippet,body "
                "FROM content_index WHERE pool='pool' "
                "AND (scope IS NULL OR scope!='example') "
                "AND (lifecycle IS NULL OR lifecycle='' OR lifecycle='active') "
                "AND source_kind IN ('knowledge','growth_fragment') "
                "ORDER BY recorded_at DESC, source_id LIMIT ?", (CARDS_LIMIT,)).fetchall()
    except sqlite3.Error:
        return []
    return [dict(r) for r in rows]


def _plain(text, limit=SUMMARY_LIMIT):
    value = re.sub(r'\s+', ' ', str(text or '')).strip()
    return value[:limit] + ('…' if len(value) > limit else '')


def _goal_ids(raw):
    """手动关联的目标 id；多数卡是空数组，照实返回，不补默认归属。"""
    try:
        value = json.loads(raw or '[]')
    except (TypeError, ValueError):
        return []
    return [str(v) for v in value] if isinstance(value, list) else []


def _norm(text):
    """归一化：只留汉字与字母数字，去空格标点。"""
    return re.sub(r'[^\u4e00-\u9fffA-Za-z0-9]', '', str(text or ''))


def _tokens(text):
    """在固定词表上做最长匹配。切出来的词都能回指到原文，不做同义扩展。"""
    plain = _norm(text)
    out, i, total = [], 0, len(plain)
    while i < total:
        hit = ''
        for size in range(min(KEY_MAX, total - i), 1, -1):
            piece = plain[i:i + size]
            if piece in KEYWORDS:
                hit = piece
                break
        if hit:
            out.append(hit)
            i += len(hit)
        else:
            i += 1
    return out


def _mention(src_body, dst_title, dst_title_words):
    """A 正文里是否出现 B 的标题或 B 标题里的关键词（≥3 字）。返回命中的那个词。"""
    title = _norm(dst_title)
    if len(title) >= MENTION_MIN and title in src_body:
        return title
    for word in dst_title_words:
        if len(word) >= MENTION_MIN and word in src_body:
            return word
    return ''


def _edges(cards, df_ratio=None, co_cap=CO_DF_CAP):
    """只造两种能回指原文的边：提及（A 正文出现 B 标题/标题词）、共现（≥2 个相同关键词）。

    同一对卡片最多一条边；提及优先，因为它的证据更强。
    共现里剔掉 df>co_cap 的泛词（如「个人」），它几乎每张卡都有，连出来的边没有信息量。
    返回 (edges, dropped)：dropped 是因为泛词被剔掉而少掉的共现边数。
    """
    df_ratio = df_ratio or {}
    bodies = {c['id']: _norm(c.get('body')) for c in cards}
    titles = {c['id']: c['title'] for c in cards}
    title_words = {c['id']: [w for w in _tokens(c['title']) if w in KEYWORDS]
                   for c in cards}
    kwsets = {c['id']: set(c['keywords']) for c in cards}
    co_sets = {cid: {w for w in words if df_ratio.get(w, 0) <= co_cap}
               for cid, words in kwsets.items()}
    ids = [c['id'] for c in cards]

    edges, pairs, dropped = [], set(), 0
    for dst in ids:
        for src in ids:
            if src == dst:
                continue
            hit = _mention(bodies[src], titles[dst], title_words[dst])
            if not hit:
                continue
            key = (src, dst) if src < dst else (dst, src)
            if key in pairs:
                continue
            pairs.add(key)
            edges.append({'source': key[0], 'target': key[1], 'type': 'mention',
                          'terms': [hit], 'from': src, 'to': dst})
    for i, a in enumerate(ids):
        for b in ids[i + 1:]:
            if (a, b) in pairs:
                continue
            shared = co_sets[a] & co_sets[b]
            if len(shared) >= 2:
                pairs.add((a, b))
                edges.append({'source': a, 'target': b, 'type': 'co',
                              'terms': sorted(shared)})
            elif len(kwsets[a] & kwsets[b]) >= 2:
                dropped += 1
    return edges, dropped


def _largest_component(cards, edges):
    """互链网络（只算提及/共现边）里最大连通块的卡片数。归属边不算，否则永远连通。"""
    parent = {c['id']: c['id'] for c in cards}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for edge in edges:
        a, b = find(edge['source']), find(edge['target'])
        if a != b:
            parent[a] = b
    size = Counter(find(c['id']) for c in cards)
    return max(size.values()) if size else 0



def build(store):
    """组装图谱数据。只读，不改库。"""
    root, pillars = _goals(store)
    mapping = pillar_mapping()
    pillar_by_name = {p['title']: p for p in pillars}
    rows = _rows(store)

    cards, clusters = [], {}
    for row in rows:
        category = (row.get('category') or '未分类').strip() or '未分类'
        group = (row.get('category_legacy') or '').strip() or category
        pillar_name = mapping.get(category) or ''
        pillar = pillar_by_name.get(pillar_name)
        cards.append({
            'id': row['source_id'],
            'title': row.get('title') or '未命名',
            'summary': _plain(row.get('snippet')),
            'category': category,
            'group': group,
            'pillar': pillar_name,
            'pillar_id': pillar['id'] if pillar else '',
            'goals': _goal_ids(row.get('goal_ids')),
            'recorded_at': row.get('recorded_at') or '',
            'kind': row.get('source_kind') or 'knowledge',
            'body': row.get('body') or '',
        })
        cluster = clusters.setdefault(category, {'id': category, 'title': category, 'count': 0,
                                                 'pillar': pillar_name,
                                                 'pillar_id': pillar['id'] if pillar else '',
                                                 'groups': {}})
        cluster['count'] += 1
        cluster['groups'][group] = cluster['groups'].get(group, 0) + 1

    pillar_counts = {}
    out_clusters = []
    for cluster in clusters.values():
        groups = [{'id': name, 'count': n} for name, n in
                  sorted(cluster['groups'].items(), key=lambda kv: (-kv[1], kv[0]))]
        out_clusters.append({'id': cluster['id'], 'title': cluster['title'],
                             'count': cluster['count'], 'pillar': cluster['pillar'],
                             'pillar_id': cluster['pillar_id'], 'groups': groups})
        if cluster['pillar']:
            pillar_counts[cluster['pillar']] = pillar_counts.get(cluster['pillar'], 0) + cluster['count']
    out_clusters.sort(key=lambda c: (-c['count'], c['title']))

    goals = []
    if root:
        goals.append({'id': root['id'], 'title': root['title'], 'level': 'root',
                      'parent_id': root.get('parent_id'), 'color': root.get('color') or ''})
    for pillar in pillars:
        goals.append({'id': pillar['id'], 'title': pillar['title'], 'level': 'pillar',
                      'parent_id': pillar.get('parent_id'), 'color': pillar.get('color') or '',
                      'card_count': pillar_counts.get(pillar['title'], 0)})

    for card in cards:
        card['keywords'] = [word for word, _ in Counter(
            _tokens(card['title']) + _tokens(card['body'])).most_common(MAX_KEYWORDS)]
    total = max(1, len(cards))
    df_count = Counter()
    for card in cards:
        for word in set(_tokens(card['title']) + _tokens(card['body'])):
            df_count[word] += 1
    df_ratio = {word: n / total for word, n in df_count.items()}
    edges, dropped = _edges(cards, df_ratio, CO_DF_CAP)
    # 2026-11-19 t158：把「同一次回答一起被引用」的共现次数变成边权（weight=次数）。
    co_count = 0
    try:
        import content_index
        known = {c['id'] for c in cards}
        existing = {(e['source'], e['target']): e for e in edges}
        for pair, count in (content_index.cooccurrence_counts(store) or {}).items():
            a, b = pair
            if a not in known or b not in known or a == b:
                continue
            edge = existing.get((a, b)) or existing.get((b, a))
            if edge:
                edge['weight'] = int(count)
                edge['cooccur'] = int(count)
            else:
                edges.append({'source': a, 'target': b, 'type': 'cooccur',
                              'terms': [], 'weight': int(count), 'cooccur': int(count)})
            co_count += 1
    except Exception:
        co_count = 0
    degree = Counter()
    for edge in edges:
        degree[edge['source']] += 1
        degree[edge['target']] += 1
    for card in cards:
        card['degree'] = degree.get(card['id'], 0)
        card.pop('body', None)

    edge_types = Counter(edge['type'] for edge in edges)
    linked = sum(1 for card in cards if card['degree'])
    top = sorted(cards, key=lambda c: (-c['degree'], c['title']))[:10]
    cut_terms = sorted(((w, n / total) for w, n in df_count.items() if n / total > CO_DF_CAP),
                       key=lambda kv: (-kv[1], kv[0]))
    stats = {
        'edges': len(edges),
        'mention': edge_types.get('mention', 0),
        'co': edge_types.get('co', 0),
        'cooccur': co_count,
        'co_cap': CO_DF_CAP,
        'co_dropped': dropped,
        'co_cut_terms': [{'word': w, 'df': round(r, 3)} for w, r in cut_terms],
        'avg_degree': round(2 * len(edges) / len(cards), 2) if cards else 0,
        'linked': linked,
        'isolated': len(cards) - linked,
        'largest': _largest_component(cards, edges),
        'keywords': len(KEYWORDS),
        'top': [{'id': c['id'], 'title': c['title'], 'degree': c['degree'],
                 'keywords': c['keywords']} for c in top],
    }

    return {
        'ok': True,
        'source': 'content_index',
        'generated_at': datetime.now(TZ).isoformat(timespec='seconds'),
        'totals': {'cards': len(cards), 'categories': len(out_clusters),
                   'groups': sum(len(c['groups']) for c in out_clusters)},
        'mapping': mapping,
        'goals': goals,
        'clusters': out_clusters,
        'cards': cards,
        'edges': edges,
        'stats': stats,
        'note': '只读：碎片来自你的知识池（非示例、有效），不改写原文。',
    }


_CACHE = {'key': None, 'until': 0.0, 'data': None}
_CACHE_TTL = 45.0


def route(handler, method, bits, query=None, store=None):
    """挂在 app.py 的路由分发上：GET /api/graph/cards。"""
    if method != 'GET' or list(bits) != ['api', 'graph', 'cards']:
        return False
    # 暖缓存：建图要扫知识池并两两算边（HDD 冷实测 2.07s）；TTL 内且知识/目标无写入、索引未重建才命中。
    rebuilt_at = None
    try:
        import content_index
        rebuilt_at = content_index._meta_get(store, 'rebuilt_at')
    except Exception:
        pass
    key = (store.versions(('knowledge', 'growth_fragment', 'growth_goal')), rebuilt_at)
    now = time.monotonic()
    if _CACHE['data'] is not None and _CACHE['key'] == key and now < _CACHE['until']:
        handler.json(_CACHE['data'])
        return True
    data = build(store)
    _CACHE.update(key=key, until=time.monotonic() + _CACHE_TTL, data=data)
    handler.json(data)
    return True
