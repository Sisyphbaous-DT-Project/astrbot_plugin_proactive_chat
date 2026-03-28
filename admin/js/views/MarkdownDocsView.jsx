/**
 * 文件职责：Markdown 文档浏览视图，负责文档目录展示、内容加载与原生 Markdown 阅读体验。
 */

const { Box, Typography, Button, Chip } = MaterialUI;

function MarkdownDocsView() {
    const { state, dispatch } = useAppContext();
    const api = useApi();
    // 从全局状态读取文档目录与当前选中文档，便于视图切换后仍能保留上下文。
    const markdownFiles = Array.isArray(state.markdownFiles) ? state.markdownFiles : [];
    const markdownDocument = state.markdownDocument || null;
    const selectedMarkdownPath = String(state.selectedMarkdownPath || '');
    // 文档目录和正文分别维护加载状态，这样可以实现更细粒度的按钮禁用与空态反馈。
    const [loadingList, setLoadingList] = React.useState(false);
    const [loadingDocument, setLoadingDocument] = React.useState(false);

    const loadMarkdownFiles = React.useCallback(async () => {
        setLoadingList(true);
        try {
            const payload = await api.listMarkdownFiles();
            const items = Array.isArray(payload?.items) ? payload.items : [];
            dispatch({ type: 'SET_MARKDOWN_FILES', payload: items });

            if (!selectedMarkdownPath && items.length > 0) {
                // 首次进入文档页时自动选中第一篇文档，避免右侧区域出现长时间空白。
                dispatch({ type: 'SET_SELECTED_MARKDOWN_PATH', payload: items[0].path || '' });
            }
        } catch (e) {
            dispatch({ type: 'SET_ERROR', payload: e.message || '加载 Markdown 文档列表失败' });
        } finally {
            setLoadingList(false);
        }
    }, [api, dispatch, selectedMarkdownPath]);

    const loadMarkdownDocument = React.useCallback(async (path) => {
        const normalizedPath = String(path || '').trim();
        if (!normalizedPath) {
            // 空路径时直接清空当前文档，避免请求出错后残留旧内容误导用户。
            dispatch({ type: 'SET_MARKDOWN_DOCUMENT', payload: null });
            return;
        }

        setLoadingDocument(true);
        try {
            const payload = await api.getMarkdownFile(normalizedPath);
            dispatch({ type: 'SET_MARKDOWN_DOCUMENT', payload: payload || null });
            dispatch({ type: 'SET_SELECTED_MARKDOWN_PATH', payload: normalizedPath });
        } catch (e) {
            dispatch({ type: 'SET_ERROR', payload: e.message || '加载 Markdown 文档失败' });
        } finally {
            setLoadingDocument(false);
        }
    }, [api, dispatch]);

    React.useEffect(() => {
        if (markdownFiles.length === 0) {
            // 仅在目录为空时自动拉取，避免每次渲染都重复请求列表。
            loadMarkdownFiles();
        }
    }, [loadMarkdownFiles, markdownFiles.length]);

    React.useEffect(() => {
        if (!selectedMarkdownPath && markdownFiles.length > 0) {
            // 若当前尚未有选中文档，则默认加载目录中的第一项。
            loadMarkdownDocument(markdownFiles[0].path || '');
            return;
        }

        if (
            selectedMarkdownPath
            && (!markdownDocument || String(markdownDocument.path || '') !== selectedMarkdownPath)
        ) {
            // 当选中路径变化，但正文尚未同步时，再补拉一次内容，保证左右两栏状态一致。
            loadMarkdownDocument(selectedMarkdownPath);
        }
    }, [loadMarkdownDocument, markdownDocument, markdownFiles, selectedMarkdownPath]);

    const handleRefreshCurrent = async () => {
        // 刷新当前文档时先刷新目录，再刷新正文，避免文件列表已经变化但正文仍指向旧路径。
        await loadMarkdownFiles();
        if (selectedMarkdownPath) {
            await loadMarkdownDocument(selectedMarkdownPath);
        }
    };

    const currentDocumentTitle = markdownDocument?.title
        || markdownFiles.find((item) => item.path === selectedMarkdownPath)?.title
        || 'Markdown 文档';
    const currentDocumentPath = markdownDocument?.path || selectedMarkdownPath;
    const renderedHtml = markdownDocument?.content
        // 文档正文和通知正文共用同一套 MarkdownRenderUtil，确保渲染风格一致。
        ? window.MarkdownRenderUtil.renderMarkdownToHtml(markdownDocument.content)
        : '';

    return (
        <Box className="notifications-view markdown-docs-view">
            <div className="card notifications-hero-card markdown-docs-hero-card">
                <Box className="tasks-header-row notifications-header-row">
                    <div className="notifications-header-main">
                        <Box className="notifications-title-row">
                            <Box sx={{ display: 'flex', alignItems: 'center', gap: 1.25, flexWrap: 'wrap' }}>
                                <Typography variant="h6" sx={{ fontWeight: 800 }}>
                                    文档浏览 ({markdownFiles.length})
                                </Typography>
                                <Chip
                                    label={currentDocumentPath ? `当前：${currentDocumentTitle}` : '请选择文档'}
                                    size="small"
                                    color="primary"
                                    variant="outlined"
                                />
                            </Box>
                        </Box>
                        <Typography variant="body2" className="tasks-header-subtitle">
                            在插件前端中直接阅读原生 Markdown 文件，例如 README、CHANGELOG 与 docs 目录文档。
                        </Typography>
                    </div>
                    <Box className="notifications-actions-row">
                        <Button
                            variant="outlined"
                            onClick={loadMarkdownFiles}
                            disabled={loadingList}
                            sx={{ borderRadius: 3 }}
                        >
                            刷新目录
                        </Button>
                        <Button
                            variant="contained"
                            onClick={handleRefreshCurrent}
                            disabled={loadingList || loadingDocument || !currentDocumentPath}
                            startIcon={<span>📘</span>}
                            sx={{ borderRadius: 3, boxShadow: 'none', px: 2.25 }}
                        >
                            刷新文档
                        </Button>
                    </Box>
                </Box>
            </div>

            <div className="markdown-docs-shell">
                <aside className="markdown-docs-aside-column">
                    <div className="markdown-docs-aside-sticky">
                        <div className="card markdown-docs-sidebar-card">
                            <div className="markdown-docs-sidebar-head">
                                <Typography variant="subtitle1" sx={{ fontWeight: 700 }}>
                                    文档目录
                                </Typography>
                                <Typography variant="body2" color="text.secondary">
                                    仅展示插件目录内允许浏览的 Markdown 文件。
                                </Typography>
                            </div>

                            {markdownFiles.length === 0 ? (
                                <div className="tasks-empty-card markdown-docs-empty-side-card">
                                    <div className="tasks-empty-icon">📚</div>
                                    <Typography variant="body1" sx={{ fontWeight: 700, mb: 1 }}>
                                        {loadingList ? '正在加载文档列表…' : '暂无可浏览文档'}
                                    </Typography>
                                </div>
                            ) : (
                                <div className="markdown-docs-file-list">
                                    {markdownFiles.map((item) => {
                                        const isActive = item.path === currentDocumentPath;
                                        return (
                                            <button
                                                key={item.path}
                                                className={`markdown-docs-file-item ${isActive ? 'is-active' : ''}`}
                                                onClick={() => {
                                                    if (isActive) {
                                                        return;
                                                    }
                                                    loadMarkdownDocument(item.path);
                                                }}
                                            >
                                                <div className="markdown-docs-file-item-top">
                                                    <span className="markdown-docs-file-icon">📝</span>
                                                    <span className="markdown-docs-file-title">{item.title || item.filename || item.path}</span>
                                                </div>
                                                <div className="markdown-docs-file-path mono">{item.path}</div>
                                            </button>
                                        );
                                    })}
                                </div>
                            )}
                        </div>
                    </div>
                </aside>

                <div className="markdown-docs-content-column">
                    <div className="card markdown-docs-content-card">
                        {!currentDocumentPath ? (
                            <div className="tasks-empty-card markdown-docs-empty-card">
                                <div className="tasks-empty-icon">📄</div>
                                <Typography variant="h6" sx={{ fontWeight: 700, mb: 1 }}>
                                    请选择左侧文档
                                </Typography>
                                <Typography variant="body1" color="text.secondary">
                                    选择后即可在当前管理端中直接阅读 Markdown 文档内容。
                                </Typography>
                            </div>
                        ) : loadingDocument && !markdownDocument ? (
                            <div className="tasks-empty-card markdown-docs-empty-card">
                                <div className="tasks-empty-icon">⏳</div>
                                <Typography variant="h6" sx={{ fontWeight: 700, mb: 1 }}>
                                    正在加载文档内容…
                                </Typography>
                            </div>
                        ) : markdownDocument ? (
                            <>
                                <div className="markdown-docs-article-head">
                                    <div>
                                        <Typography variant="h5" sx={{ fontWeight: 800, mb: 0.5 }}>
                                            {currentDocumentTitle}
                                        </Typography>
                                        <Typography variant="body2" className="task-card-session-sub mono">
                                            {currentDocumentPath}
                                        </Typography>
                                    </div>
                                </div>
                                <Box
                                    className="task-countdown-text notification-feed-content-text notification-md markdown-docs-article"
                                    // 这里直接挂载已经渲染好的 HTML，正文样式由 notification-md 体系统一承接。
                                    dangerouslySetInnerHTML={{ __html: renderedHtml }}
                                />
                            </>
                        ) : (
                            <div className="tasks-empty-card markdown-docs-empty-card">
                                <div className="tasks-empty-icon">⚠️</div>
                                <Typography variant="h6" sx={{ fontWeight: 700, mb: 1 }}>
                                    文档暂时不可用
                                </Typography>
                                <Typography variant="body1" color="text.secondary">
                                    当前文档未能成功加载，请稍后重试。
                                </Typography>
                            </div>
                        )}
                    </div>
                </div>
            </div>
        </Box>
    );
}

window.MarkdownDocsView = MarkdownDocsView;
