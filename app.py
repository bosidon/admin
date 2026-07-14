#!/usr/bin/env python3
"""
自媒体管理后台 - 主应用
端口: 3010
功能: 仪表盘 / 公众号文章 / 小红书文案 / 视频号 / 设置
"""
import os
import json
import glob
import subprocess
import re
import random
import hashlib
import time
from datetime import datetime
from pathlib import Path
from flask import Flask, render_template, jsonify, request, send_from_directory, redirect, url_for

app = Flask(__name__)
app.secret_key = os.urandom(24)

# ============================================================
# 路径配置
# ============================================================
BASE_DIR = Path(__file__).parent
DATA_DIR = BASE_DIR / "data"
ARTICLES_DIR = DATA_DIR / "articles"
XHS_DIR = DATA_DIR / "xhs"
VIDEO_DIR = DATA_DIR / "video"
CONFIG_DIR = DATA_DIR / "config"

for d in [DATA_DIR, ARTICLES_DIR, XHS_DIR, VIDEO_DIR, CONFIG_DIR]:
    d.mkdir(parents=True, exist_ok=True)

# 云服务器配置
CLOUD_HOST = "ubuntu@124.222.139.97"
CLOUD_ARTICLES_DIR = "/var/www/wechat-agent/articles"

# ============================================================
# 数据管理
# ============================================================
def load_json(path, default=None):
    """读取JSON文件"""
    if os.path.exists(path):
        with open(path, 'r', encoding='utf-8') as f:
            return json.load(f)
    return default if default is not None else {}

def save_json(path, data):
    """保存JSON文件"""
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

def get_all_articles():
    """获取所有公众号文章"""
    articles = []
    for f in sorted(glob.glob(str(ARTICLES_DIR / "*.json")), reverse=True):
        try:
            data = load_json(f)
            if data:
                articles.append(data)
        except:
            pass
    return articles

def get_all_xhs():
    """获取所有小红书文案"""
    items = []
    for f in sorted(glob.glob(str(XHS_DIR / "*.json")), reverse=True):
        try:
            data = load_json(f)
            if data:
                items.append(data)
        except:
            pass
    return items

def get_all_videos():
    """获取所有视频脚本"""
    items = []
    for f in sorted(glob.glob(str(VIDEO_DIR / "*.json")), reverse=True):
        try:
            data = load_json(f)
            if data:
                items.append(data)
        except:
            pass
    return items

# ============================================================
# 内容轮盘（公众号文章生成）
# ============================================================
CONTENT_WHEEL_PATH = CONFIG_DIR / "content-wheel.json"
DIVERSITY_CONFIG_PATH = CONFIG_DIR / "diversity-config.json"

def init_content_wheel():
    """初始化内容轮盘配置"""
    if not CONTENT_WHEEL_PATH.exists():
        wheel = {
            "version": 1,
            "books": {
                "pool": [
                    {"id": 88, "name": "太傻天书", "weight": 3},
                    {"id": 0, "name": "零极限", "weight": 3},
                    {"id": 0, "name": "与神对话", "weight": 3},
                    {"id": 0, "name": "欧林-喜悦之道", "weight": 2},
                    {"id": 0, "name": "欧林-创造金钱", "weight": 2},
                    {"id": 0, "name": "欧林-灵魂之爱", "weight": 2},
                    {"id": 0, "name": "赛斯-灵魂永生", "weight": 1},
                    {"id": 0, "name": "赛斯-个人实相的本质", "weight": 1}
                ],
                "recent_used": [],
                "max_history": 3
            },
            "themes": {
                "categories": [
                    {"id": "self", "name": "自我认知", "examples": ["你是谁", "自我价值", "内在声音", "真实自我", "自我接纳"]},
                    {"id": "emotion", "name": "情绪管理", "examples": ["焦虑", "恐惧", "愤怒", "悲伤", "喜悦", "平静"]},
                    {"id": "relationship", "name": "关系与爱", "examples": ["亲密关系", "亲子关系", "友谊", "信任", "沟通", "边界"]},
                    {"id": "purpose", "name": "人生目的", "examples": ["使命", "热情", "选择", "方向", "意义", "活出自己"]},
                    {"id": "abundance", "name": "丰盛与财富", "examples": ["金钱观", "丰盛意识", "价值感", "给予与接收", "富足心态"]},
                    {"id": "spiritual", "name": "灵性成长", "examples": ["冥想", "觉察", "当下", "觉醒", "能量", "直觉"]},
                    {"id": "healing", "name": "疗愈与释放", "examples": ["放下过去", "原谅", "内在小孩", "情绪释放", "自我疗愈"]},
                    {"id": "creation", "name": "创造与显化", "examples": ["吸引力法则", "信念创造实相", "愿景板", "行动力", "显化"]}
                ],
                "max_history": 2
            },
            "angles": {
                "pool": [
                    {"id": "personal", "name": "个人故事"},
                    {"id": "reader", "name": "读者问答"},
                    {"id": "crossbook", "name": "跨书串联"},
                    {"id": "contrary", "name": "反常识"},
                    {"id": "practical", "name": "实操指南"},
                    {"id": "seasonal", "name": "时令相关"},
                    {"id": "comparison", "name": "对比分析"},
                    {"id": "deep", "name": "深度思辨"}
                ],
                "recent_used": [],
                "max_history": 3
            }
        }
        save_json(CONTENT_WHEEL_PATH, wheel)
    return load_json(CONTENT_WHEEL_PATH)

