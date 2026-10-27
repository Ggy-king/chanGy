# -*- coding: utf-8 -*-
"""系统诊断页 E2E（v2 改版）：英雄位/各卡强调色/Top比例条/GitHub提交区/连接诊断保留"""
import os, json
os.environ.setdefault('PLAYWRIGHT_BROWSERS_PATH', os.path.expanduser('~/Library/Caches/ms-playwright'))
from playwright.sync_api import sync_playwright

PAGE = 'file://' + os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'web', 'pc_tqstats.html')

SYS = {
    'code': {'py_files': 27, 'py_lines': 8077, 'py_sloc': 6669, 'web_files': 18, 'web_lines': 8857,
             'functions': 412, 'classes': 9, 'todos': 0, 'deps': 9},
    'api': {'get': 47, 'post': 19, 'ws': 1, 'endpoints': [
        {'m': 'GET', 'p': '/pc'}, {'m': 'GET', 'p': '/pc/matrix'}, {'m': 'GET', 'p': '/pc/tqstats'},
        {'m': 'GET', 'p': '/api/overview'}, {'m': 'GET', 'p': '/api/sysdiag'},
        {'m': 'POST', 'p': '/api/positions/add'}, {'m': 'WS', 'p': '/ws'}]},
    'top_files': [{'file': 'server.py', 'lines': 3436}, {'file': 'engine.py', 'lines': 718}],
    'data': {'data_mb': 0.5, 'log_mb': 0.01},
    'runtime': {'uptime_h': 1.2, 'threads': 9, 'rss_mb': 180.5, 'python': '3.12.14', 'platform': 'Darwin'},
    'git': {'total': 65, 'today': 1, 'week': 12, 'ahead': 2, 'behind': 0,
            'last_time': '2026-10-27 20:59', 'last_msg': 'feat: 增加天勤连接诊断页面及相关功能', 'remote': 'github.com/Ggy-king/chanGy'},
}
TQ = {'stats': {'requests': 158, 'stale_serves': 0, 'err_stale': 0, 'recreates': 1},
      'subscribed': 80, 'seconds_since_recreate': 43,
      'lv_trend': {'jd2512|60': {'v': 'down', 'age_min': 22.3}}}

with sync_playwright() as p:
    b = p.chromium.launch(headless=True)
    pg = b.new_page()
    errs = []
    pg.on('pageerror', lambda e: errs.append(f'pageerror: {e}'))
    pg.add_init_script("window.fetch=async(u)=>{const s=String(u).startsWith('/api/sysdiag')?%s:%s;return {ok:true,json:async()=>s};};"
                       % (json.dumps(SYS), json.dumps(TQ)))
    pg.goto(PAGE)
    pg.wait_for_timeout(600)

    assert pg.title() == '系统诊断'
    # 英雄位
    assert pg.locator('#h_sloc').inner_text() == '6,669', '英雄位 SLOC 未渲染'
    assert pg.locator('#h_commits').inner_text() == '65', '英雄位提交总数未渲染'
    assert '最近 10-27' in pg.locator('#h_last').inner_text(), '最近提交时间未渲染'
    # 项目规模卡
    assert pg.locator('#s_pyf').inner_text() == '27'
    assert pg.locator('#s_webl').inner_text() == '8,857'
    # Top 比例条
    bars = pg.locator('#topbody .barfill')
    assert bars.count() == 2, 'Top 比例条缺失'
    w1 = bars.nth(0).evaluate('e => e.style.width')
    assert w1 == '100%', f'最大文件条应 100%: {w1}'
    # 运行环境
    assert '180.5' in pg.locator('#s_rss').inner_text()
    # 接口与代码结构
    assert pg.locator('#a_get').inner_text() == '47', 'GET 数未渲染'
    assert pg.locator('#a_post').inner_text() == '19', 'POST 数未渲染'
    assert pg.locator('#a_ws').inner_text() == '1', 'WS 数未渲染'
    assert pg.locator('#a_fn').inner_text() == '412', '函数数未渲染'
    assert pg.locator('#a_todo').inner_text() == '0', 'TODO 数未渲染'
    rows = pg.locator('#apibody tr')
    assert rows.count() == 7, f'路由清单行数不对: {rows.count()}'
    assert pg.locator('#apibody .mchip.WS').count() == 1, 'WS 徽章缺失'
    # 页面路由排在接口前
    first_path = pg.locator('#apibody tr td:nth-child(2)').first.inner_text()
    assert first_path == '/pc', f'页面路由应排最前: {first_path}'
    print('ok 1b/3 接口与代码结构区（GET/POST/WS/函数/类/TODO/依赖 + 路由清单）全部渲染')
    # GitHub 提交区
    assert pg.locator('#g_today').inner_text() == '1'
    assert pg.locator('#g_week').inner_text() == '12'
    assert pg.locator('#g_ahead').inner_text() == '2'
    assert '未推送' in pg.locator('#g_sync').inner_text(), '未推送徽章未出现'
    assert 'feat: 增加天勤连接诊断' in pg.locator('#g_lastmsg').inner_text(), '最后提交信息未渲染'
    print('ok 1/3 英雄位/规模卡/Top比例条/运行环境/GitHub提交区全部渲染')

    # 连接诊断保留
    assert pg.locator('#n_req').inner_text() == '158'
    assert '看跌' in pg.locator('#tbody').inner_text()
    print('ok 2/3 天勤连接诊断区块完整保留')

    # 强调色抽查：英雄卡渐变 clip 生效
    clip = pg.locator('#h_sloc').evaluate('e => getComputedStyle(e).webkitBackgroundClip || getComputedStyle(e).backgroundClip')
    assert 'text' in clip, f'渐变文字未生效: {clip}'
    print('ok 3/3 渐变英雄数字样式生效')

    if errs:
        print('页面 JS 错误:', errs)
        raise SystemExit(1)
    b.close()
print('ok - 系统诊断页 v2 E2E 全部通过')
