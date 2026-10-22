# 最终环境保持干净（最新范围）

只处理 Mac 本地；不清理、不改动云端目录或工作树。
本地主仓库只有 main 工作树及 31 项已有未提交变更，保留该目录完整不变，不 reset/clean/删除。
在父仓库已通过本地 .git/info/exclude 忽略的 cloud-rag-integration 子目录中，
从真实云端 bundle 建立独立 clone，作为唯一部署运行环境。
本包 Mac 适配是独立可核验补丁；本地验证后在 local/mac-deepseek-qwen 正常提交为新提交，
不得冒用 afcffff 哈希。运行 .env、.local、.cloud、数据库卷和缓存均保持忽略，
最终部署 clone 的 git status --short 应为空；Git 历史保留 cloud/rag-integration 与 97ec6fe。
本地原 main 的 31 项变更保持原样，不能把它们混进本次部署提交。
