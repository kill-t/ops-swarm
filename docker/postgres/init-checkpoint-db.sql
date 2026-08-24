-- 仅在全新本地数据卷首次初始化时创建 LangGraph 专用数据库。
SELECT 'CREATE DATABASE opsswarm_checkpoints OWNER opsswarm'
WHERE NOT EXISTS (
    SELECT FROM pg_database WHERE datname = 'opsswarm_checkpoints'
)\gexec
