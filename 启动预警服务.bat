@echo off
chcp 65001 >nul
title 缠论预警服务
cd /d E:\agent\chanGy
echo 正在启动缠论预警服务（首次会拉取行情，请稍候）...
py server.py
pause
