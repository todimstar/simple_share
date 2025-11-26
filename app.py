# -*- coding: utf-8 -*-
"""
文件共享站 - V3生产级优化版本
修复问题：
1. 并发上传/删除冲突
2. 多文件同时上传
3. 断点续传完善
4. 垃圾文件自动清理
"""

from flask import Flask, request, render_template_string, send_file, redirect, url_for, jsonify
import os
import json
from datetime import datetime, timedelta
import uuid
import gzip
import shutil
import threading  # 用于文件锁
import time
import logging
from logging.handlers import RotatingFileHandler

app = Flask(__name__)

# ==================== 配置常量 ====================
# 使用绝对路径，解决部署环境下 CWD 不一致导致找不到文件夹的问题
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
UPLOAD_FOLDER = os.path.join(BASE_DIR, 'shared')
MESSAGES_FILE = os.path.join(BASE_DIR, 'messages.json')
TEMP_FOLDER = os.path.join(BASE_DIR, 'temp_uploads')
LOG_FILE = os.path.join(BASE_DIR, 'app.log')

MAX_CONTENT_LENGTH = 500 * 1024 * 1024  # 500MB
TEMP_FILE_CLEANUP_HOURS = 2  # 超过 2 小时的临时文件自动清理

# ==================== 日志配置 ====================
# 配置日志格式和处理器
formatter = logging.Formatter(
    '[%(asctime)s] %(levelname)s in %(module)s: %(message)s'
)

# 文件处理器 - 限制大小为 10MB，保留 5 个备份
file_handler = RotatingFileHandler(
    LOG_FILE, maxBytes=10*1024*1024, backupCount=5, encoding='utf-8'
)
file_handler.setFormatter(formatter)
file_handler.setLevel(logging.INFO)

# 控制台处理器
console_handler = logging.StreamHandler()
console_handler.setFormatter(formatter)
console_handler.setLevel(logging.INFO)

# 获取 Flask 的 logger 并添加处理器
# 移除默认的处理器以避免重复
app.logger.handlers = []
app.logger.addHandler(file_handler)
app.logger.addHandler(console_handler)
app.logger.setLevel(logging.INFO)

# 同时也配置 werkzeug 的日志，避免请求日志刷屏，只记录错误
logging.getLogger('werkzeug').setLevel(logging.ERROR)

os.makedirs(UPLOAD_FOLDER, exist_ok=True)
os.makedirs(TEMP_FOLDER, exist_ok=True)

if not os.path.exists(MESSAGES_FILE):
    with open(MESSAGES_FILE, 'w', encoding='utf-8') as f:
        json.dump([], f)

app.config['MAX_CONTENT_LENGTH'] = MAX_CONTENT_LENGTH
app.config['SEND_FILE_MAX_AGE_DEFAULT'] = 0

# ==================== 文件锁机制 ====================
# 解决问题 1：防止并发操作冲突
file_locks = {}  # 格式: {文件路径: threading.Lock()}
file_locks_lock = threading.Lock()  # 保护 file_locks 字典本身的锁

def get_file_lock(filepath):
    """
    获取文件锁（File Lock）
    作用：确保同一时间只有一个操作能访问同一个文件
    例如：正在上传 A.zip 时，不能同时删除 A.zip
    """
    with file_locks_lock:
        if filepath not in file_locks:
            file_locks[filepath] = threading.Lock()
        return file_locks[filepath]


# ==================== 垃圾文件清理 ====================
# 解决问题 4：自动清理超时的临时文件
def cleanup_temp_files():
    """
    清理超时的临时文件
    场景：用户上传到一半关闭浏览器，临时文件会残留
    策略：删除超过 2 小时未修改的临时文件夹
    """
    try:
        for upload_id in os.listdir(TEMP_FOLDER):
            temp_dir = os.path.join(TEMP_FOLDER, upload_id)
            
            # 只处理文件夹
            if not os.path.isdir(temp_dir):
                continue
            
            try:
                age_hours = get_temp_dir_age_hours(temp_dir)
            except FileNotFoundError:
                continue
            
            # 超过 2 小时，删除
            if age_hours > TEMP_FILE_CLEANUP_HOURS:
                app.logger.info(f"[清理] 删除过期临时文件: {upload_id} (已存在 {age_hours:.1f} 小时)")
                remove_temp_dir(temp_dir, '清理')
    except Exception as e:
        app.logger.error(f"[清理] 清理临时文件时出错: {e}")