def select_book(wheel):
    """选择书目"""
    pool = wheel["books"]["pool"]
    recent = wheel["books"].get("recent_used", [])
    available = [b for b in pool if b["name"] not in recent]
    if not available:
        wheel["books"]["recent_used"] = []
        available = pool
    weights = [b["weight"] for b in available]
    chosen = random.choices(available, weights=weights, k=1)[0]
    recent.append(chosen["name"])
    if len(recent) > wheel["books"].get("max_history", 3):
        recent = recent[-wheel["books"]["max_history"]:]
    wheel["books"]["recent_used"] = recent
    return chosen, wheel

def select_theme(wheel):
    """选择话题"""
    categories = wheel["themes"]["categories"]
    available_cats = [c for c in categories if c["name"] not in 
                     [cat["name"] for cat in categories if cat.get("recent_used")]]
    if not available_cats:
        available_cats = categories
    cat = random.choice(available_cats)
    topic = random.choice(cat["examples"])
    return cat, topic, wheel

def select_angle(wheel):
    """选择角度"""
    pool = wheel["angles"]["pool"]
    recent = wheel["angles"].get("recent_used", [])
    available = [a for a in pool if a["name"] not in recent]
    if not available:
        wheel["angles"]["recent_used"] = []
        available = pool
    chosen = random.choice(available)
    recent.append(chosen["name"])
    if len(recent) > wheel["angles"].get("max_history", 3):
        recent = recent[-wheel["angles"]["max_history"]:]
    wheel["angles"]["recent_used"] = recent
    return chosen, wheel

# ============================================================
# 路由 - 仪表盘
# ============================================================
@app.route('/')
def dashboard():
    articles = get_all_articles()
    xhs_list = get_all_xhs()
    videos = get_all_videos()
    
    stats = {
        "articles_total": len(articles),
        "articles_pending": len([a for a in articles if a.get("status") == "pending"]),
        "articles_approved": len([a for a in articles if a.get("status") == "approved"]),
        "articles_published": len([a for a in articles if a.get("status") == "published"]),
        "xhs_total": len(xhs_list),
        "xhs_pending": len([x for x in xhs_list if x.get("status") == "pending"]),
        "video_total": len(videos),
        "video_pending": len([v for v in videos if v.get("status") == "pending"]),
    }
    
    recent = []
    for a in articles[:5]:
        recent.append({"type": "article", "title": a.get("title", ""), "time": a.get("created_at", ""), "status": a.get("status", "pending")})
    for x in xhs_list[:3]:
        recent.append({"type": "xhs", "title": x.get("title", ""), "time": x.get("created_at", ""), "status": x.get("status", "pending")})
    recent.sort(key=lambda x: x.get("time", ""), reverse=True)
    
    return render_template("dashboard.html", stats=stats, recent=recent[:10])

# ============================================================
# 路由 - 公众号文章
# ============================================================
@app.route('/articles')
def articles_list():
    articles = get_all_articles()
    return render_template("articles.html", articles=articles)

@app.route('/articles/<article_id>')
def article_detail(article_id):
    articles = get_all_articles()
    article = None
    for a in articles:
        if a.get("id") == article_id:
            article = a
            break
    if not article:
        return "文章未找到", 404
    
    # 读取Markdown内容
    md_file = article.get("md_file", "")
    md_content = ""
    if md_file:
        md_path = ARTICLES_DIR / md_file
        if md_path.exists():
            md_content = md_path.read_text(encoding="utf-8")
    
    return render_template("article_detail.html", article=article, content=md_content)

@app.route('/api/articles')
def api_articles():
    return jsonify(get_all_articles())

