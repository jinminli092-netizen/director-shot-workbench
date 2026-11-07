# Seedance Cut

![License: Apache-2.0](https://img.shields.io/badge/license-Apache--2.0-blue.svg)
![Python](https://img.shields.io/badge/python-3.11%2B-informational)

A local, single-user shot splitter and shot analyser for short video.

Upload a clip, cut it into shots, then analyse each shot with any
OpenAI-compatible vision model you configure. No account system, no cloud
database, no uploads to a third-party video service.

> 中文：把视频上传到本机，用 ffmpeg 切镜，再用你自己的视觉模型逐镜分析。
> 没有账号、没有云端作品库，视频默认只留在这台电脑。

![Seedance Cut overview](docs/screenshots/overview.png)

*主界面：左边是视频列表，右边是当前视频的分镜、缩略图和逐镜分析结果。截图为演示视频，不是真实项目素材。*

## 三步工作流

```text
1. 上传视频      浏览器把文件发给本机服务，视频不经过第三方网盘
2. 自动切镜      ffmpeg 找硬切，生成每镜缩略图和可播放片段
3. 逐镜分析      每镜取 5 帧交给你的视觉模型，结果写回本地 SQLite
```

切镜不需要模型，也不需要联网。只有“逐镜分析”会调用你配置的视觉接口。

## 界面怎么读

| 区域 | 作用 |
| --- | --- |
| 上传视频 | 选择本地视频；上传后自动开始切镜 |
| 切点方式 | `严格硬切` / `包含转场` / `本地模型回退`，都可用免费本地模式 |
| 视频列表 | 所有已上传视频、镜头数、时长、分析状态 |
| 重新切镜 | 用新的切点方式重跑当前视频，旧分析会清空 |
| 逐镜分析 | 逐镜调用视觉模型，显示进度；没有配置模型时会明确提示 |
| 分镜卡片 | 每镜的缩略图、起止时间、景别、运镜、主体、走位、微表情、内容、为什么 |

![Mobile layout](docs/screenshots/mobile.png)

*窄屏会自动变成单列，上传、视频列表和分镜仍然可用。*

## 分析结果里有什么

每一镜保存以下字段，方便继续做拉片笔记、剪辑判断或提示词反推：

| 字段 | 含义 |
| --- | --- |
| `size` | 景别：远景 / 全景 / 中景 / 近景 / 特写 |
| `move` | 运镜：固定、推、拉、摇、移、跟、升降等 |
| `subject` | 画面主体 |
| `blocking` | 主体走位与画面位置变化 |
| `micro_expression` | 表情、眼神、手部等细节 |
| `content` | 这一镜发生了什么 |
| `why` / `why_note` | 为什么这样切、这样调度 |
| `rhythm` | 镜头长度与节奏判断 |
| `confidence` | 模型对这次判断的置信度 |

## 适合谁

- 导演 / 剪辑师：把参考片拆成可检索的镜头表。
- 分镜师：整理景别、运镜、走位，做自己的镜头库。
- AIGC 创作者：把实拍参考拆成逐镜描述，再改写成生成提示词。
- 影视学生：做拉片作业，保留每镜截图和时间码。

## 不适合什么

- 不是“一键模仿爆款”的成片生成器。
- 不替代人工判断，模型输出仍要复核。
- 不适合直接暴露到公网做多租户服务；这是单用户本地工具。

## Requirements

- Python 3.11+
- ffmpeg 5.1+ available on `PATH`
- A vision-capable OpenAI-compatible endpoint (optional; cutting works without it)

## Run

```bash
cp .env.example .env
# edit .env if you want analysis
python app.py
```

Open <http://127.0.0.1:8765>.

Data is written to `./data`:

```text
data/
  studio.db
  uploads/
  media/<media-id>/thumbs/
  media/<media-id>/clips/
```

Set `SEEDANCE_DATA_DIR` to move it.

## Configure a vision model

Any endpoint that accepts `POST /chat/completions` with image content works:
Doubao/ARK, DeepSeek-compatible gateways, Kimi, a local vLLM or LM Studio.

```dotenv
SEEDANCE_MODEL_BASE_URL=https://your-endpoint.example/v1
SEEDANCE_MODEL=your-vision-model
SEEDANCE_API_KEY=sk-...
SEEDANCE_MODEL_NAME=my-vision
```

The server never sends the key to the browser. If it is missing, cutting still
works and the analyse button explains what is missing.

## Cut modes

| Mode | Meaning | Cost |
| --- | --- | --- |
| `cuts` | strict hard cuts via ffmpeg `scdet` | free |
| `all` | also treat sustained scene-score runs as transitions | free |
| `model` | try the optional local TransNetV2 environment, fall back to `scdet` | free |

The local model is optional and is not included. The app works without it.

## API

```text
GET  /api/health
GET  /api/media
GET  /api/media/:id
POST /api/media/upload       raw body + X-Filename header
POST /api/media/:id/cut      {"mode":"cuts"|"all"|"model"}
POST /api/media/:id/analyze
GET  /media/:id/<relative-path>
```

## Tests

```bash
python -m unittest discover -s tests -v
```

The tests generate a local test-pattern video, cut it with ffmpeg, and do not
call any external API.

## Docker

```bash
docker compose up --build
```

Then open <http://127.0.0.1:8765>. Put your model settings in `.env`; the
`data/` directory is mounted as a volume.

## Security notes

- The server binds to `127.0.0.1` by default. Do not expose it publicly without
  adding authentication and upload limits.
- Upload size is capped by `SEEDANCE_MAX_UPLOAD_MB` (default 4096).
- Only whitelisted video extensions are accepted.
- Keys live in `.env` or the process environment, never in the repository.

## License

Apache-2.0 for this repository's code. ffmpeg is a separate program with its
own license; model weights and third-party assets are not bundled. See
`THIRD_PARTY.md` and `NOTICE`.

## Status

Early `0.1.0`. The UI is intentionally small; the analysis schema is stable
enough to build on, but expect changes before `1.0`.
