/**
 * 文件职责：WebSocket Hook，负责全局单例连接管理与实时消息分发。
 */

// 管理端全局只维护一个 WebSocket 连接，避免多个组件重复建立相同连接。
let __globalWs = null;
// 所有订阅实时数据的回调统一挂到一个监听集合中，由单连接分发。
let __wsListeners = new Set();

function ensureGlobalWs() {
    // 若连接已存在且仍处于“连接中 / 已连接”状态，则直接复用。
    if (__globalWs && (__globalWs.readyState === WebSocket.OPEN || __globalWs.readyState === WebSocket.CONNECTING)) {
        return;
    }

    // 与 HTTP 保持相同的 token 来源，统一从 AuthUtil 中读取。
    const token = window.AuthUtil.getToken();
    const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
    // 开启鉴权时通过 query 传 token；无鉴权或 no-auth 场景则不附加参数。
    const tokenQuery = token && token !== 'no-auth' ? `?token=${encodeURIComponent(token)}` : '';
    __globalWs = new WebSocket(`${protocol}//${window.location.host}/ws${tokenQuery}`);

    __globalWs.onmessage = function (event) {
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

    __globalWs.onclose = function () {
        // 连接关闭后清空引用，便于下次订阅时自动重连。
        __globalWs = null;
    };
}

function useWebSocket(onData) {
    React.useEffect(() => {
        if (typeof onData === 'function') {
            __wsListeners.add(onData);
        }

        // 只要有任意一个订阅者存在，就确保全局连接已建立。
        ensureGlobalWs();

        return () => {
            if (typeof onData === 'function') {
                __wsListeners.delete(onData);
            }
            // 最后一个订阅者离开时主动关闭连接，减少空闲资源占用。
            if (__wsListeners.size === 0 && __globalWs) {
                try {
                    __globalWs.close();
                } catch (e) {
                    // 某些浏览器状态下 close 可能抛错，这里吞掉即可。
                }
                __globalWs = null;
            }
        };
    }, [onData]);
}

// 暴露为全局 Hook，供入口应用与各页面脚本直接调用。
window.useWebSocket = useWebSocket;

