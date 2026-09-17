# 两个独立Compose项目连接Worker

本文件命令仅供部署机器执行。本机不运行Docker、不构建镜像、不启动服务。Worker使用独立compose.worker.yaml，不与gptbot合成一个Compose项目。

## 沿用原配置和目录

直接使用原.env和data/config/platform_config.yaml，不另建.env.worker或data/worker。已有.env请勿覆盖，只补充WORKER_SERVICE_KEY（至少32字符）。首次安装才将.env.example复制为.env。

Token必须与gptbot目标账号相同。API_ID/API_HASH、BOT_PROXY及原目录配置继续保留。Worker使用独立持久的data/sessions/worker_sender_<BotID>.session，并以no_updates=True运行发送客户端；直出请求由Worker组装上传并发送RichMessage。原bot_<BotID>.session保持不变。旧bot.py不要同时处理同一消息。

Compose挂载原目录：

| 宿主目录 | 容器目录 | 用途 |
| --- | --- | --- |
| ./data | /app/data | 原平台YAML、data/db/database.db及session |
| ./downloads | /app/downloads | 下载与48小时媒体缓存 |
| ./logs | /app/logs | 原日志路径保留 |

Worker启动时初始化原数据库表，并新增带worker_前缀的表；原业务表保留。若原.env自定义DATABASE_URL、DATA_PATH或DOWNLOAD_DIR，请保留相同配置并确保对应路径已挂载。平台Cookie/代理仍存原YAML，gptbot Admin是该文件的远程编辑入口，保存后重启Worker生效。

如果原Bot由仓库的`docker-compose.yaml`运行，它默认使用命名卷`parse_hub_bot_data`，而本文件默认使用宿主机`./data`，两者不是同一存储。要让Worker复用原Bot的持久解析缓存和配置，使用可选覆盖文件：

```sh
docker compose -f compose.worker.yaml -f compose.worker.shared-data.yaml up -d --build
```

若实际卷名不同，先在部署机确认卷名，再设置`PARSEHUB_DATA_VOLUME`。覆盖文件将该现有卷作为external卷挂到`/app/data`，不会迁移或替换已有数据。原Bot本来就使用宿主`./data`时继续使用基础文件即可。

## 在部署机器构建和启动

在parse_hub_bot仓库目录，保留已有.env并填入WORKER_SERVICE_KEY后：

```sh
mkdir -p data downloads logs
chmod 600 .env
# 只在部署机器执行
docker compose -f compose.worker.yaml up -d --build
docker compose -f compose.worker.yaml ps
docker compose -f compose.worker.yaml logs --tail=100 parsehub-worker
```

镜像parsehub-worker:local复用当前Dockerfile从源码构建；Compose命令覆盖为python -m worker，不启动bot.py。原Dockerfile、原docker-compose.yaml及原Bot功能保持不变。仅新增独立Worker部署入口。

容器内部显式允许监听0.0.0.0:8080，宿主端口默认仅127.0.0.1:8080。所有API仍需Bearer认证。健康检查验证协议版本3和配置的Bot ID；平台配置在启动时已从原YAML读取。

## gptbot连接配置

如果gptbot是宿主机原生进程，config.toml的[vars]使用：

```toml
PARSEHUB_WORKER_URL = 'http://127.0.0.1:8080'
PARSEHUB_WORKER_SECRET = '<与.env的WORKER_SERVICE_KEY相同>'
PARSEHUB_WORKER_ACCOUNT_ID = '<BOT_TOKEN冒号前的Bot ID>'
```

如果gptbot也是独立容器，可选共享网络覆盖文件。先启动Worker创建parsehub-worker-network，再于gptbot仓库执行：

```sh
# 只在部署机器执行
docker compose -f docker-compose.yml -f compose.parsehub-worker.yml up -d --build midoban
```

此时地址用http://parsehub-worker:8080。覆盖文件只给gptbot增加网络，不定义Worker、不绑定depends_on；两个项目仍独立启停。已有网络能互通时不需要覆盖文件，直接配置可达Worker地址。默认仅发布宿主127.0.0.1的端口无法让另一容器通过宿主网关访问，不要混淆网络地址。

## 变更平台配置

直接编辑data/config/platform_config.yaml，或通过gptbot Admin→ParseHub远程编辑同一文件，然后在部署机器执行：

```sh
docker compose -f compose.worker.yaml restart parsehub-worker
```

gptbot不再推送本地平台配置覆盖它。没有旧缓存或数据库迁移/删除步骤；原缓存记录保留，Worker仅复用自己的已验证记录。

升级文件交接协议时，两端须配套更新。gptbot读取带认证、受租约保护的媒体流；直出请求由Worker完成Telegram发送。直出与文件交接缓存使用不同键，不需要删除数据库。

## 下载目录与文件名

Worker运行原`ParsePipeline`：例如downloads/作品标题/作品标题.mp4，图集沿用001_作品标题.jpg等原命名；重名目录由原库生成作品标题_2。下载重试、processed目录及_remux、_h264、_split规则均由原流水线决定，Worker只把结果适配为消息；归档使用原同名.tar.gz规则。缓存通过数据库登记目录与归档路径管理，不按worker-前缀扫描删除；没有登记的原下载不会被清理。

## 维护与清理缓存

内置维护脚本只清理数据库登记的落盘文件，不会误删非 Worker 历史文件。脚本会申请与 Worker 进程相同的数据目录锁：Worker 运行时执行会直接拒绝并退出码 1，因此需要先停止服务，用一次性容器运行脚本，再启动：

```sh
docker compose -f compose.worker.yaml stop parsehub-worker

# 查看当前缓存占用与条目统计
docker compose -f compose.worker.yaml run --rm parsehub-worker python -m worker.clean_cache --stats

# 演练预览（仅查看将清理的记录与容量，不实际执行删除）
docker compose -f compose.worker.yaml run --rm parsehub-worker python -m worker.clean_cache --dry-run

# 快速全量清理（默认跳过未到期的活跃租约）
docker compose -f compose.worker.yaml run --rm parsehub-worker python -m worker.clean_cache

# 强制全量清理（连同未到期租约一并清空）
docker compose -f compose.worker.yaml run --rm parsehub-worker python -m worker.clean_cache --force

# 仅清理指定链接的缓存（按当前 BOT_TOKEN 对应的任务键匹配，也接受别名）
docker compose -f compose.worker.yaml run --rm parsehub-worker python -m worker.clean_cache --url "https://..."

docker compose -f compose.worker.yaml start parsehub-worker
```

全量清理不会删除任务表：已持久化的交付回执用于幂等重查，由 Worker 自身按 48 小时过期。