@app.route('/api/articles/<article_id>/status', methods=['POST'])
def api_article_status(article_id):
    new_status = request.json.get("status")
    if new_status not in ["pending", "approved", "published", "rejected"]:
        return jsonify({"error": "Invalid status"}), 400
    
    articles = get_all_articles()
    for a in articles:
        if a.get("id") == article_id:
            a["status"] = new_status
            json_path = ARTICLES_DIR / f"{article_id}.json"
            save_json(json_path, a)
            return jsonify({"ok": True, "status": new_status})
    return jsonify({"error": "Not found"}), 404

@app.route('/api/articles/generate', methods=['POST'])
def api_generate_article():
    """触发文章生成（调用服务器脚本）"""
    try:
        result = subprocess.run(
            ["ssh", CLOUD_HOST, "cd /var/www/wechat-agent && python3 generate.py"],
            capture_output=True, text=True, timeout=120
        )
        if result.returncode == 0:
            return jsonify({"ok": True, "message": "文章生成成功", "output": result.stdout[-500:]})
        else:
            return jsonify({"ok": False, "error": result.stderr[-500:]}), 500
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

@app.route('/api/sync', methods=['POST'])
def api_sync():
    """从云服务器同步文章"""
    try:
        result = subprocess.run(
            ["scp", "-r", f"{CLOUD_HOST}:{CLOUD_ARTICLES_DIR}/*", str(ARTICLES_DIR)],
            capture_output=True, text=True, timeout=30
        )
        if result.returncode == 0:
            return jsonify({"ok": True, "message": "同步成功"})
        else:
            return jsonify({"ok": False, "error": result.stderr}), 500
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

@app.route('/articles/<path:filename>')
def serve_article_file(filename):
    return send_from_directory(ARTICLES_DIR, filename)

# ============================================================
# 路由 - 小红书文案
# ============================================================
@app.route('/xhs')
def xhs_list():
    items = get_all_xhs()
    return render_template("xhs.html", items=items)

@app.route('/xhs/<item_id>')
def xhs_detail(item_id):
    items = get_all_xhs()
    item = None
    for x in items:
        if x.get("id") == item_id:
            item = x
            break
    if not item:
        return "文案未找到", 404
    return render_template("xhs_detail.html", item=item)

@app.route('/api/xhs')
def api_xhs():
    return jsonify(get_all_xhs())

@app.route('/api/xhs/<item_id>/status', methods=['POST'])
def api_xhs_status(item_id):
    new_status = request.json.get("status")
    items = get_all_xhs()
    for x in items:
        if x.get("id") == item_id:
            x["status"] = new_status
            json_path = XHS_DIR / f"{item_id}.json"
            save_json(json_path, x)
            return jsonify({"ok": True})
    return jsonify({"error": "Not found"}), 404

# ============================================================
# 路由 - 视频号
# ============================================================
@app.route('/video')
def video_list():
    items = get_all_videos()
    return render_template("video.html", items=items)

@app.route('/video/<item_id>')
def video_detail(item_id):
    items = get_all_videos()
    item = None
    for v in items:
        if v.get("id") == item_id:
            item = v
            break
    if not item:
        return "脚本未找到", 404
    return render_template("video_detail.html", item=item)

@app.route('/api/video')
def api_video():
    return jsonify(get_all_videos())

@app.route('/api/video/<item_id>/status', methods=['POST'])
def api_video_status(item_id):
    new_status = request.json.get("status")
    items = get_all_videos()
    for v in items:
        if v.get("id") == item_id:
            v["status"] = new_status
            json_path = VIDEO_DIR / f"{item_id}.json"
            save_json(json_path, v)
            return jsonify({"ok": True})
    return jsonify({"error": "Not found"}), 404

# ============================================================
# 路由 - 设置
# ============================================================
@app.route('/settings')
def settings():
    config = load_json(CONFIG_DIR / "settings.json", {
        "cloud_host": CLOUD_HOST,
        "feishu_app_id": "",
        "deepseek_api_key": "",
        "bailian_api_key": "",
        "auto_generate": False,
        "generate_schedule": "每周二/五 09:00"
    })
    return render_template("settings.html", config=config)

@app.route('/api/settings', methods=['POST'])
def api_save_settings():
    config = request.json
    save_json(CONFIG_DIR / "settings.json", config)
    return jsonify({"ok": True})

# ============================================================
# 初始化
# ============================================================
init_content_wheel()

if __name__ == '__main__':
    print("🚀 自媒体管理后台启动")
    print("   地址: http://localhost:3010")
    print("   按 Ctrl+C 停止")
    app.run(host='0.0.0.0', port=3010, debug=True)
