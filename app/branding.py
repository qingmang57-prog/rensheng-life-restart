"""白牌化配置：用户自己的名字就是产品名，这里是唯一配置源。

侧栏 logo、浏览器标题、封面默认账号、导出文件名等所有用户可见的品牌信息
都必须从这里取值；其他模块不得再写死产品名或默认名字。
"""
import re

DEFAULT_NAME = '人生重启系统'
MAX_NAME_LEN = 30
KIND = 'settings'
KEY = 'brand'
_INVALID_FILENAME = re.compile(r'[\\/:*?"<>|\x00-\x1f\x7f]+')


def _stored_name(store):
    record = store.get(KIND, KEY) or {}
    value = record.get('name')
    return value if isinstance(value, str) else ''


def name(store):
    """当前显示名；未填写或已清空时回落到中性占位。"""
    value = _stored_name(store).strip()
    return value or DEFAULT_NAME


def configured_name(store):
    """用户真正填过的名字；没填就是空，不拿占位名冒充作者。"""
    return _stored_name(store).strip()


def set_name(store, value):
    """保存名字。空字符串是合法输入，表示回到占位名；不会残留旧名字。"""
    if not isinstance(value, str):
        raise ValueError('名字需要是文字。')
    text = value.strip()
    if len(text) > MAX_NAME_LEN:
        raise ValueError('名字最多 %d 个字。' % MAX_NAME_LEN)
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in text):
        raise ValueError('名字里不能有换行或控制字符。')
    store.put(KIND, {'name': text}, key=KEY)
    return name(store)


def account(store):
    """封面默认账号名：@ + 当前名字。"""
    value = name(store).lstrip('@').strip() or DEFAULT_NAME
    return '@' + value


def safe_filename(value, limit=40):
    """清掉 Windows 文件名非法字符；空值回落占位名。"""
    text = _INVALID_FILENAME.sub('-', str(value or '')).strip(' .-')
    return text[:limit] or DEFAULT_NAME


def file_stem(store, title='', label=''):
    """导出文件名主干：名字-标题-用途。"""
    parts = [safe_filename(name(store), 24)]
    if title:
        parts.append(safe_filename(title, 30))
    if label:
        parts.append(safe_filename(label, 20))
    return '-'.join(parts)
