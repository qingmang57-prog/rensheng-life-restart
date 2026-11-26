# -*- coding: utf-8 -*-
"""口播稿公开分享·客户端（2026-11-25 一期）：脱敏闸 + 云端转发。

红线：稿子里带密钥/令牌/登录 Cookie/本机绝对路径/手机号一律拒发，
不先发了再删、也不静默改写用户的稿子；拒发时点名类别并给退路。
云端连不上/未登录由 cloud_gateway 翻成中文，这里不伪造成功。
"""
import re

import cloud_gateway

BLOCK_RULES = (
    ('密钥', re.compile(r'sk-[A-Za-z0-9]{16,}')),
    ('Bearer 令牌', re.compile(r'Bearer\s+[A-Za-z0-9._\-]{16,}', re.I)),
    ('登录 Cookie', re.compile(r'ip_auth\s*=')),
    ('本机绝对路径', re.compile(r'([A-Za-z]:[\\/]|/Users/|/home/|/opt/)')),
    ('手机号', re.compile(r'(?<!\d)1[3-9]\d{9}(?!\d)')),
)


def scan(text):
    hits = []
    for label, rx in BLOCK_RULES:
        m = rx.search(str(text or ''))
        if m:
            sample = m.group(0)
            masked = (sample[:4] + '…' + sample[-2:]) if len(sample) > 8 else '…'
            hits.append('%s（%s）' % (label, masked))
    return hits


def publish(payload):
    hits = scan(payload.get('body')) + scan(payload.get('title'))
    if hits:
        raise cloud_gateway.ProxyError(
            '稿子里有不该公开的内容：%s。删掉后再分享；这份稿子没有发出去，仍可复制或存图。'
            % '、'.join(hits), 'SHARE_BLOCKED', 400)
    return cloud_gateway._call('/api/share/publish', payload, timeout=20)


def revoke(token):
    return cloud_gateway._call('/api/share/revoke', {'token': token}, timeout=15)


def mine():
    return cloud_gateway._call('/api/share/mine', None, timeout=15, method='GET')