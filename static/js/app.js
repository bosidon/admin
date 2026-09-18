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