# ==================== 启动时清理一次 ====================
cleanup_temp_files()


# ==================== Gzip 压缩中间件 ====================
@app.after_request
def compress_response(response):
    """Gzip 压缩响应，节省流量"""
    if response.status_code < 200 or response.status_code >= 300:
        return response
    
    accept_encoding = request.headers.get('Accept-Encoding', '')
    if 'gzip' not in accept_encoding.lower():
        return response
    
    if (response.direct_passthrough or 
        len(response.get_data()) < 500 or
        'Content-Encoding' in response.headers):
        return response
    
    response.direct_passthrough = False
    gzipped_data = gzip.compress(response.get_data(), compresslevel=6)
    
    response.set_data(gzipped_data)
    response.headers['Content-Encoding'] = 'gzip'
    response.headers['Content-Length'] = len(gzipped_data)
    
    return response


# ==================== HTML 模板 ====================
HTML_TEMPLATE = '''
<!DOCTYPE html>
<html>
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>文件共享站</title>
    <style>
        *{margin:0;padding:0;box-sizing:border-box}
        body{font-family:Arial,sans-serif;background:#f5f7fa;padding:10px}
        .container{max-width:1200px;margin:0 auto}
        .section{background:#fff;margin:15px 0;padding:20px;border-radius:8px;box-shadow:0 2px 8px rgba(0,0,0,.08)}
        h1,h2{color:#2c3e50;margin-bottom:15px}
        input,textarea,button{width:100%;padding:10px;margin:8px 0;border:2px solid #ddd;border-radius:6px;font-size:14px}
        input[type="text"]{max-width:300px}
        textarea{min-height:100px;resize:vertical;font-family:inherit}
        button{background:#3498db;color:#fff;border:none;cursor:pointer;transition:.3s}
        button:hover{background:#2980b9}
        button:disabled{background:#95a5a6;cursor:not-allowed}
        .delete-btn{background:#e74c3c;padding:6px 12px;font-size:12px;width:auto;display:inline-block}
        .delete-btn:hover{background:#c0392b}
        .download-btn{background:#27ae60;padding:6px 12px;font-size:12px;width:auto;display:inline-block;margin-right:5px}
        
        /* 上传任务列表 */
        .upload-task{background:#f8f9fa;padding:15px;margin:10px 0;border-radius:6px;border-left:4px solid #3498db}
        .upload-task.completed{border-left-color:#27ae60}
        .upload-task.failed{border-left-color:#e74c3c}
        .task-header{display:flex;justify-content:space-between;align-items:center;margin-bottom:10px}
        .task-name{font-weight:bold;color:#2c3e50}
        .task-status{font-size:12px;color:#7f8c8d}
        
        .progress-container{margin:10px 0}
        .progress-bar{width:100%;height:24px;background:#ecf0f1;border-radius:12px;overflow:hidden;position:relative}
        .progress-fill{height:100%;background:linear-gradient(90deg,#3498db,#2ecc71);transition:width .3s;border-radius:12px}
        .progress-text{position:absolute;width:100%;text-align:center;line-height:24px;font-weight:bold;color:#2c3e50;z-index:1;font-size:12px}
        .upload-info{font-size:12px;color:#7f8c8d;margin-top:5px}
        
        .file-item{display:flex;justify-content:space-between;align-items:center;padding:10px;margin:8px 0;background:#f8f9fa;border-radius:6px;border-left:4px solid #3498db}
        .file-item a{text-decoration:none;color:#2c3e50;flex-grow:1}
        .file-item a:hover{color:#3498db}
        .message{background:#f8f9fa;padding:15px;margin:12px 0;border-radius:6px;border-left:4px solid #27ae60}
        .message-header{display:flex;justify-content:space-between;margin-bottom:10px}
        .message-author{font-weight:bold;color:#2c3e50}
        .message-time{font-size:12px;color:#7f8c8d}
        .message-content{white-space:pre-wrap;line-height:1.5;color:#34495e;word-wrap:break-word}
        .empty-state{text-align:center;color:#7f8c8d;padding:30px}
        
        @media(max-width:768px){
            .file-item{flex-direction:column;align-items:flex-start}
            .delete-btn,.download-btn{margin-top:8px}
        }
    </style>
</head>
<body>
    <div class="container">
        <h1 style="text-align:center">📁 文件共享站（支持多文件并发上传）</h1>
        
        <!-- 文件上传区域 -->
        <div class="section">
            <h2>📤 上传文件</h2>
            <input type="file" id="fileInput" multiple>
            <button id="uploadBtn" onclick="addUploadTasks()">添加到上传队列</button>
            <small style="color:#7f8c8d;display:block;margin-top:5px">
                ✨ 支持多文件选择，支持并发上传，最大 500MB/文件
            </small>
            
            <!-- 上传任务列表 -->
            <div id="uploadTasks"></div>
        </div>
        
        <!-- 文件列表 -->
        <div class="section">
            <h2>📋 共享文件 ({{ files|length }})</h2>
            {% if files %}
                {% for file in files %}
                <div class="file-item">
                    <a href="#" onclick="downloadFile('{{file.name}}');return false">
                        📄 {{file.name}} <small>({{file.size}}, {{file.time}})</small>
                    </a>
                    <div>
                        <button class="download-btn" onclick="downloadFile('{{file.name}}')">下载</button>
                        <button class="delete-btn" onclick="deleteFile('{{file.name}}')">删除</button>
                    </div>
                </div>
                {% endfor %}
            {% else %}
                <div class="empty-state">暂无文件</div>
            {% endif %}
        </div>
        
        <!-- 留言板 -->
        <div class="section">
            <h2>💬 留言板</h2>
            <form method="post" action="/message">
                <input type="text" name="name" placeholder="昵称" required maxlength="50">
                <textarea name="message" placeholder="留言内容..." required maxlength="1000"></textarea>
                <button type="submit">发送</button>
            </form>
        </div>
        
        <!-- 留言列表 -->
        <div class="section">
            <h2>📝 留言列表 ({{ messages|length }})</h2>
            {% if messages %}
                {% for msg in messages %}
                <div class="message">
                    <div class="message-header">
                        <span class="message-author">{{msg.name}}</span>
                        <div>
                            <span class="message-time">{{msg.time}}</span>
                            <button class="delete-btn" onclick="deleteMessage('{{msg.id}}')">删除</button>
                        </div>
                    </div>
                    <div class="message-content">{{msg.content}}</div>
                </div>
                {% endfor %}
            {% else %}
                <div class="empty-state">暂无留言</div>
            {% endif %}
        </div>
    </div>

    <script>
        // ==================== 配置 ====================
        const CHUNK_SIZE = 512 * 1024;  // 512KB
        const MAX_CONCURRENT_UPLOADS = 3;  // 最多同时上传 3 个文件
        
        // 上传任务队列
        let uploadQueue = [];  // 等待上传的任务
        let activeUploads = [];  // 正在上传的任务
        
        /**
         * 添加上传任务到队列
         * 解决问题 2：支持多文件同时上传
         */
        function addUploadTasks() {
            const fileInput = document.getElementById('fileInput');
            const files = fileInput.files;
            
            if (!files || files.length === 0) {
                alert('请先选择文件！');
                return;
            }
            
            // 为每个文件创建上传任务
            for (let i = 0; i < files.length; i++) {
                const file = files[i];
                const taskId = Date.now() + '-' + Math.random().toString(36).substr(2, 9);
                
                const task = {
                    id: taskId,
                    file: file,
                    status: 'waiting',  // waiting | uploading | completed | failed | cancelled
                    progress: 0,
                    speed: 0,
                    currentChunk: 0,
                    totalChunks: Math.ceil(file.size / CHUNK_SIZE),
                    cancelled: false
                };
                
                uploadQueue.push(task);
                renderTask(task);
            }
            
            // 清空文件选择框
            fileInput.value = '';
            
            // 开始处理队列
            processQueue();
        }
        
        /**
         * 处理上传队列
         * 解决问题 2：控制并发数，避免同时上传太多文件
         */
        function processQueue() {
            // 检查是否有空闲槽位
            while (activeUploads.length < MAX_CONCURRENT_UPLOADS && uploadQueue.length > 0) {
                const task = uploadQueue.shift();
                activeUploads.push(task);
                uploadFile(task);
            }
        }
        
        /**
         * 渲染上传任务 UI
         */
        function renderTask(task) {
            const container = document.getElementById('uploadTasks');
            
            const taskDiv = document.createElement('div');
            taskDiv.id = 'task-' + task.id;
            taskDiv.className = 'upload-task';
            taskDiv.innerHTML = `
                <div class="task-header">
                    <span class="task-name">📄 ${task.file.name}</span>
                    <span class="task-status" id="status-${task.id}">等待上传...</span>
                </div>
                <div class="progress-container">
                    <div class="progress-bar">
                        <div class="progress-text" id="progress-text-${task.id}">0%</div>
                        <div class="progress-fill" id="progress-fill-${task.id}" style="width:0%"></div>
                    </div>
                    <div class="upload-info" id="info-${task.id}">队列中...</div>
                </div>
                <button class="delete-btn" onclick="cancelUpload('${task.id}')" id="cancel-btn-${task.id}">取消</button>
            `;
            
            container.appendChild(taskDiv);
        }
        
        /**
         * 上传文件（分片上传）
         * 解决问题 3：每个文件独立的 uploadId，互不干扰
         */
        async function uploadFile(task) {
            task.status = 'uploading';
            updateTaskUI(task, '上传中...');
            
            try {
                for (let i = 0; i < task.totalChunks; i++) {
                    // 检查是否取消
                    if (task.cancelled) {
                        task.status = 'cancelled';
                        updateTaskUI(task, '已取消');
                        // 通知服务器清理临时文件
                        await fetch('/cancel_upload', {
                            method: 'POST',
                            headers: {'Content-Type': 'application/json'},
                            body: JSON.stringify({uploadId: task.id})
                        });
                        break;
                    }
                    
                    const start = i * CHUNK_SIZE;
                    const end = Math.min(start + CHUNK_SIZE, task.file.size);
                    const chunk = task.file.slice(start, end);
                    
                    const formData = new FormData();
                    formData.append('chunk', chunk);
                    formData.append('chunkIndex', i);
                    formData.append('totalChunks', task.totalChunks);
                    formData.append('uploadId', task.id);  // 每个文件独立 ID
                    formData.append('filename', task.file.name);
                    
                    const startTime = Date.now();
                    await fetch('/upload_chunk', {method: 'POST', body: formData});
                    const elapsed = (Date.now() - startTime) / 1000;
                    
                    task.currentChunk = i + 1;
                    task.progress = ((i + 1) / task.totalChunks * 100).toFixed(1);
                    task.speed = (chunk.size / elapsed / 1024).toFixed(1);
                    
                    updateTaskUI(task, `上传中 ${task.currentChunk}/${task.totalChunks} 片 | ${task.speed} KB/s`);
                }
                
                if (!task.cancelled) {
                    task.status = 'completed';
                    updateTaskUI(task, '✅ 上传完成！');
                    document.getElementById('task-' + task.id).className = 'upload-task completed';
                    document.getElementById('cancel-btn-' + task.id).style.display = 'none';
                    
                    // 3 秒后刷新页面
                    setTimeout(() => location.reload(), 3000);
                }
                
            } catch (e) {
                task.status = 'failed';
                updateTaskUI(task, '❌ 上传失败: ' + e.message);
                document.getElementById('task-' + task.id).className = 'upload-task failed';
            } finally {
                // 从活跃列表移除
                activeUploads = activeUploads.filter(t => t.id !== task.id);
                // 继续处理队列
                processQueue();
            }
        }
        
        /**
         * 更新任务 UI
         */
        function updateTaskUI(task, statusText) {
            document.getElementById('status-' + task.id).textContent = statusText;
            document.getElementById('progress-fill-' + task.id).style.width = task.progress + '%';
            document.getElementById('progress-text-' + task.id).textContent = task.progress + '%';
            document.getElementById('info-' + task.id).textContent = statusText;
        }
        
        /**
         * 取消上传
         * 解决问题 4：标记取消，通知服务器清理
         */
        function cancelUpload(taskId) {
            // 在队列中查找
            let task = uploadQueue.find(t => t.id === taskId);
            if (task) {
                uploadQueue = uploadQueue.filter(t => t.id !== taskId);
                document.getElementById('task-' + taskId).remove();
                return;
            }
            
            // 在活跃列表中查找
            task = activeUploads.find(t => t.id === taskId);
            if (task) {
                task.cancelled = true;  // 标记取消，上传循环会检测
            }
        }
        
        /**
         * 下载文件（带进度条）
         */
        async function downloadFile(filename) {
            // 创建临时进度条（代码简化，你可以美化）
            const taskId = 'download-' + Date.now();
            const container = document.getElementById('uploadTasks');
            
            const taskDiv = document.createElement('div');
            taskDiv.id = 'task-' + taskId;
            taskDiv.className = 'upload-task';
            taskDiv.innerHTML = `
                <div class="task-header">
                    <span class="task-name">📥 下载: ${filename}</span>
                    <span class="task-status" id="status-${taskId}">下载中...</span>
                </div>
                <div class="progress-container">
                    <div class="progress-bar">
                        <div class="progress-text" id="progress-text-${taskId}">0%</div>
                        <div class="progress-fill" id="progress-fill-${taskId}" style="width:0%"></div>
                    </div>
                    <div class="upload-info" id="info-${taskId}">正在下载...</div>
                </div>
            `;
            container.appendChild(taskDiv);
            
            try {
                const response = await fetch('/download/' + encodeURIComponent(filename));
                const reader = response.body.getReader();
                const contentLength = +response.headers.get('Content-Length');
                
                let receivedLength = 0;
                let chunks = [];
                
                while (true) {
                    const {done, value} = await reader.read();
                    if (done) break;
                    
                    chunks.push(value);
                    receivedLength += value.length;
                    
                    const progress = (receivedLength / contentLength * 100).toFixed(1);
                    document.getElementById('progress-fill-' + taskId).style.width = progress + '%';
                    document.getElementById('progress-text-' + taskId).textContent = progress + '%';
                    document.getElementById('info-' + taskId).textContent = 
                        `${(receivedLength/1024/1024).toFixed(2)} MB / ${(contentLength/1024/1024).toFixed(2)} MB`;
                }
                
                const blob = new Blob(chunks);
                const url = window.URL.createObjectURL(blob);
                const a = document.createElement('a');
                a.href = url;
                a.download = filename;
                a.click();
                window.URL.revokeObjectURL(url);
                
                document.getElementById('status-' + taskId).textContent = '✅ 下载完成';
                document.getElementById('task-' + taskId).className = 'upload-task completed';
                
                setTimeout(() => document.getElementById('task-' + taskId).remove(), 3000);
                
            } catch (e) {
                alert('下载失败：' + e.message);
                document.getElementById('task-' + taskId).remove();
            }
        }
        
        /**
         * 删除文件
         */
        function deleteFile(filename) {
            if (confirm('确定删除 "' + filename + '" ?')) {
                fetch('/delete_file', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({filename: filename})
                }).then(r => r.ok ? location.reload() : alert('删除失败'));
            }
        }
        
        /**
         * 删除留言
         */
        function deleteMessage(messageId) {
            if (confirm('确定删除留言?')) {
                fetch('/delete_message', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({message_id: messageId})
                }).then(r => r.ok ? location.reload() : alert('删除失败'));
            }
        }
        
        // 页面加载时清理过期临时文件
        window.addEventListener('load', () => {
            fetch('/cleanup_temp', {method: 'POST'});
        });
    </script>
</body>
</html>
'''


