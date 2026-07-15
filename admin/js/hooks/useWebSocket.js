/**
 * 文件职责：WebSocket Hook，负责全局单例连接管理与实时消息分发。
 */

// 管理端全局只维护一个 WebSocket 连接，避免多个组件重复建立相同连接。
let __globalWs = null;
// 所有订阅实时数据的回调统一挂到一个监听集合中，由单连接分发。
let __wsListeners = new Set();
// 重连定时器与退避参数：插件重载期间避免高频重连打满日志。
let __reconnectTimer = null;
const WS_RECONNECT_BASE_DELAY_MS = 1000;
const WS_RECONNECT_MAX_DELAY_MS = 10000;
let __nextReconnectDelayMs = WS_RECONNECT_BASE_DELAY_MS;
let __authRejected = false;

function clearReconnectTimer() {
    if (!__reconnectTimer) return;
    clearTimeout(__reconnectTimer);
    __reconnectTimer = null;
}

function resetReconnectBackoff() {
    __nextReconnectDelayMs = WS_RECONNECT_BASE_DELAY_MS;
    clearReconnectTimer();
}

function scheduleReconnect() {
    // 没有任何订阅者时不需要重连，避免后台空转。
    if (__wsListeners.size === 0) return;
    // 鉴权失败需要回到登录流程，不能带着失效 token 无限重连。
    if (__authRejected) return;
    // 已有重连任务时直接复用，防止重复排队。
    if (__reconnectTimer) return;

    const delay = __nextReconnectDelayMs;
    __nextReconnectDelayMs = Math.min(__nextReconnectDelayMs * 2, WS_RECONNECT_MAX_DELAY_MS);

    __reconnectTimer = setTimeout(() => {
        __reconnectTimer = null;
        ensureGlobalWs();
    }, delay);
}

function ensureGlobalWs() {
    // 若连接已存在且仍处于“连接中 / 已连接”状态，则直接复用。
    if (__globalWs && (__globalWs.readyState === WebSocket.OPEN || __globalWs.readyState === WebSocket.CONNECTING)) {
        return;
    }

    const token = window.AuthUtil.getToken();
    const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
    const ws = new WebSocket(`${protocol}//${window.location.host}/ws`);
    __globalWs = ws;

    ws.onopen = function () {
        // 连接恢复后重置退避，后续若再次断开可从短延迟开始。
        if (__globalWs === ws) {
            resetReconnectBackoff();
        }
        // 与 HTTP 保持相同的 token 来源，但 token 只走首帧 JSON，不进入 URL。
        if (token && token !== 'no-auth') {
            try {
                ws.send(JSON.stringify({ type: 'auth', token }));
            } catch (e) {
                // 发送失败会触发 close/error 流程，交给统一重连处理。
            }
        }
    };

    ws.onmessage = function (event) {
        try {
            const msg = JSON.parse(event.data);
            // 仅处理后端定义的两类数据推送消息，其余消息类型忽略。
            if (msg.type === 'full_update' || msg.type === 'update') {
                const data = msg.data || {};
                __wsListeners.forEach((listener) => {
                    try {
                        // 将后端 payload 原样广播给所有订阅者，由页面组件自行决定如何消费。
                        listener(data);
                    } catch (e) {
                        // 单个监听器报错不应影响其他订阅者继续接收消息。
                    }
                });
            }
        } catch (e) {
            // 非 JSON 消息或异常格式直接忽略，避免污染控制台与中断连接。
        }
    };

    ws.onerror = function () {
        // 出错后主动触发 close 流程，统一走 onclose 的回收与重连路径。
        try {
            ws.close();
        } catch (e) {
            // 某些浏览器状态下 close 可能抛错，这里吞掉即可。
        }
    };

    ws.onclose = function (event) {
        // 仅处理当前活动连接的关闭事件，避免旧连接回调干扰新的连接状态。
        if (__globalWs !== ws) {
            return;
        }
        __globalWs = null;
        if (event && event.code === 1008) {
            __authRejected = true;
            clearReconnectTimer();
            if (window.AuthUtil && typeof window.AuthUtil.handleAuthFailure === 'function') {
                window.AuthUtil.handleAuthFailure();
            } else if (window.AuthUtil && typeof window.AuthUtil.clearToken === 'function') {
                window.AuthUtil.clearToken();
            }
            return;
        }
        // 连接关闭后自动尝试重连，覆盖插件重载等短暂不可用场景。
        scheduleReconnect();
    };
}

function useWebSocket(onData) {
    React.useEffect(() => {
        if (typeof onData === 'function') {
            __wsListeners.add(onData);
        }

        // 只要有任意一个订阅者存在，就确保全局连接已建立。
        __authRejected = false;
        ensureGlobalWs();

        return () => {
            if (typeof onData === 'function') {
                __wsListeners.delete(onData);
            }
            // 最后一个订阅者离开时主动关闭连接，减少空闲资源占用。
            if (__wsListeners.size === 0) {
                clearReconnectTimer();
                __nextReconnectDelayMs = WS_RECONNECT_BASE_DELAY_MS;
                if (__globalWs) {
                    try {
                        __globalWs.close();
                    } catch (e) {
                        // 某些浏览器状态下 close 可能抛错，这里吞掉即可。
                    }
                }
                __globalWs = null;
            }
        };
    }, [onData]);
}

// 暴露为全局 Hook，供入口应用与各页面脚本直接调用。
window.useWebSocket = useWebSocket;
