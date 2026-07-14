/**
 * 自媒体管理后台 - 前端脚本
 */

// Toast 通知
function showToast(message, type = 'info') {
    const toast = document.createElement('div');
    toast.className = `toast ${type}`;
    toast.textContent = message;
    document.body.appendChild(toast);
    
    setTimeout(() => toast.classList.add('show'), 10);
    setTimeout(() => {
        toast.classList.remove('show');
        setTimeout(() => toast.remove(), 300);
    }, 3000);
}

// API 请求
async function api(url, options = {}) {
    try {
        const response = await fetch(url, {
            headers: {
                'Content-Type': 'application/json',
                ...options.headers
            },
            ...options
        });
        return await response.json();
    } catch (error) {
        console.error('API Error:', error);
        showToast('请求失败: ' + error.message, 'error');
        throw error;
    }
}

// 更新状态
async function updateStatus(type, id, status) {
    const result = await api(`/api/${type}/${id}/status`, {
        method: 'POST',
        body: JSON.stringify({ status })
    });
    
    if (result.ok) {
        showToast('状态已更新', 'success');
        setTimeout(() => location.reload(), 500);
    } else {
        showToast('更新失败: ' + (result.error || '未知错误'), 'error');
    }
}

// 同步文章
async function syncArticles() {
    showToast('正在从服务器同步...', 'info');
    const result = await api('/api/sync', { method: 'POST' });
    
    if (result.ok) {
        showToast('同步成功', 'success');
        setTimeout(() => location.reload(), 1000);
    } else {
        showToast('同步失败: ' + (result.error || '未知错误'), 'error');
    }
}

// 生成文章
async function generateArticle() {
    if (!confirm('确定要生成新文章吗？')) return;
    
    showToast('正在生成文章，请稍候...', 'info');
    const result = await api('/api/articles/generate', { method: 'POST' });
    
    if (result.ok) {
        showToast('文章生成成功！', 'success');
        setTimeout(() => location.reload(), 1000);
    } else {
        showToast('生成失败: ' + (result.error || '未知错误'), 'error');
    }
}

// 格式化时间
function formatTime(timeStr) {
    if (!timeStr) return '-';
    const date = new Date(timeStr);
    const now = new Date();
    const diff = now - date;
    
    if (diff < 60000) return '刚刚';
    if (diff < 3600000) return Math.floor(diff / 60000) + '分钟前';
    if (diff < 86400000) return Math.floor(diff / 3600000) + '小时前';
    if (diff < 604800000) return Math.floor(diff / 86400000) + '天前';
    
    return date.toLocaleDateString('zh-CN');
}

// 状态标签
function getStatusTag(status) {
    const map = {
        'pending': '<span class="tag tag-yellow">⏳ 待审核</span>',
        'approved': '<span class="tag tag-green">✅ 已通过</span>',
        'published': '<span class="tag tag-blue">📤 已发布</span>',
        'rejected': '<span class="tag tag-red">❌ 已拒绝</span>'
    };
    return map[status] || '<span class="tag tag-gray">未知</span>';
}