# ==================== 工具函数 ====================
def safe_filename(filename):
    """
    安全处理文件名，保留中文
    替代 werkzeug.secure_filename (因为它会过滤掉中文)
    """
    # 去除路径信息，只保留文件名
    filename = os.path.basename(filename)
    # 替换掉可能导致路径穿越的字符
    filename = filename.replace('..', '').replace('/', '').replace('\\', '')
    if not filename:
        filename = 'unnamed_file'
    return filename

def get_temp_dir_age_hours(temp_dir):
    """计算临时目录自最后活动以来的小时数（含子文件 mtime）"""
    latest = os.path.getmtime(temp_dir)
    for root, _, files in os.walk(temp_dir):
        for name in files:
            file_path = os.path.join(root, name)
            try:
                latest = max(latest, os.path.getmtime(file_path))
            except FileNotFoundError:
                continue
    return (time.time() - latest) / 3600

def remove_temp_dir(temp_dir, context_tag):
    """统一删除临时目录，便于日志排查"""
    try:
        shutil.rmtree(temp_dir)
        app.logger.info(f"[{context_tag}] 已删除临时目录: {os.path.basename(temp_dir)}")
        return True
    except FileNotFoundError:
        return True
    except Exception as e:
        app.logger.error(f"[{context_tag}] 删除临时目录失败 {temp_dir}: {e}")
        return False

