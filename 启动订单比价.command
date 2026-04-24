#!/bin/zsh

PROJECT_DIR="/Users/zhangyang/Documents/order-detail-proxy"
SERVER_URL="http://127.0.0.1:8787/"

cd "$PROJECT_DIR" || exit 1

# 服务已启动则直接打开页面
if lsof -iTCP:8787 -sTCP:LISTEN >/dev/null 2>&1; then
  open "$SERVER_URL"
  exit 0
fi

# 优先用 python3 启动服务
if command -v python3 >/dev/null 2>&1; then
  nohup python3 server.py >/tmp/order-detail-proxy.log 2>&1 &
else
  osascript -e 'display alert "启动失败" message "未找到 python3，请先安装 Python 3。"' >/dev/null 2>&1
  exit 1
fi

# 等待服务启动（最多约6秒）
for _ in {1..30}; do
  if lsof -iTCP:8787 -sTCP:LISTEN >/dev/null 2>&1; then
    open "$SERVER_URL"
    exit 0
  fi
  sleep 0.2
done

osascript -e 'display alert "服务启动超时" message "请检查 /tmp/order-detail-proxy.log 日志。"' >/dev/null 2>&1
exit 1
