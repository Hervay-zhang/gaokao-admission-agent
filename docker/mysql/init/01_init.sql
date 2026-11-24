-- ============================================================
--  MySQL 容器初始化（仅在数据卷为空时执行一次）
--  字符集必须 utf8mb4，否则中文校名/政策文本乱码
-- ============================================================
CREATE DATABASE IF NOT EXISTS gaokao
    DEFAULT CHARACTER SET utf8mb4
    DEFAULT COLLATE utf8mb4_0900_ai_ci;

USE gaokao;

-- 全文检索需 ngram 解析器（T13 RAG 用，此处确保可用）
-- 注意：ngram_token_size 是启动参数，需在 my.cnf 配置，见 docs 说明
SELECT '数据库 gaokao 初始化完成（utf8mb4）' AS message;