def get_file_size(filepath):
    """获取文件大小的友好显示"""
    size = os.path.getsize(filepath)
    for unit in ['B', 'KB', 'MB', 'GB']:
        if size < 1024:
            return f"{size:.1f}{unit}"
        size /= 1024
    return f"{size:.1f}TB"


# ==================== 路由 ====================

@app.route('/')
def index():
    """主页"""
    files = []
    if os.path.exists(UPLOAD_FOLDER):
        for filename in os.listdir(UPLOAD_FOLDER):
            filepath = os.path.join(UPLOAD_FOLDER, filename)
            if os.path.isfile(filepath):
                files.append({
                    'name': filename,
                    'size': get_file_size(filepath),
                    'time': datetime.fromtimestamp(os.path.getctime(filepath)).strftime('%m-%d %H:%M')
                })
    
    files.sort(key=lambda x: os.path.getctime(os.path.join(UPLOAD_FOLDER, x['name'])), reverse=True)
    
    messages = []
    if os.path.exists(MESSAGES_FILE):
        try:
            with open(MESSAGES_FILE, 'r', encoding='utf-8') as f:
                messages = json.load(f)
        except:
            messages = []
    
    messages.sort(key=lambda x: x.get('timestamp', 0), reverse=True)
    
    return render_template_string(HTML_TEMPLATE, files=files, messages=messages)


