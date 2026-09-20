# -*- coding: utf-8 -*-
"""
缠论 顶底分型识别（标准定义）
经过K线包含处理后：
- 顶分型：三根相邻K线，中间那根high最高，且low也最高
- 底分型：三根相邻K线，中间那根low最低，且high也最低
"""
import pandas as pd


def find_fenxing(merged_df):
    top_fx = []
    bottom_fx = []
    
    for i in range(1, len(merged_df) - 1):
        prev_h = merged_df['high'].iloc[i-1]
        curr_h = merged_df['high'].iloc[i]
        next_h = merged_df['high'].iloc[i+1]
        
        prev_l = merged_df['low'].iloc[i-1]
        curr_l = merged_df['low'].iloc[i]
        next_l = merged_df['low'].iloc[i+1]
        
        # 顶分型：中间high最高 且 中间low也最高
        if (curr_h > prev_h and curr_h > next_h and
            curr_l > prev_l and curr_l > next_l):
            top_fx.append({
                'idx': i,
                'high': curr_h,
                'low': curr_l,
                'datetime': merged_df['datetime'].iloc[i],
                'start_idx': merged_df['start_idx'].iloc[i],
                'end_idx': merged_df['end_idx'].iloc[i],
            })
        
        # 底分型：中间low最低 且 中间high也最低
        if (curr_l < prev_l and curr_l < next_l and
            curr_h < prev_h and curr_h < next_h):
            bottom_fx.append({
                'idx': i,
                'high': curr_h,
                'low': curr_l,
                'datetime': merged_df['datetime'].iloc[i],
                'start_idx': merged_df['start_idx'].iloc[i],
                'end_idx': merged_df['end_idx'].iloc[i],
            })
    
    return top_fx, bottom_fx
