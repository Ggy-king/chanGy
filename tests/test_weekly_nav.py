# -*- coding: utf-8 -*-
"""周复盘导航回归：_weekly_data 同时接受 ISO 周与日期锚点（2026-11-18 修"上周按钮无反应"）。
bug 历史：服务端只认 ISO 周（len==7），前端周导航传日期被静默回退今天——点上周永远显示当前周。"""
import server as gy


def test_iso_week_still_works():
    d = gy._weekly_data('2026-W46')
    assert d['week'] == '2026-W46'
    assert d['monday'] == '2026-11-09'


def test_date_anchor_accepted():
    d = gy._weekly_data('2026-11-09')
    assert d['week'] == '2026-W46'
    assert d['monday'] == '2026-11-09'


def test_date_anchor_snaps_to_monday():
    # 周六/周日锚点吸附到所在周周一（ISO 周）
    assert gy._weekly_data('2026-11-14')['monday'] == '2026-11-09'   # 周六
    assert gy._weekly_data('2026-11-15')['monday'] == '2026-11-09'   # 周日


def test_empty_and_garbage_fallback_today():
    d = gy._weekly_data('')
    assert d['week'].startswith('2026-W')
    d2 = gy._weekly_data('garbage!!')
    assert d2['week'].startswith('2026-W')