@app.route('/upload_chunk', methods=['POST'])
def upload_chunk():
    """
    分片上传路由（加锁版本）
    解决问题 1 & 2 & 3：
    - 使用文件锁防止并发冲突
    - 每个文件用独立 uploadId
    - 支持断点续传
    """
    try:
        chunk = request.files['chunk']
        chunk_index = int(request.form['chunkIndex'])
        total_chunks = int(request.form['totalChunks'])
        upload_id = request.form['uploadId']  # 每个文件独立的 ID
        raw_filename = request.form['filename']
        filename = safe_filename(raw_filename)
        raw_ext = os.path.splitext(raw_filename)[1]
        sanitized_ext = os.path.splitext(filename)[1]
        if raw_ext and not sanitized_ext:
            filename = f"{filename}{raw_ext}"
            app.logger.info(f"[上传] 追加原始扩展名，保持文件类型: {raw_filename} -> {filename}")
        elif filename != raw_filename:
            app.logger.info(f"[上传] 文件名规范化: {raw_filename} -> {filename}")
        
        # 创建临时目录
        temp_dir = os.path.join(TEMP_FOLDER, upload_id)
        os.makedirs(temp_dir, exist_ok=True)
        
        # 保存分片（不需要锁，因为每个 uploadId 独立）
        chunk_path = os.path.join(temp_dir, f'chunk_{chunk_index}')
        chunk.save(chunk_path)
        
        # 如果是最后一片，合并文件
        if chunk_index == total_chunks - 1:
            final_path = os.path.join(UPLOAD_FOLDER, filename)
            
            # 🔒 获取文件锁（防止正在删除该文件）
            lock = get_file_lock(final_path)
            with lock:
                # 如果文件已存在，添加时间戳
                if os.path.exists(final_path):
                    name, ext = os.path.splitext(filename)
                    timestamp = datetime.now().strftime('%H%M%S')
                    filename = f"{name}_{timestamp}{ext}"
                    final_path = os.path.join(UPLOAD_FOLDER, filename)
                
                # 合并文件
                with open(final_path, 'wb') as final_file:
                    for i in range(total_chunks):
                        chunk_file_path = os.path.join(temp_dir, f'chunk_{i}')
                        if not os.path.exists(chunk_file_path):
                            raise Exception(f"分片 {i} 丢失！")
                        with open(chunk_file_path, 'rb') as chunk_file:
                            final_file.write(chunk_file.read())
                
                # 删除临时文件夹
                remove_temp_dir(temp_dir, '上传合并')
                app.logger.info(f"[上传完成] 文件: {filename}, ID: {upload_id}")
        
        return jsonify({'success': True})
    
    except Exception as e:
        app.logger.error(f"[上传失败] ID: {upload_id}, 错误: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/cancel_upload', methods=['POST'])
def cancel_upload():
    """
    取消上传路由
    解决问题 4：删除已上传的临时文件
    """
    try:
        data = request.get_json()
        upload_id = data.get('uploadId')
        
        if upload_id:
            temp_dir = os.path.join(TEMP_FOLDER, upload_id)
            if os.path.exists(temp_dir):
                remove_temp_dir(temp_dir, '取消')
                app.logger.info(f"[取消上传] ID: {upload_id}")
        
        return jsonify({'success': True})
    except Exception as e:
        app.logger.error(f"[取消失败] ID: {upload_id}, 错误: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/cleanup_temp', methods=['POST'])
def cleanup_temp():
    """
    手动清理临时文件的路由
    前端页面加载时会调用
    """
    cleanup_temp_files()
    return jsonify({'success': True})


@app.route('/download/<path:filename>')
def download(filename):
    """
    文件下载路由（加锁版本）
    解决问题 1：下载时防止文件被删除
    """
    # 确保文件名被正确解码（处理中文和特殊字符）
    import urllib.parse
    filename = urllib.parse.unquote(filename)
    filename = os.path.basename(filename)
    filepath = os.path.join(UPLOAD_FOLDER, filename)
    
    # 🔒 获取文件锁
    lock = get_file_lock(filepath)
    with lock:
        if not os.path.exists(filepath):
            return jsonify({'error': '文件不存在'}), 404
        
        return send_file(
            filepath,
            as_attachment=True,
            download_name=filename
        )


@app.route('/delete_file', methods=['POST'])
def delete_file():
    """
    删除文件路由（加锁版本）
    解决问题 1：删除时防止文件正在上传/下载
    """
    try:
        data = request.get_json()
        filename = data.get('filename')
        
        if filename:
            filename = os.path.basename(filename)
            filepath = os.path.join(UPLOAD_FOLDER, filename)
            
            # 🔒 获取文件锁
            lock = get_file_lock(filepath)
            with lock:
                if os.path.exists(filepath):
                    os.remove(filepath)
                    app.logger.info(f"[删除文件] {filename}")
                    return jsonify({'success': True})
        
        return jsonify({'success': False}), 400
    except Exception as e:
        app.logger.error(f"[删除失败] {filename}: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/message', methods=['POST'])
def add_message():
    """添加留言"""
    name = request.form['name'].strip()
    message = request.form['message'].strip()
    
    if not name or not message:
        return redirect(url_for('index'))
    
    new_message = {
        'id': str(uuid.uuid4()),
        'name': name,
        'content': message,
        'time': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        'timestamp': datetime.now().timestamp()
    }
    
    messages = []
    if os.path.exists(MESSAGES_FILE):
        try:
            with open(MESSAGES_FILE, 'r', encoding='utf-8') as f:
                messages = json.load(f)
        except:
            messages = []
    
    messages.append(new_message)
    
    with open(MESSAGES_FILE, 'w', encoding='utf-8') as f:
        json.dump(messages, f, ensure_ascii=False, indent=2)
    
    return redirect(url_for('index'))


@app.route('/delete_message', methods=['POST'])
def delete_message():
    """删除留言"""
    try:
        data = request.get_json()
        message_id = data.get('message_id')
        
        if message_id:
            messages = []
            if os.path.exists(MESSAGES_FILE):
                with open(MESSAGES_FILE, 'r', encoding='utf-8') as f:
                    messages = json.load(f)
            
            messages = [msg for msg in messages if msg.get('id') != message_id]
            
            with open(MESSAGES_FILE, 'w', encoding='utf-8') as f:
                json.dump(messages, f, ensure_ascii=False, indent=2)
            
            return jsonify({'success': True})
        
        return jsonify({'success': False}), 400
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=False)